"""Bulk historical import: drop a whole folder of mixed bank
files, get a per-file PLAN (detected type, destination account
recommendation, amount-sign guess with an uncertainty flag), edit it,
then submit it to run in the background (jobs/bulk_import_job).

Staged uploads live in the tenant-bound `import_staging` table (an
in-memory dict breaks under multiple workers and evicts batches
silently); nothing touches the ledger until the plan is submitted, and
each file imports through the SAME dispatch the single-file page uses —
all its dedup guards and undo batches apply."""

from __future__ import annotations

import csv as csvmod
import io
import itertools
import logging
import re

import psycopg

from ..db import staging
from ..engine.compat import jsonb

MAX_FILES = 200
# Aggregate row budget for one folder import. Each file already carries
# the per-file row cap (sync.rowcap, 50k) and its own dedup scan, but a
# cap per file times MAX_FILES files would let one job process 200 × 50k
# rows. The budget bounds the whole batch; files past it are refused with
# a clear note and can be run again in a second batch.
MAX_TOTAL_ROWS = 200_000
# Aggregate bytes one folder analyze may carry. The folder arrives as ONE
# multipart body, held to security.UPLOAD_BODY_LIMIT at the edge, and the
# handler then holds every file in memory while it stages them — so the
# batch budget is the body ceiling less the multipart framing, not the
# larger staging budget, which a single request could never reach. It
# stays at or above the per-file cap so any one file still fits a batch.
MAX_BATCH_BYTES = 60 * 1024 * 1024
BATCH_TOO_LARGE = (f"these files total more than "
                   f"{MAX_BATCH_BYTES // (1024 * 1024)} MB — import the "
                   f"folder in smaller batches")

