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
    # a long digit run is an account or card number wherever it sits —
    # "Checking1234567890123" has no word boundary to catch it below; the
    # numbers a file name spells are added back whole by _new_account_name,
    # which the stored name cuts
    from ..sync.card_number import LONG_RUN_RE
    base = LONG_RUN_RE.sub(" ", base)
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
        from ..sync import csvimport
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
            # a money cell carries cents after either separator; a
            # decimal-comma export ("-12,50") counts the same as "-12.50",
            # and an id or a whole count carries none and is not a vote
            c = cell.strip()
            if re.fullmatch(r"[-+]?\$?-?[\d.,]*\d[.,]\d{1,2}", c):
                try:
                    nums.append(csvimport.parse_money_text(c))
                except ValueError:
                    pass
    if len(nums) < 3:
        return "bank", False
    neg = sum(1 for n in nums if n < 0) / len(nums)
    if neg >= 0.4:
        return "bank", True           # clear mix/negative body: bank style
    return "plaid", False             # all-positive: could be either — flag


# Filename words that mark an investment export. Brand and wrapper names
# long enough to be unambiguous match anywhere, so a CamelCase export like
# "VanguardIRA.csv" or "SchwabBrokerage.csv" is still recognised. The short
# tokens are guarded, because a bare substring match typed ordinary bank
# files as investments: "ira" sits inside ordinary words like admiral or
# spiral, "roth" inside Brothers, and "401" inside any date stamp from
# January 2024 or an April 1st. So "ira" and "roth" count only as a word of
# their own or as a capitalised CamelCase part ("RothIRA", "VanguardRoth"),
# and a retirement plan number only with its letter (401k, 401(k), 403b)
# and never as part of a longer digit run.
_INVESTMENT_NAME_RE = re.compile(
    r"(?i:invest|broker|vanguard|fidelity|schwab|wealthfront|etrade|"
    r"holdings|portfolio|retirement)"
    r"|(?<![A-Za-z])(?i:ira)(?![a-z])|(?<=[a-z])IRA(?![a-z])"
    r"|(?<![A-Za-z])(?i:roth)(?![a-z])|(?<=[a-z])Roth(?![a-z])"
    r"|(?<![A-Z])ROTH(?![A-Z])|(?<![a-z])Roth(?=[A-Z])"
    r"|(?<!\d)(?i:401\s*\(?k|(?:403|457)\s*\(?b)")


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
    from ..sync import account_sources as srcs
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
        # destination: the account a filename spelling a full number
        # already has, else filename last-4 ↔ account mask, else name
        # overlap, else a new account named from the file
        match, choices = _source_match(conn, name)
        # an account already answering to another full number is another
        # card, however alike the names
        other_cards = (set() if match or choices else
                       srcs.claimed_accounts(conn, srcs.DOOR_MANUAL,
                                             _stem(name)))
        open_ = [] if match or choices else [
            a for a in accounts if a["id"] not in other_cards]
        for d in _digits(name) if open_ else []:
            match = next((a for a in open_ if a["mask"] == d), None)
            if match:
                break
        if match is None and not choices:
            tokens = _name_tokens(name)
            match = next((a for a in open_
                          if tokens & _name_tokens(a["name"] or "")), None)
        sign, certain = ("bank", True)
        if ext == ".csv":
            if _INVESTMENT_NAME_RE.search(name):
                sign, certain = "investment", True
            else:
                sign, certain = _csv_sign_guess(data)
        elif ext == ".pdf":
            if re.search(
                    r"card|credit|visa|amex|mastercard|discover", name, re.I):
                sign, certain = "card", True
            elif (_INVESTMENT_NAME_RE.search(name)
                  or re.search(r"(?<![a-z])summary", name, re.I)):
                sign, certain = "investment", True
            elif re.search(r"statement|checking|savings|bank", name, re.I):
                sign, certain = "statement", True
            else:
                sign, certain = "statement", False
        entry = {"index": i, "name": name, "kind": kind, "size": len(data),
                 "action": "import", "amount_sign": sign,
                 "sign_certain": certain, "note": ""}
        if ext == ".zip":
            from ..sync.restore import foreign_archive_refusal
            refusal = foreign_archive_refusal(data)
            entry.update(account_id=None, new_account_name="",
                         note=refusal or "full export restore — no account "
                                         "needed")
            if refusal:
                entry.update(kind="unsupported", action="skip")
        elif choices:
            # the person picks one before running: the job has nobody to ask
            entry.update(account_id=None, new_account_name="",
                         choices=choices,
                         note=_choice_message(stored_name(_stem(name)),
                                              len(choices)))
        elif match is not None:
            entry.update(account_id=match["id"], new_account_name="",
                         note=f"matches existing “{match['name']}”"
                              + (f" (…{match['mask']})" if match["mask"]
                                 else ""))
        else:
            entry.update(account_id=None,
                         new_account_name=_new_account_name(name),
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


def _stem(filename: str) -> str:
    return re.sub(r"\.[A-Za-z]+$", "", filename.rsplit("/", 1)[-1]).strip()


def _source_match(conn, filename: str) -> tuple[dict | None, list[dict]]:
    """(the account a file's name is recorded as filling, []), or (None,
    the accounts a filename spelling a full number could mean) when it
    cannot say which, or (None, []) when the ordinary matching places it.
    The recorded source is the file's stem or the name a new account
    would be given from it (account_sources), so a renamed account still
    receives its file. Reads only."""
    from ..sync import account_sources as srcs
    from ..sync import card_number
    full = _stem(filename)[:NEW_ACCOUNT_NAME_MAX]
    if not full:
        return None, []
    for typed in dict.fromkeys((full, _new_account_name(filename))):
        found = srcs.lookup(conn, srcs.DOOR_MANUAL, typed)
        if found:
            return (_describe(conn, [found]) or [None])[0], []
    if not card_number.holds_number(full):
        return None, []
    ids, shared, _ = _candidates(conn, full, stored_name(full))
    if len(ids) == 1 and not shared:
        return _describe(conn, ids)[0], []
    return None, (_describe(conn, ids) if ids else [])


def _new_account_name(filename: str) -> str:
    """The name a new account is offered for a file: words from the file
    name, and every full number it spells, which the stored name cuts to
    its last four and account_sources keys the account by — so two cards
    sharing their last four are two accounts, not one called "Visa"."""
    from ..sync import card_number
    nums = card_number.find(_stem(filename))
    guess = _guess_account_name(filename)
    return (" ".join([guess, *nums]) if nums else guess
            )[:NEW_ACCOUNT_NAME_MAX]


def _ynab_accounts(data: bytes) -> int:
    """How many YNAB accounts a YNAB register names; 0 for any other file
    (or one that does not parse — the run reports that itself)."""
    from ..sync import csvimport, ynabimport
    from .pages import upload_text
    try:
        text = upload_text(data)
        header = csvimport.read_header(text)
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


def hold_restore_write_locks(conn) -> None:
    """The locks a full-export restore takes, for the rest of its one
    transaction, before its first row: the household's file-storage lock,
    then the two hold_ledger_write_locks takes.

    A restore reserves room for every receipt it brings (the storage lock)
    and later re-keys accounts and re-ids rows under the pairing lock and
    names merchants under the identity lock; a receipt snapped meanwhile
    takes storage, then pairing, then its row (engine.receipts.add). Asked
    for after the restore's rows, either lock closes a cycle with that
    snap or with any edit holding the pairing lock while it waits on a
    restored row, and Postgres kills the restore. Storage is only ever
    locked when a quota is set (storage_quota.reserve), so without one
    nothing waits on it here either. The key is the one reserve takes."""
    from ..engine import storage_quota
    if storage_quota.quota_bytes() is not None:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext("
            "'oikonome:storage:' || current_setting('app.tenant_id')))")
    hold_ledger_write_locks(conn)


NEW_ACCOUNT_NAME_MAX = 120


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def stored_name(name: str) -> str:
    """The name a new hand-made account stores for `name`: every full
    number cut to its last four (sync.card_number). A name whose slug
    still spells a number after that — a separator the grammar does not
    take ("4000_0000_0000_0002") — is cut the way the slug reads it
    (card_number.cut_as_slug_reads), as the ids every earlier build
    derived from it were moved, so the name keeps no more digits than
    those ids."""
    from ..sync import card_number
    from ..sync.base import NUMBERED_MANUAL_ID_RE
    shown = card_number.cut(name)
    if NUMBERED_MANUAL_ID_RE.search(f"manual:{_slug(shown)}"):
        shown = card_number.cut_as_slug_reads(shown)
    return shown


def _legacy_slug(typed: str) -> str:
    """The slug a build before opaque ids gave a hand-made account named
    `typed`: the slug of the cut name, moved off any number it still
    spelled the way a re-key moves it. Only for finding such accounts."""
    from ..sync import card_number
    from ..sync.base import NUMBERED_MANUAL_ID_RE, renumbered_manual_id
    slug = _slug(card_number.cut(typed)) or "account"
    if NUMBERED_MANUAL_ID_RE.search(f"manual:{slug}"):
        slug = renumbered_manual_id(f"manual:{slug}").split(":", 1)[1]
    return slug


def _lineage_names(full: str, shown: str) -> set[str]:
    """Every stored name an account made for `full` may carry: the full
    one an early build made it with, the cut one a door stores now, and
    what each stored-name scrub (unbroken runs, then grouped ones) left."""
    from ..sync import card_number
    return {full.strip(), shown.strip(), card_number.cut_for_hash(full).strip(),
            card_number.cut(full).strip()}


def _slug_walk(conn, base: str, names: set[str]) -> list[str]:
    """The live accounts a build before opaque ids made under `base` or a
    "-N" beside it whose stored name is one of `names`, walked as those
    builds handed the suffixes out."""
    out, slug, n = [], base, 2
    while True:
        row = conn.execute(
            "SELECT name, user_removed_at FROM accounts WHERE id=%s",
            (f"manual:{slug}",)).fetchone()
        if row is None:
            return out
        if (row["user_removed_at"] is None
                and (row["name"] or "").strip() in names):
            out.append(f"manual:{slug}")
        slug, n = f"{base}-{n}", n + 1


def _candidates(conn, typed: str, shown: str
                ) -> tuple[list[str], bool, list[str]]:
    """(the live hand-made accounts a source with no account_sources row
    of its own could mean, best first; whether nothing can say which of
    them it is; the accounts the name leads to that already answer to
    another card, which only the file's own rows can claim for it —
    import_named). Reads only.

    By the digest of the stored name each account was made with (the
    c-rows, backfilled for every account made before them); then, for an
    account made before opaque ids, the slug walk those builds made; then
    the accounts a re-key moved a full-number id to — the one whose stored
    name is this name when every record leads to one account, every one
    when a record leads to several (two cards sharing their last four left
    one cut record, which cannot tell them apart whichever is hidden). A
    source spelling a full number never means an account that already
    answers to a different number: two cards sharing their last four share
    a cut name and nothing else."""
    from ..sync import account_sources as srcs
    from ..sync.base import cut_lineage_candidates
    names = _lineage_names(typed, shown)
    found = srcs.by_name(conn, names)
    for base in dict.fromkeys((_legacy_slug(typed), _slug(typed))):
        if base:
            found += _slug_walk(conn, base, names)
    shared = False
    if typed != shown and _slug(typed):
        records = cut_lineage_candidates(conn, f"manual:{_slug(typed)}")
        targets = list(dict.fromkeys(t for _, ts in records for t in ts))
        rows = conn.execute(
            "SELECT id, name FROM accounts WHERE id = ANY(%s)"
            "   AND user_removed_at IS NULL ORDER BY id",
            (targets,)).fetchall()
        shared = any(len(ts) > 1 for _, ts in records)
        named = [r["id"] for r in rows if (r["name"] or "").strip() in names]
        found += ([r["id"] for r in rows] if shared
                  else named or [r["id"] for r in rows])
    live = {r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE id = ANY(%s) AND item_id = 'manual'"
        "   AND user_removed_at IS NULL", (list(set(found)),)).fetchall()}
    other_cards = srcs.claimed_accounts(conn, srcs.DOOR_MANUAL, typed)
    lineage = [a for a in dict.fromkeys(found) if a in live]
    return ([a for a in lineage if a not in other_cards], shared,
            [a for a in lineage if a in other_cards])


