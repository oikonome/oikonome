"""Quicken QIF importer — tier 1 of the import hub. QIF is a line-tagged
format from the 90s that half the world's financial history still lives
in; the parser needs no dependency:

    !Type:Bank          header (Bank/CCard/Cash/...)
    D01/15'04           date (many variants incl. the ' year quirk)
    T-42.50             amount (bank sign: negative = money out)
    PSAFEWAY STORE      payee
    MMEMO TEXT          memo
    LGroceries          category
    ^                   end of record

Sign: bank convention → flipped to the engine's (positive = out).
ids: qif:<sha1(...)|occurrence>. Cross-source dedup via sync.dedup.
Quicken's transfer marker — category `L[AccountName]` (square brackets) —
maps to TRANSFER_OUT/IN by sign so transfers never count as spend.

Multi-account files follow the same doctrine as the Mint importer: a
whole-file Quicken export interleaves `!Account` blocks (N = name, T = type)
with each account's `!Type:` sections. Those blocks must not reach the
amount parser (the account block's T-for-type line is not a float), and
merging every account into the one hub-selected account corrupts transfers
and spend.
Records now carry their source account; >1 distinct account splits into
stable, re-import-safe manual accounts, one Quicken account → the user's
pick, exactly like the other importers. A split account takes the type its
`!Account` block declares (CCard is a card, Invst a brokerage); only an
account that declares none falls back to the name inference.

Investment sections (`!Type:Invst`) write the amount UNSIGNED: the action
in N says which way the money went (Buy is out, Div is in), and a
reinvestment or a share movement moves no cash at all. Those records are
signed from the action instead of from T, and a share purchase or sale is
a move inside the account, never spend or income.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re

from ..engine.compat import as_date
from . import flowmap
from .base import (FileImportIds, Transaction, ensure_split_account,
                   drop_before_history, upsert_transactions)
from .csvimport import scrub_identifiers
from .csvimport import date_order
from .rowcap import MAX_IMPORT_ROWS, check_len
from .dedup import Deduper


def _clean_date(s: str) -> str:
    s = s.strip().replace("'", "/")          # 01/15'04 → 01/15/04
    return re.sub(r"\s+", "", s)


def _date(s: str, order: str = "mdy") -> dt.date:
    s = _clean_date(s)
    fmts = (("%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y") if order == "mdy"
            else ("%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%m/%d/%y"))
    for fmt in (*fmts, "%Y-%m-%d"):
        try:
            d = dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        # a year the rest of the app cannot read is refused, never stored
        return as_date(d)
    raise ValueError(f"unrecognized QIF date: {s!r}")


# Quicken's account types, as the engine types an account. A type not
# listed here (Oth A, a type a newer Quicken invented) is left to the name.
_ACCOUNT_TYPES = {
    "ccard": ("credit", "credit card"),
    "invst": ("investment", "brokerage"),
    "port": ("investment", "brokerage"),
    "401(k)/403(b)": ("investment", "401k"),
    "oth l": ("loan", None),
}

# Investment actions by what they do to the account's cash. The X forms
# move the cash to or from another account instead, in the same direction.
_INVEST_OUT_MOVE = {"buy", "cvrshrt", "xout", "withdrw"}
_INVEST_IN_MOVE = {"sell", "shtsell", "xin", "contrib", "rtrncap"}
_INVEST_IN_INCOME = {"div": "INCOME_DIVIDENDS",
                     "intinc": "INCOME_INTEREST_EARNED",
                     "cglong": "INCOME_DIVIDENDS",
                     "cgmid": "INCOME_DIVIDENDS",
                     "cgshort": "INCOME_DIVIDENDS",
                     "miscinc": None}
_INVEST_OUT_COST = {"miscexp", "margint"}
_INVEST_NO_CASH = {"reinvdiv", "reinvint", "reinvlg", "reinvmd", "reinvsh",
                   "stksplit", "shrsin", "shrsout", "reprice", "grant",
                   "vest", "exercise", "expire", "reminder"}


def account_type(qif_type: str | None) -> tuple[str | None, str | None]:
    """(type, subtype) for a Quicken account type; (None, None) when the
    file does not say, so the caller falls back to the name."""
    return _ACCOUNT_TYPES.get((qif_type or "").strip().lower(), (None, None))


def _invest_action(action: str | None) -> str:
    a = (action or "").strip().lower()
    return a[:-1] if a.endswith("x") and a[:-1] in (
        _INVEST_OUT_MOVE | _INVEST_IN_MOVE | set(_INVEST_IN_INCOME)
        | _INVEST_OUT_COST) else a


def _signed(r: dict) -> tuple[float | None, str | None, str | None]:
    """(engine amount, category, detailed category) for one record, or a
    None amount for an investment record that moves no cash."""
    if (r.get("section") or "").lower() != "invst":
        return -r["amount"], None, None        # bank sign → engine sign
    act = _invest_action(r.get("action"))
    size = abs(r["amount"])
    if act in _INVEST_NO_CASH:
        return None, None, None
    if act in _INVEST_OUT_MOVE:
        return size, flowmap.transfer_category(size), None
    if act in _INVEST_IN_MOVE:
        return -size, flowmap.transfer_category(-size), None
    if act in _INVEST_IN_INCOME:
        detailed = _INVEST_IN_INCOME[act]
        return -size, ("INCOME" if detailed else None), detailed
    if act in _INVEST_OUT_COST:
        return size, None, None
    # an action this list does not know: cash written with its sign (the
    # Cash action, and what older exports do), read the way a bank's is
    return -r["amount"], None, None


# Quicken's whole-file export also writes LISTS — categories, classes,
# securities, memorized payees, prices — as !Type: sections. Their tags mean
# other things (a security's T is its type, "Stock"; a category's bare T is
# the tax-related flag), so their records are skipped, never read as money.
_LIST_SECTIONS = {"cat", "class", "security", "memorized", "prices",
                  "invitem", "template", "bud"}


# A non-empty line, split where str.splitlines splits (an old Mac export
# ends its lines with a bare CR); empty lines were skipped anyway.
_LINE = re.compile("[^\n\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029]+")


def parse_qif(text: str) -> list[dict]:
    out: list[dict] = []
    rec: dict = {}
    in_account = False          # inside a !Account block (N=name, T=type)
    current_account = ""        # the account subsequent !Type sections belong to
    current_type = ""           # that account's declared Quicken type
    section = ""                # the !Type: of the records being read
    types: dict[str, str] = {}  # account name → its declared type
    # one line at a time: splitlines() built every line of the file as its
    # own string up front — about eight times the text for a file of short
    # lines, none of which reaches the row cap (only finished records
    # count) — before the first record was read
    for m in _LINE.finditer(text):
        line = m.group()
        tag, rest = line[0], line[1:]
        if tag == "!":
            # !Account starts an account header; any other bang directive
            # (!Type:Bank, !Option:AutoSwitch, !Clear:AutoSwitch, …) ends it
            in_account = rest.strip().lower() == "account"
            if in_account:
                rec = {}
                if section.lower() in _LIST_SECTIONS:
                    section = ""        # a list ended; accounts follow
            elif rest.lower().startswith("type:"):
                section = rest[5:].strip()
                rec = {}
            continue
        if section.lower() in _LIST_SECTIONS:
            continue
        if in_account:
            if tag == "^":                     # header done — switch context
                current_account = (rec.get("_acct_name") or
                                   current_account)
                current_type = rec.get("_acct_type") or ""
                if current_account and current_type:
                    types.setdefault(current_account, current_type)
                rec = {}
            elif tag == "N":
                rec["_acct_name"] = rest.strip()
            elif tag == "T":
                # the account TYPE (Bank/CCard/Invst) — never an amount
                rec["_acct_type"] = rest.strip()
            continue
        if tag == "^":
            if rec.get("date") and rec.get("amount") is not None:
                if current_account:
                    rec["account"] = current_account
                rec["section"] = section
                if current_type:
                    rec["account_type"] = current_type
                # the cap is enforced as records are built — materializing
                # a whole hostile file to count it afterwards is the very
                # thing the cap exists to prevent
                if len(out) >= MAX_IMPORT_ROWS:
                    check_len(out + [rec], what="transactions")
                out.append(rec)
            rec = {}
            continue
        if tag == "D":
            # kept raw until the whole file is read: day-first or
            # month-first is decided per FILE, not per record
            rec["date"] = _clean_date(rest)
        elif tag == "T" or tag == "U":
            rec["amount"] = float(rest.replace(",", "").replace("$", ""))
        elif tag == "P":
            rec["payee"] = rest.strip()
        elif tag == "M":
            rec["memo"] = rest.strip()
        elif tag == "Y" and section.lower() == "invst":
            rec["security"] = rest.strip()
        elif tag == "L":
            rec["category"] = rest.strip()
        elif tag == "N":
            # in an investment section N is the ACTION (Buy, Div, …); in
            # every other section it is the check number
            if section.lower() == "invst":
                rec["action"] = rest.strip()
            else:
                rec["number"] = rest.strip()
    if rec.get("date") and rec.get("amount") is not None:
        if current_account:
            rec["account"] = current_account
        rec["section"] = section
        if current_type:
            rec["account_type"] = current_type
        # the unterminated last record counts against the cap too
        if len(out) >= MAX_IMPORT_ROWS:
            check_len(out + [rec], what="transactions")
        out.append(rec)
    order = date_order(r["date"] for r in out)
    for r in out:
        r["date"] = _date(r["date"], order)
    return out


def import_qif(conn, account_id: str, text: str,
               batch_id: str | None = None) -> dict:
    rows = check_len(parse_qif(text), what="transactions")
    # several distinct !Account headers → split per account
    # (mint doctrine: a single named account still honors the user's pick)
    qif_accounts = {r["account"] for r in rows if r.get("account")}
    if len(qif_accounts) > 1:
        by_acct: dict[str, list[dict]] = {}
        for r in rows:
            by_acct.setdefault(r.get("account") or "?", []).append(r)
        total = {"imported": 0, "rows": 0, "skipped_duplicates": 0,
                 "split_accounts": {}}
        for qif_name in sorted(by_acct):
            declared = next((r["account_type"] for r in by_acct[qif_name]
                             if r.get("account_type")), None)
            if declared is None and any(
                    (r.get("section") or "").lower() == "invst"
                    for r in by_acct[qif_name]):
                declared = "Invst"
            type_, subtype = account_type(declared)
            target = ensure_split_account(conn, "qif", qif_name,
                                          type_=type_, subtype=subtype)
            out = _import_into(conn, target, by_acct[qif_name],
                               batch_id=batch_id)
            for k in ("imported", "rows", "skipped_duplicates"):
                total[k] += out[k]
            total["split_accounts"][qif_name] = out["imported"]
        total["note"] = (f"file contains {len(by_acct)} accounts — split "
                         f"into one account per Quicken account (the "
                         f"selected account was not used)")
        return total
    return _import_into(conn, account_id, rows, batch_id=batch_id)


def _import_into(conn, account_id: str, rows: list[dict], *,
                 batch_id: str | None) -> dict:
    deduper = Deduper(conn, account_id, "qif", [r["date"] for r in rows])
    txns: list[Transaction] = []
    ids = FileImportIds("qif", account_id)
    # The ids an earlier release gave investment records, which it signed
    # as written (-T) and named by payee or memo alone, for every action —
    # share-only ones included. Counted on their own, over every record, so
    # each occurrence number matches the one that import gave it.
    legacy_ids = FileImportIds("qif", account_id)
    legacy_of: dict[str, list[tuple[str, str]]] = {}
    no_cash_legacy: list[tuple[str, str]] = []
    skipped_dupes = 0
    for r in rows:
        amount, invest_cat, invest_detail = _signed(r)
        invst = (r.get("section") or "").lower() == "invst"
        # each legacy id travels with the action of the record it named:
        # the two counters number different record sets, so a cash
        # record's new id can equal another record's legacy id, and only a
        # stored row of the SAME kind of record is the one it replaces
        old_ids: list[tuple[str, str]] = []
        if invst:
            old_name = (r.get("payee") or r.get("memo") or "?").strip()
            lid = legacy_ids.id_for(r["date"], -r["amount"], old_name)
            act = (r.get("action") or "").strip().lower()
            old_ids = [(x, act) for x in (lid, legacy_ids.legacy_id(lid))
                       if x]
        if amount is None:
            no_cash_legacy.extend(old_ids)     # moved shares, not cash
            continue
        name = (r.get("payee") or r.get("memo") or r.get("security")
                or r.get("action") or "?").strip()
        date = r["date"]
        if deduper.claim(date, amount, name):
            skipped_dupes += 1
            continue
        tid = ids.id_for(date, amount, name)
        if old_ids and tid not in {x for x, _ in old_ids}:
            legacy_of[tid] = old_ids
        # Quicken transfer marker: L[AccountName] → TRANSFER_* by sign
        cat = (r.get("category") or "").strip()
        primary = (flowmap.transfer_category(amount)
                   if len(cat) > 2 and cat.startswith("[") and cat.endswith("]")
                   else invest_cat)
        txns.append(Transaction(
            id=tid, account_id=account_id, date=date, amount=amount,
            name=name, category_primary=primary,
            category_detailed=(invest_detail if primary == invest_cat
                               else None),
            # transactions.raw rides /export verbatim, so the CSV family's
            # scrub runs here too. QIF's tag set carries no account number
            # today (N inside a transaction is the CHECK number, and the
            # `account` key is Quicken's account NAME — the split accounts
            # are named from it, so both are kept whole); this keeps the
            # rule with the write, where a new tag would otherwise sail
            # past it.
            raw=scrub_identifiers(
                {k: (v.isoformat() if isinstance(v, dt.date) else v)
                 for k, v in r.items()})))
    txns = ids.resolve(conn, txns)
    if legacy_of:
        txns = _adopt_legacy_invst(conn, account_id, txns, legacy_of)
    # the household's history window (Settings → how far back): rows
    # dated before it are left out and counted, never written
    every = txns
    txns, skipped_older = drop_before_history(conn, txns)
    if batch_id:
        from . import batches
        batches.tag(conn, txns, batch_id)
    added = upsert_transactions(conn, txns)
    # never a row this import has just written: that is a cash record
    # whose id happens to equal a share-only record's old one. Read over
    # every row of the file, the ones the history window left out
    # included: whether an old record is retired must not depend on it
    written = {t.id for t in every}
    no_cash_legacy = [p for p in no_cash_legacy if p[0] not in written]
    if no_cash_legacy:
        _retire_legacy_no_cash(conn, account_id, no_cash_legacy)
    return {"imported": added, "rows": len(txns),
            "skipped_duplicates": skipped_dupes,
            "skipped_older": skipped_older}


def _adopt_legacy_invst(conn, account_id: str, txns: list[Transaction],
                        legacy_of: dict[str, list[tuple[str, str]]]
                        ) -> list[Transaction]:
    """Correct, in place, an investment row an earlier import signed as
    written and named by payee alone. Its id hashes that sign and name, so
    re-importing the file would compute a new id and insert the corrected
    row beside the old, wrongly-signed one — a buy counted once as money in
    and once as money out. Where the old id stands on this account as an
    investment record, and the corrected id does not, the row keeps the old
    id and the upsert rewrites its amount, name and category. Only a
    stored row of the same action: another record's row that happens to
    wear the id is not this one's to rewrite."""
    candidates = [x for t in txns for x, _ in legacy_of.get(t.id, ())]
    if not candidates:
        return txns
    rows = conn.execute(
        "SELECT id, lower(COALESCE(raw->>'section', '')) AS section,"
        " lower(trim(raw->>'action')) AS action"
        " FROM transactions WHERE account_id = %s AND id = ANY(%s)",
        (account_id, candidates + [t.id for t in txns])).fetchall()
    have = {r["id"] for r in rows}
    # a row written before actions were kept says nothing either way
    invst = {r["id"]: r["action"] for r in rows if r["section"] == "invst"}
    taken: set[str] = set()
    out = []
    for t in txns:
        old = next((x for x, act in legacy_of.get(t.id, ())
                    if x in invst and invst[x] in (None, act)
                    and x not in taken), None)
        if old is not None and t.id not in have:
            taken.add(old)
            t = dataclasses.replace(t, id=old)
        out.append(t)
    return out


def _retire_legacy_no_cash(conn, account_id: str,
                           legacy: list[tuple[str, str]]) -> None:
    """Retire the rows an earlier import made for share-only investment
    records (a reinvested dividend, a split): they moved no cash, and left
    in place they read as income or spend that never happened. Only rows
    still standing as investment records of this account, written for the
    same action as the share-only record that named the id."""
    conn.execute(
        "UPDATE transactions t SET removed = 1"
        " FROM unnest(%s::text[], %s::text[]) AS v(id, action)"
        " WHERE t.account_id = %s AND t.id = v.id AND t.removed = 0"
        " AND lower(COALESCE(t.raw->>'section', '')) = 'invst'"
        " AND (t.raw->>'action' IS NULL"
        "      OR lower(trim(t.raw->>'action')) = v.action)",
        ([x for x, _ in legacy], [a for _, a in legacy], account_id))
