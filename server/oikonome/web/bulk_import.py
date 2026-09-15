"""Bulk historical import: drop a whole folder of mixed bank
files, get a per-file PLAN (detected type, destination account
recommendation, amount-sign guess with an uncertainty flag), edit it,
then run everything in one shot.

Staged uploads live in the tenant-bound `import_staging` table (an
in-memory dict breaks under multiple workers and evicts batches
silently); nothing touches the ledger until /run, and each file imports
through the SAME dispatch_import the single-file page uses — all its
dedup guards and undo batches apply."""

from __future__ import annotations

import csv as csvmod
import io
import itertools
import re

from ..db import staging

MAX_FILES = 200
# Aggregate row budget for one /run. Each file already carries the
# per-file row cap (sync.rowcap, 50k) and its own dedup scan, but a cap
# per file times MAX_FILES files still let one request process 200 × 50k
# rows synchronously. The budget bounds the whole batch; files past it
# are refused with a clear note and can be re-run in a second batch.
MAX_TOTAL_ROWS = 200_000

KINDS = {".zip": "Oikonome export ZIP", ".ofx": "OFX", ".qfx": "OFX",
         ".qif": "QIF", ".pdf": "PDF statement", ".csv": "CSV"}

# too generic to prove a filename ↔ account match on their own
_STOPWORDS = {"card", "bank", "credit", "debit", "account", "checking",
              "savings", "statement", "statements", "export", "exports",
              "download", "history", "transactions", "data", "test"}


def _name_tokens(s: str) -> set[str]:
    """The words in a filename or account name distinctive enough to prove
    they are the same account.

    Four letters and up, less the generic banking filler — with a fallback
    to the short words when NOTHING survives that. An account whose name is
    an initialism plus filler ('ING Savings', 'IRA') otherwise matches no
    filename at all, so every statement dropped for it silently creates a
    SECOND account beside the real one. The fallback is per string, so a
    name that has a long word still never matches on a short one, and pure
    digits never count as a name (the mask check above owns those)."""
    words = [t for t in re.split(r"[^a-z0-9]+", (s or "").lower())
             if t and not t.isdigit() and t not in _STOPWORDS]
    return {t for t in words if len(t) > 3} or {t for t in words if len(t) >= 2}


def _ext(name: str) -> str:
    name = name.lower()
    return name[name.rfind("."):] if "." in name else ""


def _digits(name: str) -> list[str]:
    """last-4 candidates from the FILENAME (mask matching).

    A 4-digit YEAR (1900-2099) is almost never an account mask — try real
    masks first and fall back to year-shaped groups only if nothing else
    matched, so 'Chase_4821_2023.csv' picks 4821, not 2023.
    """
    groups = re.findall(r"(?<!\d)(\d{4})(?!\d)", name)
    years = [g for g in groups if 1900 <= int(g) <= 2099]
    non_years = [g for g in groups if g not in years]
    return non_years + years


def _guess_account_name(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1]
    base = re.sub(r"\.[A-Za-z]+$", "", base)
    base = re.sub(r"[_\-.]+", " ", base)
    base = re.sub(r"\b(19|20)\d{2}\b|\b\d{4,}\b", "", base)  # years/numbers
    base = re.sub(r"\b(export|statement|transactions|download|history)\b",
                  "", base, flags=re.I)
    return (re.sub(r"\s+", " ", base).strip().title()
            or "Imported Account")[:60]


# The sign guess only ever reads the first rows of a file, so the sniff
# must never materialize more than that. Parsing a whole upload into a
# list of lists costs roughly thirty times its size in live Python
# objects — one 50 MB CSV of tiny rows is gigabytes — and on a shared
# instance the container's memory limit kills the process for every
# tenant before Python sees a MemoryError. Bound the BYTES before
# decoding (so a big file never becomes a big str either) and the ROWS
# while parsing, and the sniff costs the same for a 50 MB file as for a
# 5 KB one. The byte window holds far more than the row cap needs for
# any real bank export; a file with rows wide enough to fill it is not
# one the guess can say anything useful about anyway.
SNIFF_BYTES = 256 * 1024
SNIFF_ROWS = 200