def _manual_item(conn) -> None:
    conn.execute(
        """INSERT INTO items (id, aggregator, institution_name, access_token)
           VALUES ('manual','manual','Manual',NULL)
           ON CONFLICT (tenant_id, id) DO NOTHING""")


def _insert_manual(conn, typed: str, type_: str, subtype: str | None,
                   balance: float = 0.0, *, claim: bool) -> str:
    """Mint and insert a hand-made account for the typed name `typed`, with
    its source and stored-name rows. `claim`: the typed name's source row
    moves to this account even when another holds it (an import making a
    new account beside a hidden one); otherwise an existing one stays."""
    from ..sync import account_sources as srcs
    name = stored_name(typed)
    aid = srcs.mint_id(conn)
    # balance defaults to 0, never NULL: a NULL balance files the brand-
    # new account under archived/closed and the cash runway screams
    # 'out of money' on day one
    conn.execute(
        """INSERT INTO accounts (id, item_id, name, type, subtype,
                                 balance_current, updated_at)
           VALUES (%s,'manual',%s,%s,%s,%s,now())""",
        (aid, name.strip(), type_, subtype, balance))
    srcs.remember(conn, srcs.DOOR_MANUAL, typed, aid, replace=claim)
    srcs.remember_name(conn, aid, name)
    return aid


