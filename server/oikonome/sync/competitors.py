"""Importers for other personal-finance apps' exports: Monarch Money,
Copilot, Simplifi. Each is a known CSV schema with its own sign
convention, auto-detected by header shape in the /import dispatcher.
If a vendor changes their export, the generic CSV column-mapper is the
fallback — these detectors only claim files they're sure about.

Sign conventions (normalized to the engine's positive = money out):
  * Monarch: signed amounts, bank-style (negative = expense) → flip.
  * Copilot: positive = expense already (plaid-like)… BUT income rows are
    negative; kept as-is.
  * Simplifi: signed amounts, bank-style (negative = out) → flip.

Category strings are vendor/user-defined and mostly ride along in raw —
EXCEPT flow strings (card payments / transfers), which map through the
shared sync.flowmap table to spend-excluded categories; letting them
through as vendor strings makes card-payment months double-count as spend.
Copilot additionally carries a `type` column ("internal transfer") and an
`excluded` flag — both honored as TRANSFER_* so they stay spend-excluded.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
from typing import Callable

from ..engine.compat import as_date
from . import flowmap
from .base import Transaction, ensure_split_account, upsert_transactions
from .csvimport import date_order, scrub_identifiers
from .rowcap import capped
from .dedup import Deduper

MONARCH_COLUMNS = {"date", "merchant", "category", "account",
                   "original statement", "amount"}
COPILOT_COLUMNS = {"date", "name", "amount", "status", "category",
                   "account"}
SIMPLIFI_COLUMNS = {"date", "payee", "amount", "account", "category"}


def _low(header: list[str]) -> set[str]:
    return {h.strip().lower().lstrip('"') for h in header}


def looks_like_monarch(header: list[str]) -> bool:
    return MONARCH_COLUMNS <= _low(header)


def looks_like_copilot(header: list[str]) -> bool:
    h = _low(header)
    return COPILOT_COLUMNS <= h and "original statement" not in h


def looks_like_simplifi(header: list[str]) -> bool:
    h = _low(header)
    # Simplifi has Payee (Monarch has Merchant; Copilot has Name)
    return SIMPLIFI_COLUMNS <= h and "merchant" not in h and "name" not in h


def _date(s: str, order: str = "mdy") -> dt.date:
    fmts = (("%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y") if order == "mdy"
            else ("%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%m/%d/%y"))
    for fmt in ("%Y-%m-%d", *fmts):
        try:
            d = dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
        # strptime accepts years 1-9999; a year the rest of the app cannot
        # read must refuse the file here, not break every later reader
        return as_date(d)
    raise ValueError(f"unrecognized date: {s!r}")


def _money(s: str) -> float:
    s = (s or "").strip().replace("$", "").replace(",", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return float(s) if s else 0.0


def _vendor_category(low: dict, amount: float) -> tuple[str | None, str | None]:
    """Monarch/Simplifi: only the flow strings map; the rest ride in raw."""
    return flowmap.flow_category(low.get("category"), amount)


def _copilot_category(low: dict, amount: float) -> tuple[str | None, str | None]:
    """Copilot: `type` = "internal transfer" and the `excluded` flag both
    mean "not spend" → TRANSFER_* (spend-excluded); then the shared map."""
    if (low.get("type") or "").strip().lower() == "internal transfer":
        return flowmap.transfer_category(amount), None
    excluded = (low.get("excluded") or "").strip().lower()
    if excluded not in ("", "false", "0", "no"):
        return flowmap.transfer_category(amount), None
    return flowmap.flow_category(low.get("category"), amount)


def _generic(conn, account_id: str, text: str, *, prefix: str,
             name_col: str, merchant_col: str | None, flip: bool,
             batch_id: str | None,
             categorize: Callable = _vendor_category) -> dict:
    reader = csv.DictReader(io.StringIO(text))
    parsed: list[tuple] = []
    # day-first or month-first is decided per FILE (csvimport.date_order)
    rows = list(capped(reader, what="transactions"))
    lows = [{k.strip().lower(): (v or "") for k, v in row.items()}
            for row in rows]
    order = date_order(low.get("date", "") for low in lows)
    for row, low in zip(rows, lows):
        if not low.get("date") or not low.get("amount"):
            continue
        date = _date(low["date"], order)
        amount = _money(low["amount"])
        if flip:
            amount = -amount
        name = (low.get(name_col) or "?").strip()
        merchant = (low.get(merchant_col) or "").strip() if merchant_col else ""
        parsed.append((date, amount, name, merchant, low, dict(row)))

    # these exports are whole-household files with an `account` column,
    # and merging every vendor account into the one hub-selected account —
    # checking + credit card + savings as a single ledger — corrupts
    # transfers, balances and spend. One vendor account →
    # the user's pick (unchanged). Several → split: each vendor account
    # gets its own (stable, re-import-safe) manual account, and the result
    # says exactly where the rows went.
    vendor_accounts = {(p[4].get("account") or "").strip() for p in parsed}
    vendor_accounts.discard("")
    if len(vendor_accounts) <= 1:
        return _import_into(conn, account_id, parsed, prefix=prefix,
                            batch_id=batch_id, categorize=categorize)
    by_vendor: dict[str, list[tuple]] = {}
    for p in parsed:
        by_vendor.setdefault((p[4].get("account") or "?").strip(), []).append(p)
    total = {"imported": 0, "rows": 0, "skipped_duplicates": 0,
             "split_accounts": {}}
    for vendor_name in sorted(by_vendor):
        # ensure_split_account (sync.base, shared with Mint/OFX): stable
        # slug id, never clobbers existing rows, shared type inference
        target = ensure_split_account(conn, prefix, vendor_name)
        out = _import_into(conn, target, by_vendor[vendor_name],
                           prefix=prefix, batch_id=batch_id,
                           categorize=categorize)
        for k in ("imported", "rows", "skipped_duplicates"):
            total[k] += out[k]
        total["split_accounts"][vendor_name] = out["imported"]
    total["note"] = (f"file contains {len(by_vendor)} accounts — split "
                     f"into one account per vendor account (the selected "
                     f"account was not used)")
    return total


def _import_into(conn, account_id: str, parsed: list[tuple], *, prefix: str,
                 batch_id: str | None, categorize: Callable) -> dict:
    deduper = Deduper(conn, account_id, prefix, [p[0] for p in parsed])
    txns: list[Transaction] = []
    occurrence: dict[str, int] = {}
    skipped_dupes = 0
    for date, amount, name, merchant, low, raw in parsed:
        if deduper.claim(date, amount, name, merchant):
            skipped_dupes += 1
            continue
        base = f"{account_id}|{date.isoformat()}|{round(amount * 100)}|{name.lower()}"
        occurrence[base] = occurrence.get(base, 0) + 1
        tid = f"{prefix}:" + hashlib.sha1(
            f"{base}|{occurrence[base]}".encode()).hexdigest()
        primary, detailed = categorize(low, amount)
        txns.append(Transaction(
            id=tid, account_id=account_id, date=date, amount=amount,
            name=name, merchant_name=merchant or None,
            category_primary=primary, category_detailed=detailed,
            # same account/routing-column scrub as mint/ynab/qif — a
            # vendor export that grows such a column must not land it
            # verbatim in raw, which rides /export unredacted
            raw=scrub_identifiers(raw)))
    if batch_id:
        from . import batches
        batches.tag(conn, txns, batch_id)
    added = upsert_transactions(conn, txns)
    return {"imported": added, "rows": len(txns),
            "skipped_duplicates": skipped_dupes}


def import_monarch(conn, account_id: str, text: str,
                   batch_id: str | None = None) -> dict:
    return _generic(conn, account_id, text, prefix="monarch",
                    name_col="original statement", merchant_col="merchant",
                    flip=True, batch_id=batch_id)


def import_copilot(conn, account_id: str, text: str,
                   batch_id: str | None = None) -> dict:
    return _generic(conn, account_id, text, prefix="copilot",
                    name_col="name", merchant_col="name",
                    flip=False, batch_id=batch_id,
                    categorize=_copilot_category)


def import_simplifi(conn, account_id: str, text: str,
                    batch_id: str | None = None) -> dict:
    return _generic(conn, account_id, text, prefix="simplifi",
                    name_col="payee", merchant_col="payee",
                    flip=True, batch_id=batch_id)