def _csv_sign_guess(data: bytes) -> tuple[str, bool]:
    """(amount_sign, certain): mostly-negative amounts = bank convention
    (negative = money out); mostly positive = ambiguous → flag."""
    try:
        head = data[:SNIFF_BYTES]
        if len(data) > SNIFF_BYTES:
            # The window almost always cuts mid-row. Drop the torn tail so
            # a half-read cell ('-45.0' from '-45.07', or a quote opened
            # and never closed) cannot invent a value the file does not
            # contain — the numbers here decide the sign convention, so a
            # fabricated one is a wrong guess, not a rounding error. If the
            # window holds no line break at all, keep it: a single
            # enormous line yields no usable numbers either way.
            cut = head.rfind(b"\n")
            head = head[:cut + 1] if cut >= 0 else head
        text = head.decode("utf-8-sig", errors="replace")
        rows = list(itertools.islice(csvmod.reader(io.StringIO(text)),
                                     SNIFF_ROWS))
    except Exception:                             # noqa: BLE001
        return "bank", False
    nums: list[float] = []
    for row in rows[1:]:
        for cell in row:
            c = cell.strip().replace("$", "").replace(",", "")
            if re.fullmatch(r"-?\d+(\.\d{1,2})?", c) and "." in c:
                try:
                    nums.append(float(c))
                except ValueError:
                    pass
    if len(nums) < 3:
        return "bank", False
    neg = sum(1 for n in nums if n < 0) / len(nums)
    if neg >= 0.4:
        return "bank", True           # clear mix/negative body: bank style
    return "plaid", False             # all-positive: could be either — flag


def analyze(conn, files: list[tuple[str, bytes]]) -> dict:
    """Build the plan. `files` = (filename, data). Returns
    {token, files:[{index, name, kind, size, action, account_id,
                    new_account_name, amount_sign, sign_certain, note}]}."""
    if len(files) > MAX_FILES:
        raise ValueError(f"too many files (max {MAX_FILES})")
    # hidden accounts are never a destination: upsert_transactions refuses
    # their rows, so routing a file there reported "imported: 0" with no
    # reason
    accounts = conn.execute(
        """SELECT a.id, COALESCE(a.display_name, a.name) AS name, a.mask
           FROM accounts a WHERE a.user_removed_at IS NULL""").fetchall()
    plan = []
    for i, (name, data) in enumerate(files):
        ext = _ext(name)
        kind = KINDS.get(ext)
        if kind is None:
            plan.append({"index": i, "name": name, "kind": "unsupported",
                         "size": len(data), "action": "skip",
                         "account_id": None, "new_account_name": "",
                         "amount_sign": "bank", "sign_certain": True,
                         "note": f"unsupported file type '{ext}'"})
            continue
        # destination: filename last-4 ↔ account mask, else name overlap,
        # else a new account named from the file
        match = None
        for d in _digits(name):
            match = next((a for a in accounts if a["mask"] == d), None)
            if match:
                break
        if match is None:
            tokens = _name_tokens(name)
            match = next((a for a in accounts
                          if tokens & _name_tokens(a["name"] or "")), None)
        sign, certain = ("bank", True)
        if ext == ".csv":
            if re.search(
                    r"invest|broker|ira|401|roth|vanguard|fidelity|schwab|"
                    r"wealthfront|etrade|holdings|portfolio|retirement",
                    name, re.I):
                sign, certain = "investment", True
            else:
                sign, certain = _csv_sign_guess(data)
        elif ext == ".pdf":
            if re.search(
                    r"card|credit|visa|amex|mastercard|discover", name, re.I):
                sign, certain = "card", True
            elif re.search(
                    r"invest|broker|ira|401|roth|vanguard|fidelity|schwab|"
                    r"wealthfront|etrade|holdings|portfolio|retirement|"
                    r"summary", name, re.I):
                sign, certain = "investment", True
            elif re.search(r"statement|checking|savings|bank", name, re.I):
                sign, certain = "statement", True
            else:
                sign, certain = "statement", False
        entry = {"index": i, "name": name, "kind": kind, "size": len(data),
                 "action": "import", "amount_sign": sign,
                 "sign_certain": certain, "note": ""}
        if ext == ".zip":
            entry.update(account_id=None, new_account_name="",
                         note="full export restore — no account needed")
        elif match is not None:
            entry.update(account_id=match["id"], new_account_name="",
                         note=f"matches existing “{match['name']}”"
                              + (f" (…{match['mask']})" if match["mask"]
                                 else ""))
        else:
            entry.update(account_id=None,
                         new_account_name=_guess_account_name(name),
                         note="no existing account matched — creates a new one")
        plan.append(entry)
    token = staging.put(conn, "bulk", files=files)
    return {"token": token, "files": plan}