def create_manual_account(conn, name: str, type_: str,
                          subtype: str | None, balance: float = 0.0) -> str:
    """A NEW hand-made account, as the account-add doors make one: always
    a new account (two typed "Chase" are two accounts), under an opaque id,
    with the typed name's source row unless an earlier account holds it."""
    from ..sync import account_sources as srcs
    typed = name.strip()[:NEW_ACCOUNT_NAME_MAX]
    _manual_item(conn)
    with conn.transaction():
        srcs.hold_lock(conn)
        return _insert_manual(conn, typed, type_, subtype, balance,
                              claim=False)


def _ensure_account(conn, name: str, amount_sign: str = "bank") -> str:
    """The hand-made account a new-account name fills: the one its source
    row names (account_sources), else the one account made before that
    row existed which the name leads to (_candidates) — remembered, so it
    is found by its row from then on — else a NEW account under an opaque
    id. A name spelling a full number that leads to several accounts (two
    cards sharing their last four) raises AccountChoiceNeeded instead of
    landing on either; the choice the person makes is remembered.

    amount_sign tips type: investment → investment/brokerage, card →
    credit/credit card, else name-based inference."""
    from ..sync import account_sources as srcs
    from ..sync import card_number
    typed = name.strip()[:NEW_ACCOUNT_NAME_MAX]
    shown = stored_name(typed)
    _manual_item(conn)
    with conn.transaction():
        # check-then-act: two imports naming the same new account at once
        # (web + phone, a double submit) make one account
        srcs.hold_lock(conn)
        found = srcs.lookup(conn, srcs.DOOR_MANUAL, typed)
        if found:
            return found
        cands, shared, _ = _candidates(conn, typed, shown)
        if cands and (len(cands) > 1 or shared) \
                and card_number.holds_number(typed):
            raise AccountChoiceNeeded(_choice_message(shown, len(cands)),
                                      cands)
        if cands:
            srcs.remember(conn, srcs.DOOR_MANUAL, typed, cands[0])
            return cands[0]
        # Type from the name (shared inference): always depository/checking
        # would type card-named imports as cash and break the credit
        # sign/exclusion rules. The user can still reclassify.
        from ..sync.base import infer_account_type
        if amount_sign == "investment":
            type_, subtype = "investment", "brokerage"
        elif amount_sign == "card":
            type_, subtype = "credit", "credit card"
        else:
            type_, subtype = infer_account_type(shown)
        return _insert_manual(conn, typed, type_, subtype, claim=True)


