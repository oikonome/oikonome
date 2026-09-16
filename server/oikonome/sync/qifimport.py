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
stable, re-import-safe manual accounts (shared type inference), one Quicken
account → the user's pick, exactly like the other importers.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re

from ..engine.compat import as_date
from . import flowmap
from .base import Transaction, ensure_split_account, upsert_transactions
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


def parse_qif(text: str) -> list[dict]:
    out: list[dict] = []
    rec: dict = {}
    in_account = False          # inside a !Account block (N=name, T=type)
    current_account = ""        # the account subsequent !Type sections belong to
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line:
            continue
        tag, rest = line[0], line[1:]
        if tag == "!":
            # !Account starts an account header; any other bang directive
            # (!Type:Bank, !Option:AutoSwitch, !Clear:AutoSwitch, …) ends it
            in_account = rest.strip().lower() == "account"
            if in_account:
                rec = {}
            continue
        if in_account:
            if tag == "^":                     # header done — switch context
                current_account = (rec.get("_acct_name") or
                                   current_account)
                rec = {}
            elif tag == "N":
                rec["_acct_name"] = rest.strip()
            # T here is the account TYPE (Bank/CCard) — never an amount
            continue
        if tag == "^":
            if rec.get("date") and rec.get("amount") is not None:
                if current_account:
                    rec["account"] = current_account
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
        elif tag == "L":
            rec["category"] = rest.strip()
        elif tag == "N":
            rec["number"] = rest.strip()
    if rec.get("date") and rec.get("amount") is not None:
        if current_account:
            rec["account"] = current_account
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
            target = ensure_split_account(conn, "qif", qif_name)
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
    occurrence: dict[str, int] = {}
    skipped_dupes = 0
    for r in rows:
        amount = -r["amount"]                  # bank sign → engine sign
        name = (r.get("payee") or r.get("memo") or "?").strip()
        date = r["date"]
        if deduper.claim(date, amount, name):
            skipped_dupes += 1
            continue
        base = f"{account_id}|{date.isoformat()}|{round(amount * 100)}|{name.lower()}"
        occurrence[base] = occurrence.get(base, 0) + 1
        tid = "qif:" + hashlib.sha1(
            f"{base}|{occurrence[base]}".encode()).hexdigest()
        # Quicken transfer marker: L[AccountName] → TRANSFER_* by sign
        cat = (r.get("category") or "").strip()
        primary = (flowmap.transfer_category(amount)
                   if len(cat) > 2 and cat.startswith("[") and cat.endswith("]")
                   else None)
        txns.append(Transaction(
            id=tid, account_id=account_id, date=date, amount=amount,
            name=name, category_primary=primary,
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
    if batch_id:
        from . import batches
        batches.tag(conn, txns, batch_id)
    added = upsert_transactions(conn, txns)
    return {"imported": added, "rows": len(txns),
            "skipped_duplicates": skipped_dupes}