def _ensure_account(conn, name: str, amount_sign: str = "bank") -> str:
    """Create a NEW manual account, disambiguating the slug so two files
    named 'Chase' and 'chase!' — or a new file colliding with a
    pre-existing same-slug account for a DIFFERENT real-world account —
    never silently merge into one. Only reuse an existing account when the
    display name matches exactly (idempotent re-run).

    amount_sign tips type: investment → investment/brokerage, card →
    credit/credit card, else name-based inference.
    """
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "account"
    conn.execute(
        """INSERT INTO items (id, aggregator, institution_name, access_token)
           VALUES ('manual','manual','Manual',NULL)
           ON CONFLICT (tenant_id, id) DO NOTHING""")
    # the probe-then-insert below is a check-then-act: two imports naming
    # the same new account at once (web + phone, a double submit) both saw
    # no row and the second INSERT hit the primary key. Serialize the
    # slug decision per tenant for the length of this call.
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                     ("oikonome:ensure-account:" + str(conn.execute(
                         "SELECT current_setting('app.tenant_id', true) AS t"
                     ).fetchone()["t"]),))
        return _ensure_account_locked(conn, name, base, amount_sign)


def _ensure_account_locked(conn, name: str, base: str,
                           amount_sign: str) -> str:
    slug, n = base, 2
    while True:
        row = conn.execute("SELECT name FROM accounts WHERE id=%s",
                           (f"manual:{slug}",)).fetchone()
        if row is None:
            break
        if (row["name"] or "").strip() == name.strip():
            return f"manual:{slug}"      # same account, re-run — reuse
        slug, n = f"{base}-{n}", n + 1   # different account — new slug
    # Type from the name (shared inference): always depository/checking
    # would type card-named imports as cash and break the credit
    # sign/exclusion rules. The user can still reclassify.
    from ..sync.base import infer_account_type
    if amount_sign == "investment":
        type_, subtype = "investment", "brokerage"
    elif amount_sign == "card":
        type_, subtype = "credit", "credit card"
    else:
        type_, subtype = infer_account_type(name)
    conn.execute(
        """INSERT INTO accounts (id, item_id, name, type, subtype,
                                 balance_current, updated_at)
           VALUES (%s,'manual',%s,%s,%s,0,now())""",
        (f"manual:{slug}", name.strip(), type_, subtype))
    return f"manual:{slug}"