def import_named(conn, name: str, amount_sign: str, run):
    """Import a file into the hand-made account the new-account name
    `name` fills: (that account's id, what `run(account_id)` — the
    import itself — returned).

    _ensure_account decides from the name alone, and a name spelling a
    full number never lands on an account that already answers to another
    card. That account may still hold THIS card's rows: a build before
    opaque ids filed two cards sharing their last four on one account,
    each card's rows hashed under its id. A fresh account would hash them
    under a fresh id, and the re-import would insert the card's whole
    history a second time. So when the name leads to such an account and
    the decision made a fresh one, the import runs there in a savepoint,
    its rows are recomputed under each such account's id
    (sync.base._holds_file_rows), and when one holds any of them the
    savepoint is rolled back — the fresh account with it — and the file
    lands there instead, remembered for the next statement. The file is
    parsed twice only on that one re-import.

    A file that cannot be imported yet — a CSV whose columns the person
    must map, a YNAB register whose accounts they must place — has no rows
    to probe, so a new account is not made for it: the account's creation
    is rolled back and the file is parked under the typed name and sign
    (_park_by_name), and the mapped submit comes back here with the rows.
    The id is then None."""
    from ..sync import account_sources as srcs
    typed = name.strip()[:NEW_ACCOUNT_NAME_MAX]
    if srcs.lookup(conn, srcs.DOOR_MANUAL, typed):
        aid = _ensure_account(conn, name, amount_sign)
        return aid, run(aid)
    cands, _, held = _candidates(conn, typed, stored_name(typed))
    parked = None
    with conn.transaction() as trial:
        aid = _ensure_account(conn, name, amount_sign)
        fresh = aid not in held and conn.execute(
            "SELECT 1 FROM transactions WHERE account_id = %s LIMIT 1",
            (aid,)).fetchone() is None
        result = run(aid)
        if result[1] is not None and aid not in cands:
            parked = _parked(conn, result[1])
            raise psycopg.Rollback(trial)
        earlier = _holding_this_file(conn, aid, typed, held) \
            if fresh and held else None
        if earlier is None:
            return aid, result
        raise psycopg.Rollback(trial)
    if parked is not None:
        return None, _park_by_name(conn, parked, typed, amount_sign)
    with conn.transaction():
        srcs.hold_lock(conn)
        srcs.remember(conn, srcs.DOOR_MANUAL, typed, earlier)
    return earlier, run(earlier)