# Where a whole-ledger export ZIP is restored on its own: that door streams
# the archive to disk under the restore cap, while a folder analyze holds
# every file in memory under the ordinary upload cap. The analyze answers
# name it (`restore_door`) so a client can send the archive there.
RESTORE_DOOR = "/api/import"

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
    """Last-4 candidates from the FILENAME (mask matching).

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


# A header cell naming the money: the row that carries one is the column
# header, whatever summary lines a bank prints above it.
_AMOUNT_HEADER = re.compile(
    r"\b(amount|debit|credit|withdrawals?|deposits?)\b", re.I)
# Columns whose numbers are not the transaction's own money.
_NOT_AN_AMOUNT = re.compile(r"\b(balance|bal|total)\b", re.I)


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
        # decoded the way the import itself will read the file, so a
        # UTF-16 export is sniffed as text, not as NUL-riddled noise
        from .pages import upload_text
        text = upload_text(head)
        rows = list(itertools.islice(csvmod.reader(io.StringIO(text)),
                                     SNIFF_ROWS))
    except Exception:                             # noqa: BLE001
        return "bank", False
    # The convention is a property of the AMOUNT column, so only that column
    # is counted. A running balance is positive on nearly every row and a
    # summary block above the header carries totals; pooled with the
    # amounts they halve the negative share, and a checking export that is
    # 70% debits reads as all-positive and has every sign flipped.
    header_at = next(
        (i for i, row in enumerate(rows)
         if any(_AMOUNT_HEADER.search(c or "") for c in row)), 0)
    header = [(c or "").strip() for c in rows[header_at]] if rows else []
    amount_cols = [i for i, h in enumerate(header)
                   if re.search(r"\bamount\b", h, re.I)
                   and not _NOT_AN_AMOUNT.search(h)]
    skip = {i for i, h in enumerate(header) if _NOT_AN_AMOUNT.search(h)}
    nums: list[float] = []
    for row in rows[header_at + 1:]:
        for j, cell in enumerate(row):
            if (amount_cols and j not in amount_cols) or j in skip:
                continue
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
    # Hidden accounts are never a destination: upsert_transactions refuses
    # their rows, so a file routed there would import nothing.
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
        if ext == ".csv":
            n = _ynab_accounts(data)
            if n > 1:
                # the run refuses it (the dispatcher asks only a client
                # that can show the choice); saying so here lets the person
                # take it out of the folder before running
                entry["note"] = (f"covers {n} YNAB accounts; import it on "
                                 f"its own to choose where each goes")
        plan.append(entry)
    token = staging.put(conn, "bulk", files=files)
    return {"token": token, "files": plan}


def _ynab_accounts(data: bytes) -> int:
    """How many YNAB accounts a YNAB register names; 0 for any other file
    (or one that does not parse — the run reports that itself)."""
    from ..sync import ynabimport
    from .pages import upload_text
    try:
        text = upload_text(data)
        header = next(csvmod.reader(io.StringIO(text)), [])
        if not ynabimport.looks_like_ynab(header):
            return 0
        return len(ynabimport.account_names(ynabimport.parse(text)))
    except Exception:                                    # noqa: BLE001
        return 0


def hold_ledger_write_locks(conn) -> None:
    """Take, for the rest of the caller's transaction, the two household
    locks a file import meets on its way through upsert_transactions — the
    reimbursement-pairing lock (the waiting-receipt matcher) and the
    merchant-identity lock (the resolver) — BEFORE any account or
    transaction row is written.

    Every other holder takes them before its rows: an edit takes the
    pairing lock and then the row it changes (reanchor.hold_pairing_lock),
    a rename takes the identity lock and then its rows (merchant_dedup).
    An import that wrote its rows first and asked for those locks last
    would hold a row the edit wants while waiting for the lock the edit
    holds, and Postgres breaks that cycle by killing one side: the edit
    answers 500, or the import's matcher and resolver quietly drop their
    work for the whole file. Pairing before identity, because nothing
    holds the identity lock and then asks for the pairing one.

    Callers hold the sync lock and then the push writer lock
    (erasure.WRITER_LOCKS), then this, then rows — the order written down
    beside erasure.WRITER_LOCKS."""
    from ..engine import merchant_identity
    from ..sync.reanchor import hold_pairing_lock
    hold_pairing_lock(conn)
    merchant_identity._lock_identity(conn)


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
    # The probe-then-insert below is a check-then-act: two imports naming
    # the same new account at once (web + phone, a double submit) would both
    # see no row and the second INSERT would hit the primary key. Serialize
    # the slug decision per tenant for the length of this call.
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
        row = conn.execute(
            "SELECT name, user_removed_at FROM accounts WHERE id=%s",
            (f"manual:{slug}",)).fetchone()
        if row is None:
            break
        # a HIDDEN account of the same name is not a destination: the
        # upsert refuses its rows, so the import would land nothing and
        # say "0 transactions". The new account takes the next slug.
        if ((row["name"] or "").strip() == name.strip()
                and row["user_removed_at"] is None):
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


def _index(d) -> int | None:
    """The staged file a decision names, or None for a decision that is
    not an object or whose index is not a whole number."""
    if not isinstance(d, dict):
        return None
    try:
        i = int(d.get("index", -1))
    except (TypeError, ValueError, OverflowError):
        return None
    return i if i >= 0 else None


def _staged(conn, token: str) -> dict[int, dict]:
    """The bulk stage's files keyed by their own staging index — names and
    sizes, never bytes. Keyed, not listed: read as a list by position, a
    stage with any row missing shifts every later file down one, and a
    decision naming the archive then reads as naming nothing."""
    return {r["idx"]: r for r in conn.execute(
        """SELECT idx, filename, octet_length(data) AS n FROM import_staging
            WHERE token = %s AND kind = 'bulk'""", (token,)).fetchall()}


def _is_archive(row) -> bool:
    return (row["filename"] or "").lower().endswith(".zip")


def restores_archive(conn, token: str, decisions: list[dict],
                     role: str = "") -> bool:
    """Would this plan start a whole-ledger restore? True when a decision
    imports a non-empty `.zip` from the stage and the role may restore (a
    role that may not gets a refused row, not a restore). Reads names and
    sizes only, and consumes nothing."""
    from . import permissions
    if not permissions.may_restore(role):
        return False
    staged = _staged(conn, token)
    for d in decisions:
        i = _index(d)
        if i is None or d.get("action") != "import":
            continue
        row = staged.get(i)
        if row is not None and (row["n"] or 0) > 0 and _is_archive(row):
            return True
    return False


# How long the run door waits for a job before answering a client that
# predates the job (one that does not ask for `background`): long enough
# for a small folder to finish inside the request, as it always did, and
# well under the edge timeout in front of a hosted instance. Only the post
# that creates the job waits; a repost of its token reads it back at once.
COMPAT_WAIT_SECONDS = 25.0
COMPAT_POLL_SECONDS = 0.5

BUSY = ("another folder import is still running on this account — wait "
        "for it to finish, then run this one")


class Busy(ValueError):
    """The household already has a live folder import."""


def _frozen(decisions: list, staged: dict[int, dict]
            ) -> tuple[list[dict], list[int]]:
    """The plan as the job will run it: (one entry per staged file to
    import, in upload order; the archives the plan imports). Each staged
    file at most once — a repeated index is dropped, as is anything that
    is not an import of a staged file. Only the four fields the import
    reads survive, each coerced to its type."""
    chosen: dict[int, dict] = {}
    for d in decisions:
        i = _index(d)
        if i is None or i not in staged or i in chosen \
                or d.get("action") != "import":
            continue
        acct = d.get("account_id")
        new = d.get("new_account_name")
        sign = d.get("amount_sign")
        chosen[i] = {
            "file_id": i,
            "account_id": acct if isinstance(acct, str) and acct else None,
            "new_account_name": (new.strip()[:120]
                                 if isinstance(new, str) else ""),
            "amount_sign": (sign[:32] if isinstance(sign, str) and sign
                            else "bank")}
    files = [chosen[i] for i in sorted(chosen)
             if not _is_archive(staged[i])]
    archives = [i for i in sorted(chosen) if _is_archive(staged[i])]
    return files, archives


def submit(conn, token: str, decisions: list, user: dict, *,
           step_up=None) -> dict:
    """Freeze an analyzed folder's plan as a background job and return its
    status. The one door through which a plan is decided.

    A second post of the same token answers the job the first one made,
    whatever plan it carries: the decisions — which files, which accounts,
    whether an archive restores — are made once, here, and never re-read.
    An export ZIP the plan imports is restored by THIS request, after
    `step_up()` (the caller's step-up check, which raises to refuse) and
    before anything is consumed; its bytes never enter the job, which runs
    later with no request to ask again. The staged bytes of every other
    file move into the job in the same transaction that spends the stage.

    Raises ValueError for a malformed plan or a spent stage, Busy while
    another folder import runs, PermissionError for a script token or for
    an archive with no `step_up` to ask."""
    from ..jobs import bulk_import_job as jobs
    from . import permissions, security
    if not isinstance(decisions, list):
        raise ValueError("files must be a list")
    # A plan has one decision per staged file, and no batch holds more
    # than MAX_FILES. A longer plan is refused before anything is consumed.
    if len(decisions) > MAX_FILES:
        raise ValueError(f"too many files (max {MAX_FILES})")
    if user.get("script_token"):
        # the auth gate already keeps script tokens off this door; a job
        # must have a signed-in person to answer to
        raise PermissionError("a folder import needs a signed-in session")
    token = str(token or "")
    existing = jobs.find_by_token(conn, token)
    if existing:
        return dict(jobs.status(conn, existing) or {}, existing=True)
    staged = _staged(conn, token)
    if not staged:
        raise ValueError("this batch expired — re-analyze the files")
    files, archives = _frozen(decisions, staged)
    role = str(user.get("role") or "")
    notes: list[dict] = []
    restore: list[int] = []
    for i in archives:
        name = staged[i]["filename"] or ""
        if not (staged[i]["n"] or 0):
            notes.append({"index": i, "name": name, "ok": False,
                          "error": "file was too large to import"})
        elif not permissions.may_restore(role):
            notes.append({"index": i, "name": name, "ok": False,
                          "error": permissions.RESTORE_DENIED})
        else:
            restore.append(i)
    if restore:
        # a call site that does not say how it stepped up does not get to
        # restore: the failure that matters is a new caller inheriting the
        # most privileged answer by omission
        if step_up is None:
            raise PermissionError("restoring an export needs a step-up")
        step_up()
    tid = str(conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
    archive_bytes: dict[int, bytes] = {}
    try:
        with conn.transaction():
            job_id = str(conn.execute(
                """INSERT INTO bulk_import_jobs
                       (stage_token_hash, state, starter_user_id,
                        starter_cred, starter_via, decisions, notes, total,
                        finished_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                           CASE WHEN %s THEN now() END)
                   RETURNING id""",
                (jobs.token_hash(token), "queued" if files else "done",
                 user.get("user_id") or None,
                 security.current_credential() or None, "app",
                 jsonb({"files": files}), jsonb(notes), len(files),
                 not files)).fetchone()["id"])
            if restore:
                archive_bytes = {r["idx"]: bytes(r["data"] or b"")
                                 for r in conn.execute(
                    """SELECT idx, data FROM import_staging
                        WHERE token = %s AND kind = 'bulk'
                          AND idx = ANY(%s)""", (token, restore))}
            # moved, never copied: the bytes stay inside the database
            conn.execute(
                """INSERT INTO bulk_import_files
                       (job_id, file_id, filename, data, size)
                   SELECT %s, idx, filename, data,
                          coalesce(octet_length(data), 0)
                     FROM import_staging
                    WHERE token = %s AND kind = 'bulk' AND idx = ANY(%s)""",
                (job_id, token, [f["file_id"] for f in files]))
            if not conn.execute(
                    "DELETE FROM import_staging WHERE token = %s"
                    " AND kind = 'bulk'", (token,)).rowcount:
                # spent by a request that raced this one
                raise _Spent
    except (_Spent, psycopg.errors.UniqueViolation):
        # the same token submitted twice at once answers the one job; any
        # other clash is the household's one-live-job rule
        existing = jobs.find_by_token(conn, token)
        if existing:
            return dict(jobs.status(conn, existing) or {}, existing=True)
        if staged and not _staged(conn, token):
            raise ValueError("this batch expired — re-analyze the files")
        raise Busy(BUSY)
    if restore:
        notes = notes + _start_restores(tid, restore, staged, archive_bytes)
        conn.execute("UPDATE bulk_import_jobs SET notes = %s WHERE id = %s",
                     (jsonb(notes), job_id))
    if files:
        jobs.nudge(tid, job_id)
    return jobs.status(conn, job_id) or {"job_id": job_id, "state": "queued"}


class _Spent(Exception):
    pass


def _start_restores(tid: str, restore: list[int], staged: dict,
                    archive_bytes: dict[int, bytes]) -> list[dict]:
    """Start each archive's restore from the submitting request, whose
    credential the restore records as its starter; one result row each."""
    from ..sync import restore_job
    rows = []
    for i in restore:
        row = {"index": i, "name": staged[i]["filename"] or "", "ok": False}
        try:
            started = restore_job.start(tid, archive_bytes.get(i) or b"")
        except Exception as e:                    # noqa: BLE001
            logging.getLogger("oikonome.import").exception(
                "bulk import could not start a restore")
            row["error"] = (f"import failed ({type(e).__name__}) — "
                            f"upload this file again to retry")
        else:
            if started.get("error"):
                row["error"] = started["error"]
            else:
                row.update(ok=True, imported=0, restore_started=True)
        rows.append(row)
    return rows


def wait_for(tenant_id: str, job_id: str, seconds: float) -> dict | None:
    """The job's status once it settles, or after `seconds` — for the
    clients that read the run door's `results` and nothing else.

    Each poll borrows a tenant connection and hands it straight back:
    the tenant connections are one small pool shared by every household
    on the instance, and a wait that kept one for its whole length let a
    few such requests leave everyone else's queueing until they failed."""
    import time

    from ..db import tenancy
    from ..jobs import bulk_import_job as jobs
    deadline = time.monotonic() + max(0.0, seconds)
    st = None
    while True:
        try:
            conn = tenancy.tenant_connect(tenant_id)
        except Exception:
            # the pool is spoken for right now: the job exists and runs
            # on its own, so the door answers with what it last saw
            return st
        try:
            st = jobs.status(conn, job_id)
        finally:
            conn.close()
        if st is None or st["state"] not in jobs.LIVE \
                or time.monotonic() >= deadline:
            return st
        time.sleep(COMPAT_POLL_SECONDS)