def run(conn, token: str, decisions: list[dict],
        role: str = "") -> list[dict]:
    """Execute the (edited) plan. Each decision: {index, action,
    account_id?, new_account_name?, amount_sign?}. Per-file isolation —
    one bad file never stops the rest.

    `role` decides only whether a `.zip` in the batch may restore; it is
    passed to dispatch_import for everything else. Unset means "not the
    owner", so a call site that forgets cannot restore by omission."""
    from .pages import dispatch_import
    from ..sync.base import tenant_sync_lock
    if staging.peek(conn, "bulk", token) is None:
        raise ValueError("this batch expired — re-analyze the files")
    # Take the tenant's sync lock BEFORE consuming the stage: each file's
    # dispatch takes it again (same session, re-entrant), and when the
    # hourly sync already held it every file answered "a sync is running"
    # after the staged bytes were gone — the person re-uploaded a batch the
    # server had just thrown away. Refusing first leaves the batch where
    # the token still points.
    with tenant_sync_lock(conn, str(conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
            ) as held:
        if not held:
            raise ValueError("a sync is already running — give it a moment "
                             "and run the batch again")
        batch = staging.pop(conn, "bulk", token)
        if batch is None:
            raise ValueError("this batch expired — re-analyze the files")
        return _run_batch(conn, batch, decisions, role, dispatch_import)


def _run_batch(conn, batch: dict, decisions: list[dict], role: str,
               dispatch_import) -> list[dict]:
    files = batch["files"]
    results = []
    total_rows = 0
    for d in decisions:
        # a decision that is not an object, or whose index is not a
        # number, is skipped — it used to escape the per-file try and
        # 500 the whole run
        if not isinstance(d, dict):
            continue
        try:
            i = int(d.get("index", -1))
        except (TypeError, ValueError):
            continue
        if not 0 <= i < len(files) or d.get("action") != "import":
            continue
        name, data = files[i]
        if total_rows >= MAX_TOTAL_ROWS:
            results.append({"index": i, "name": name, "ok": False,
                            "error": f"batch row budget reached "
                                     f"(max {MAX_TOTAL_ROWS:,} rows per "
                                     f"run) — run this file in a new "
                                     f"batch"})
            continue
        account_id = d.get("account_id")
        try:
            # an oversized file staged as empty bytes — never import
            # it even if the client re-checked the box; fail with a clear note
            if not data:
                results.append({"index": i, "name": name, "ok": False,
                                "error": "file was too large to import"})
                continue
            # a whole-ledger restore runs as a background job — it is the
            # one import that regularly outlives the edge timeout in front
            # of a hosted instance (see sync/restore_job). The plan row
            # reports it as started; the page polls it to completion.
            # Its rows no longer count toward this run's row budget (they
            # are not parsed here any more); restore_zip carries its own
            # per-member and archive-size caps, which is the bound that
            # matters for an archive.
            if name.lower().endswith(".zip"):
                from . import permissions
                if not permissions.may_restore(role):
                    results.append({"index": i, "name": name, "ok": False,
                                    "error": permissions.RESTORE_DENIED})
                    continue
                from ..sync import restore_job
                tid = conn.execute(
                    "SELECT current_setting('app.tenant_id', true) AS t"
                ).fetchone()["t"]
                started = restore_job.start(str(tid), data)
                if started.get("error"):
                    results.append({"index": i, "name": name, "ok": False,
                                    "error": started["error"]})
                else:
                    results.append({"index": i, "name": name, "ok": True,
                                    "imported": 0, "restore_started": True,
                                    "account_id": None})
                continue
            if not account_id:
                new_name = str(d.get("new_account_name") or "").strip()
                if not new_name:
                    raise ValueError("needs an account")
                account_id = _ensure_account(
                    conn, new_name, str(d.get("amount_sign") or "bank"))
            result, mapping = dispatch_import(
                conn, account_id or "", str(d.get("amount_sign") or "bank"),
                name, data, role=role)
            if mapping is not None:
                results.append({"index": i, "name": name, "ok": False,
                                "error": "CSV columns not recognized — "
                                         "import it alone to map them"})
            elif result.get("error"):
                results.append({"index": i, "name": name, "ok": False,
                                "error": result["error"]})
            else:
                total_rows += (result.get("rows") or 0) + \
                    (result.get("skipped_duplicates") or 0)
                results.append({"index": i, "name": name, "ok": True,
                                "imported": result.get("imported", 0),
                                "account_id": account_id})
        except Exception as e:                    # noqa: BLE001
            results.append({"index": i, "name": name, "ok": False,
                            "error": f"{type(e).__name__}: {e}"})
    return results