def _parked(conn, need: dict) -> tuple[str, dict | None, dict]:
    """The upload an import parked to ask the person something (`need`,
    what the dispatcher answered), read before the savepoint that parked
    it is rolled back: (stage kind, the stage, `need`)."""
    kind = "ynab" if need.get("kind") == "ynab_accounts" else "csv"
    return kind, staging.peek(conn, kind, str(need.get("token") or "")), need


def _park_by_name(conn, parked, typed: str, amount_sign: str):
    """Park the upload again with no account: a CSV under the typed
    new-account name and sign, which the mapped submit resolves through
    import_named once its rows can be read; a YNAB register with no
    picked account, each of its accounts placed by the person. Returns
    what the dispatcher returns: (result, what to ask)."""
    kind, stash, need = parked
    if stash is None:
        return {"error": "upload expired — please upload the file "
                         "again"}, None
    meta = dict(stash["meta"] or {}, account_id=None)
    if kind == "csv":
        meta.update(new_account_name=typed, amount_sign=amount_sign)
    try:
        token = staging.put(conn, kind, stash["files"], meta)
    except ValueError as e:                    # the staging budget
        return {"error": str(e)}, None
    need = dict(need, token=token)
    if kind == "ynab":
        need["pick"] = ""
    return {}, need


def _holding_this_file(conn, aid: str, typed: str,
                       accounts: list[str]) -> str | None:
    """The first of `accounts` holding a row of the file just imported
    into the fresh account `aid`: its rows, as the importer hashed them
    (date, amount, name, and an OFX row's FITID), recomputed under each
    account's id by sync.base._holds_file_rows."""
    from ..sync.base import LINEAGE_PROBE_LINES, _holds_file_rows
    groups: dict[str, tuple[list, list]] = {}
    for r in conn.execute(
            "SELECT id, date, amount, name FROM transactions"
            " WHERE account_id = %s ORDER BY date, id LIMIT %s",
            (aid, LINEAGE_PROBE_LINES)).fetchall():
        prefix = r["id"].split(":", 1)[0]
        lines, fitids = groups.setdefault(prefix, ([], []))
        lines.append((r["date"], float(r["amount"]), r["name"] or ""))
        head = f"{prefix}:{aid}:"
        if prefix == "ofx" and r["id"].startswith(head):
            fitids.append(r["id"][len(head):].split("~", 1)[0])
    for earlier in accounts:
        if any(_holds_file_rows(conn, prefix, earlier, typed, lines, fitids)
               for prefix, (lines, fitids) in sorted(groups.items())):
            return earlier
    return None


class AccountChoiceNeeded(ValueError):
    """A typed name spelling a full number leads to more than one account
    (two cards sharing their last four): the person picks one. Worded for
    the person, as the importers' own refusals are; `choices` holds the
    candidate account ids."""

    def __init__(self, message: str, choices=()):
        super().__init__(message)
        self.choices = list(choices)


def _choice_message(shown: str, n: int) -> str:
    many = "two" if n == 2 else str(n)
    return (f"“{shown}” matches {many} accounts that share those "
            f"last four digits — choose one for this file")


