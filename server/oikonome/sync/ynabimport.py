"""YNAB register-export importer — tier 1 of the import hub. YNAB's
register CSV carries separate Outflow/Inflow columns (both positive,
currency-formatted); categories are user-defined so they pass through as
raw metadata only (the recurring finder + future categorizer do the rest).

Detection: header contains Date + Payee + Outflow + Inflow.
Sign: Outflow → engine positive (money out); Inflow → negative.
ids: ynab:<sha1|occurrence>; cross-source dedup via the shared sync.dedup
matcher. YNAB's transfer marker ("Transfer : <Account>" payee) and
"Starting Balance" rows map to TRANSFER_* so they never count as spend.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io

from ..engine.compat import as_date
from . import flowmap
from .base import Transaction, upsert_transactions
from .csvimport import date_order, scrub_identifiers
from .rowcap import capped
from .dedup import Deduper

YNAB_COLUMNS = {"date", "payee", "outflow", "inflow"}


def looks_like_ynab(header: list[str]) -> bool:
    return YNAB_COLUMNS <= {h.strip().lower().lstrip('"') for h in header}


def _money(s: str) -> float:
    s = (s or "").strip().replace("$", "").replace(",", "")
    return float(s) if s else 0.0


def _date(s: str, order: str = "mdy") -> dt.date:
    fmts = (("%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y") if order == "mdy"
            else ("%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%m/%d/%y"))
    for fmt in ("%Y-%m-%d", *fmts, "%Y/%m/%d"):
        try:
            d = dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
        # a year the rest of the app cannot read is refused, never stored
        return as_date(d)
    raise ValueError(f"unrecognized YNAB date: {s!r}")


def import_ynab(conn, account_id: str, text: str,
                batch_id: str | None = None) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    parsed: list[tuple] = []
    # day-first or month-first is a property of the FILE (see
    # csvimport.date_order) — read the whole column before parsing a row
    rows = list(capped(reader, what="transactions"))
    lows = [{k.strip().lower(): (v or "") for k, v in row.items()}
            for row in rows]
    order = date_order(low.get("date", "") for low in lows)
    for row, low in zip(rows, lows):
        if not low.get("date"):
            continue
        out = _money(low.get("outflow", ""))
        inn = _money(low.get("inflow", ""))
        if out == 0 and inn == 0:
            continue
        amount = out - inn                     # engine sign: positive = out
        date = _date(low["date"], order)
        payee = (low.get("payee") or "").strip()
        name = (payee or low.get("memo") or "?").strip()
        # the whole source row lands in transactions.raw, and looks_like_ynab
        # matches on a column SUBSET — a register re-exported through a bank
        # or converter tool can carry an account/routing column into it, and
        # raw rides /export verbatim. Same scrub as the CSV importer; the
        # YNAB "Account" column is a budget-account nickname and is kept.
        parsed.append((date, amount, name, payee, scrub_identifiers(row)))
    deduper = Deduper(conn, account_id, "ynab", [p[0] for p in parsed])
    txns: list[Transaction] = []
    occurrence: dict[str, int] = {}
    skipped_dupes = 0
    for date, amount, name, payee, raw in parsed:
        if deduper.claim(date, amount, name):
            skipped_dupes += 1
            continue
        base = f"{account_id}|{date.isoformat()}|{round(amount * 100)}|{name.lower()}"
        occurrence[base] = occurrence.get(base, 0) + 1
        tid = "ynab:" + hashlib.sha1(
            f"{base}|{occurrence[base]}".encode()).hexdigest()
        # YNAB's transfer marker + starting-balance rows are flows, not
        # spend/income: outflow → TRANSFER_OUT, inflow → TRANSFER_IN
        pl = payee.lower()
        primary = (flowmap.transfer_category(amount)
                   if pl.startswith("transfer : ") or pl == "starting balance"
                   else None)
        txns.append(Transaction(
            id=tid, account_id=account_id, date=date, amount=amount,
            name=name, merchant_name=payee or None,
            category_primary=primary, raw=raw))
    if batch_id:
        from . import batches
        batches.tag(conn, txns, batch_id)
    added = upsert_transactions(conn, txns)
    return {"imported": added, "rows": len(txns),
            "skipped_duplicates": skipped_dupes}
