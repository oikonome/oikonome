"""Mint export importer — tier 1 of the import hub.

Mint's transactions.csv is a FIXED schema: Date, Description,
Original Description, Amount, Transaction Type, Category, Account Name,
Labels, Notes. Amount is always positive; "Transaction Type"
(debit/credit) carries the direction → debit = money out = engine
positive. Categories map to the engine's primaries through the table
below; "Transfer" resolves direction by sign; unknown categories pass
through as None (the display falls back gracefully).

ids: mint:<sha1(account|date|cents|desc|occurrence)> — idempotent, and
identical same-day rows survive via the occurrence counter. Cross-source
dedup reuses csvimport's ±3-day window.

Multi-account files: "Account Name" carries the
Mint-side account; >1 distinct name splits per account exactly like the
other-app importers (stable slug ids, re-import-safe, shared type
inference), with per-account counts in the result.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io

from . import flowmap
from .base import Transaction, ensure_split_account, upsert_transactions
from .csvimport import date_order, scrub_identifiers
from .rowcap import capped
from .dedup import Deduper

MINT_COLUMNS = {"date", "description", "amount", "transaction type",
                "category", "account name"}

CATEGORY_MAP = {
    "Groceries": "FOOD_AND_DRINK", "Restaurants": "FOOD_AND_DRINK",
    "Fast Food": "FOOD_AND_DRINK", "Coffee Shops": "FOOD_AND_DRINK",
    "Alcohol & Bars": "FOOD_AND_DRINK", "Food & Dining": "FOOD_AND_DRINK",
    "Gas & Fuel": "TRANSPORTATION", "Parking": "TRANSPORTATION",
    "Rental Car & Taxi": "TRANSPORTATION",
    "Public Transportation": "TRANSPORTATION",
    "Auto & Transport": "TRANSPORTATION", "Auto Insurance": "TRANSPORTATION",
    "Auto Payment": "TRANSPORTATION", "Service & Parts": "TRANSPORTATION",
    "Tolls": "TRANSPORTATION",
    "Hotel": "TRAVEL", "Air Travel": "TRAVEL", "Travel": "TRAVEL",
    "Vacation": "TRAVEL",
    "Entertainment": "ENTERTAINMENT", "Movies & DVDs": "ENTERTAINMENT",
    "Music": "ENTERTAINMENT", "Amusement": "ENTERTAINMENT",
    "Arts": "ENTERTAINMENT", "Newspapers & Magazines": "ENTERTAINMENT",
    "Sports": "ENTERTAINMENT", "Books": "ENTERTAINMENT",
    "Bills & Utilities": "RENT_AND_UTILITIES",
    "Utilities": "RENT_AND_UTILITIES", "Mobile Phone": "RENT_AND_UTILITIES",
    "Internet": "RENT_AND_UTILITIES", "Television": "RENT_AND_UTILITIES",
    "Mortgage & Rent": "RENT_AND_UTILITIES",
    "Home Phone": "RENT_AND_UTILITIES",
    "Home Insurance": "RENT_AND_UTILITIES",
    "Home Improvement": "HOME_IMPROVEMENT",
    "Home Supplies": "HOME_IMPROVEMENT", "Furnishings": "HOME_IMPROVEMENT",
    "Lawn & Garden": "HOME_IMPROVEMENT", "Home Services": "HOME_IMPROVEMENT",
    "Home": "HOME_IMPROVEMENT",
    "Doctor": "MEDICAL", "Dentist": "MEDICAL", "Pharmacy": "MEDICAL",
    "Health & Fitness": "MEDICAL", "Eyecare": "MEDICAL",
    "Health Insurance": "MEDICAL",
    "Gym": "PERSONAL_CARE", "Hair": "PERSONAL_CARE",
    "Personal Care": "PERSONAL_CARE", "Spa & Massage": "PERSONAL_CARE",
    "Laundry": "PERSONAL_CARE",
    "Pets": "GENERAL_MERCHANDISE",
    "Pet Food & Supplies": "GENERAL_MERCHANDISE",
    "Veterinary": "GENERAL_SERVICES",
    "Babies & Kids": "GENERAL_MERCHANDISE",
    "Baby Supplies": "GENERAL_MERCHANDISE", "Kids": "GENERAL_MERCHANDISE",
    "Toys": "GENERAL_MERCHANDISE", "Kids Activities": "ENTERTAINMENT",
    "Child Support": "GENERAL_SERVICES", "Education": "GENERAL_SERVICES",
    "Tuition": "GENERAL_SERVICES",
    "Student Loan": "LOAN_PAYMENTS", "Loans": "LOAN_PAYMENTS",
    "Loan Payment": "LOAN_PAYMENTS",
    "Shopping": "GENERAL_MERCHANDISE", "Clothing": "GENERAL_MERCHANDISE",
    "Electronics & Software": "GENERAL_MERCHANDISE",
    "Sporting Goods": "GENERAL_MERCHANDISE",
    "Hobbies": "GENERAL_MERCHANDISE",
    "Office Supplies": "GENERAL_MERCHANDISE", "Printing": "GENERAL_SERVICES",
    "Shipping": "GENERAL_SERVICES", "Gift": "GENERAL_MERCHANDISE",
    "Gifts & Donations": "GENERAL_MERCHANDISE",
    "Charity": "GOVERNMENT_AND_NON_PROFIT",
    "Subscriptions": "GENERAL_SERVICES",
    "Business Services": "GENERAL_SERVICES", "Legal": "GENERAL_SERVICES",
    "Financial": "GENERAL_SERVICES",
    "Bank Fee": "BANK_FEES", "Finance Charge": "BANK_FEES",
    "Late Fee": "BANK_FEES", "Service Fee": "BANK_FEES",
    "ATM Fee": "BANK_FEES", "Fees & Charges": "BANK_FEES",
    "Taxes": "GOVERNMENT_AND_NON_PROFIT",
    "Federal Tax": "GOVERNMENT_AND_NON_PROFIT",
    "State Tax": "GOVERNMENT_AND_NON_PROFIT",
    "Local Tax": "GOVERNMENT_AND_NON_PROFIT",
    "Cash & ATM": "TRANSFER_OUT", "Withdrawal": "TRANSFER_OUT",
    "Deposit": "TRANSFER_IN", "Investment": "TRANSFER_OUT",
    "Investments": "TRANSFER_OUT", "Buy": "TRANSFER_OUT",
    "Sell": "TRANSFER_IN",
    "Paycheck": "INCOME", "Income": "INCOME", "Interest Income": "INCOME",
    "Bonus": "INCOME", "Reimbursement": "INCOME", "Rental Income": "INCOME",
    "Returned Purchase": "INCOME", "Credit Card Cashback": "INCOME",
}
# card settlements + transfer-ish strings map through the SHARED flow map
# (sync.flowmap) so every importer excludes them from spend identically


def looks_like_mint(header: list[str]) -> bool:
    return MINT_COLUMNS <= {h.strip().lower() for h in header}


def _date(s: str, order: str = "mdy") -> dt.date:
    fmts = (("%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y") if order == "mdy"
            else ("%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%m/%d/%y"))
    for fmt in (*fmts, "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            continue
    raise ValueError(f"unrecognized Mint date: {s!r}")


def import_mint(conn, account_id: str, text: str,
                batch_id: str | None = None) -> dict:
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
        amt = abs(float(low["amount"].replace(",", "").replace("$", "")))
        # debit = money out = engine positive; credit = in = negative
        amount = amt if low.get("transaction type", "").lower() == "debit" else -amt
        name = (low.get("original description") or low.get("description") or "?").strip()
        merchant = (low.get("description") or "").strip() or None
        cat_raw = low.get("category", "").strip()
        acct = (low.get("account name") or "").strip()
        # the WHOLE source row rides into transactions.raw, and detection is
        # by column SUBSET (looks_like_mint), so a re-export carrying its
        # bank's "Account Number" column imports as Mint and would store it
        # verbatim in a column that leaves through /export. Mint's own
        # "Account Name" is a nickname, not an identifier — it is kept (the
        # per-account split reads it).
        parsed.append((date, amount, name, merchant, cat_raw, acct,
                       scrub_identifiers(row)))

    # A Mint transactions.csv is a whole-household file with an "Account
    # Name" column. Merging every Mint account into the one hub-selected
    # account would put checking + cards + savings in a single ledger, which
    # corrupts transfers, balances, and spend. Same split as the other
    # importers: one Mint account → the user's pick (unchanged); several → each gets its own stable,
    # re-import-safe manual account, with per-account counts reported.
    mint_accounts = {p[5] for p in parsed if p[5]}
    if len(mint_accounts) <= 1:
        return _import_into(conn, account_id, parsed, batch_id=batch_id)
    by_acct: dict[str, list[tuple]] = {}
    for p in parsed:
        by_acct.setdefault(p[5] or "?", []).append(p)
    total = {"imported": 0, "rows": 0, "skipped_duplicates": 0,
             "split_accounts": {}}
    for mint_name in sorted(by_acct):
        target = ensure_split_account(conn, "mint", mint_name)
        out = _import_into(conn, target, by_acct[mint_name],
                           batch_id=batch_id)
        for k in ("imported", "rows", "skipped_duplicates"):
            total[k] += out[k]
        total["split_accounts"][mint_name] = out["imported"]
    total["note"] = (f"file contains {len(by_acct)} accounts — split "
                     f"into one account per Mint account (the selected "
                     f"account was not used)")
    return total


def _import_into(conn, account_id: str, parsed: list[tuple], *,
                 batch_id: str | None) -> dict:
    deduper = Deduper(conn, account_id, "mint", [p[0] for p in parsed])
    txns: list[Transaction] = []
    occurrence: dict[str, int] = {}
    skipped_dupes = 0
    for date, amount, name, merchant, cat_raw, _acct, raw in parsed:
        if deduper.claim(date, amount, name, merchant):
            skipped_dupes += 1
            continue
        primary, detailed = flowmap.flow_category(cat_raw, amount)
        if primary is None:
            primary, detailed = CATEGORY_MAP.get(cat_raw), None
        base = f"{account_id}|{date.isoformat()}|{round(amount * 100)}|{name.lower()}"
        occurrence[base] = occurrence.get(base, 0) + 1
        tid = "mint:" + hashlib.sha1(
            f"{base}|{occurrence[base]}".encode()).hexdigest()
        txns.append(Transaction(
            id=tid, account_id=account_id, date=date, amount=amount,
            name=name, merchant_name=merchant,
            category_primary=primary, category_detailed=detailed,
            raw={**raw, "_mint_category": cat_raw}))
    if batch_id:
        from . import batches
        batches.tag(conn, txns, batch_id)
    added = upsert_transactions(conn, txns)
    return {"imported": added, "rows": len(txns),
            "skipped_duplicates": skipped_dupes}