def _describe(conn, ids: list[str]) -> list[dict]:
    """Each account as a picker shows it: its name and mask, and what
    tells two cards sharing their last four apart when those read the
    same — the rows it holds, the dates they span (ISO) and its latest
    balance in cents (None when it has none)."""
    rows = {r["id"]: r for r in conn.execute(
        "SELECT a.id, COALESCE(a.display_name, a.name) AS name, a.mask,"
        "       a.balance_current, t.n, t.first_date, t.last_date"
        "  FROM accounts a LEFT JOIN LATERAL ("
        "       SELECT count(*) AS n, min(date) AS first_date,"
        "              max(date) AS last_date"
        "         FROM transactions"
        "        WHERE account_id = a.id AND removed = 0) t ON true"
        " WHERE a.id = ANY(%s)", (ids,)).fetchall()}
    iso = (lambda d: d.isoformat() if d else None)
    return [{"id": i, "name": r["name"], "mask": r["mask"],
             "rows": r["n"] or 0, "first_date": iso(r["first_date"]),
             "last_date": iso(r["last_date"]),
             "balance_cents": (None if r["balance_current"] is None
                               else round(r["balance_current"] * 100))}
            for i in ids if (r := rows.get(i))]


def _choices_for(conn, typed: str) -> list[str]:
    """The account ids a name spelling a full number could mean when it
    cannot say which; [] when it finds or makes one account. Reads only."""
    from ..sync import account_sources as srcs
    from ..sync import card_number
    if not card_number.holds_number(typed) \
            or srcs.lookup(conn, srcs.DOOR_MANUAL, typed):
        return []
    cands, shared, _ = _candidates(conn, typed, stored_name(typed))
    return cands if len(cands) > 1 or shared else []


def account_choices(conn, typed: str) -> list[dict]:
    """The accounts a new-account name could mean when it cannot say which
    ({id, name, mask} each), or [] when it makes or finds one account.
    Reads only: a plan asks it before anything is written, so the person
    chooses before the file runs rather than after."""
    return _describe(conn, _choices_for(
        conn, (typed or "").strip()[:NEW_ACCOUNT_NAME_MAX]))


def remember_choice(conn, typed: str, account_id: str) -> None:
    """Record the account the person chose for a name spelling a full
    number that could mean several (account_choices), so it is asked once:
    the next file spelling the same number lands there."""
    from ..sync import account_sources as srcs
    typed = (typed or "").strip()[:NEW_ACCOUNT_NAME_MAX]
    if account_id in _choices_for(conn, typed):
        srcs.remember(conn, srcs.DOOR_MANUAL, typed, account_id)


def moved_account(conn, account_id: str) -> list[str]:
    """[account_id] while it is an account; else the accounts a move took
    it to (sync.account_sources.moved). For a decision made before a move
    and carried out after it."""
    from ..sync import account_sources
    return account_sources.moved(conn, account_id)


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
    _refuse_unplaceable(conn, files, staged)
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
            # a choice made between accounts sharing a file's last four is
            # remembered by the job once that file has landed
            # (jobs/bulk_import_job._run_one): settled here, a file that
            # then fails, needs mapping or is refused would decide where
            # every later file of that number goes
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


def _refuse_unplaceable(conn, files: list[dict], staged: dict) -> None:
    """Refuse, before anything is consumed, a plan whose new-account name
    leads to several accounts (account_choices): the job runs later with
    nobody to ask, so the person chooses one here and posts the same
    plan again. A row naming no account at all (a plan's "choose one"
    row left unchosen) is refused the same way, rather than spending the
    upload on a job that can only refuse it. The job keeps its own
    refusal for a move that lands between this check and the file's
    turn."""
    asks = []
    for f in files:
        if f["account_id"]:
            # a hidden account takes no rows (the upsert refuses them), so
            # a plan naming one would spend the upload on a file that
            # lands nowhere and says ok
            if conn.execute(
                    "SELECT 1 FROM accounts WHERE id = %s"
                    "   AND user_removed_at IS NOT NULL",
                    (f["account_id"],)).fetchone():
                asks.append(f"{staged[f['file_id']]['filename'] or 'a file'}: "
                            "that account is hidden — un-hide it on Accounts "
                            "or choose another")
            continue
        if not f["new_account_name"]:
            asks.append(f"{staged[f['file_id']]['filename'] or 'a file'}: "
                        "choose an account for this file")
            continue
        choices = account_choices(conn, f["new_account_name"])
        if choices:
            asks.append(f"{staged[f['file_id']]['filename'] or 'a file'}: "
                        + _choice_message(stored_name(f["new_account_name"]),
                                          len(choices)))
    if asks:
        raise ValueError("; ".join(asks))


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
                # the plan's staged copy is already spent: a refusal here
                # (busy server, a restore already running) needs the file
                # uploaded again, not just a retry
                row["error"] = (started["error"].rstrip(".")
                                + " — upload this file again then")
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
