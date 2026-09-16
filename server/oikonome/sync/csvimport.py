"""Generic CSV transaction import — the always-available sync path (works
with any bank's export; the floor when no aggregator fits).

Design:
  * column mapping supplied by the caller/wizard: {"date": "Date",
    "amount": "Amount", "name": "Description", ...} — header names or
    0-based indexes.
  * `amount_sign`: 'bank' (negative = money out; most bank exports) or
    'plaid' (positive = out). Normalized to the engine convention here.
  * ids are content-hashed with an occurrence counter
    (csv:<sha1(account|date|cents|name|occurrence)>) — the SAME file
    imports idempotently, and two identical same-day charges survive.
  * dedup against rows from OTHER sources via the shared one-to-one,
    name-aware matcher (sync.dedup) — a CSV overlapping an aggregator's
    window must not double-count (posting-date drift tolerated).
  * the category column routes through the shared flow map (sync.flowmap)
    so card payments / transfers land spend-excluded, not as vendor
    strings the spend definition counts.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import re

from ..engine.compat import as_date
from . import flowmap
from .base import Transaction, upsert_transactions
from .rowcap import capped
from .dedup import WINDOW_DAYS as DEDUP_WINDOW_DAYS  # noqa: F401 (re-export)
from .dedup import Deduper


# Data minimization for `transactions.raw`, the same rule ofximport applies
# to the OFX ACCTID and mx.py to account_number/routing_number: raw is
# plaintext JSONB that leaves the instance verbatim through /export and the
# data-portability archive, so a full account or routing number must never
# land in it. Plenty of real bank and credit-union exports (exactly the
# banks with no aggregator coverage, i.e. this importer's population) put an
# "Account Number" or "Routing Number" column on every row.
#
# What is scrubbed: a column whose HEADER names one of these identifiers —
# truncated to its last 4 characters, which is all any display or matching
# here ever wants (accounts.mask is a last-4 too). What is kept: everything
# else in the row, unchanged. raw is what re-categorization and an audit of
# "where did this row come from" read, so blanking the row wholesale would
# cost real function to buy nothing — the leak is the identifier, not the
# row. Matching is on the header name only: a regex over every VALUE would
# mangle memos and reference numbers that merely look account-shaped.
_ID_HEADERS = (
    "account number", "account no", "account num", "account nbr",
    "acct number", "acct no", "acct num", "acct nbr",
    "acctno", "acctnum", "accountnumber", "bank account",
    "routing", "aba number", "iban", "sort code",
    "card number", "card no", "card num", "cardnumber",
    "member number", "member no",
)


def _norm_header(h) -> str:
    """Header names compared with punctuation flattened, so Account_Number,
    ACCOUNT NUMBER, "Acct. No." and "Account #" all read the same. '#' is
    spelled out rather than dropped — it is the whole discriminator in
    "Account #", and bare "Account" is a nickname column at least as often
    as a number."""
    return re.sub(r"[^a-z0-9]+", " ",
                  str(h).lower().replace("#", " number ")).strip()


def scrub_identifiers(raw: dict) -> dict:
    """A copy of one source row with account/routing-shaped COLUMNS cut to
    their last 4 characters (see _ID_HEADERS for the why). Shared by the
    CSV-family importers — mint/ynab/qif detect their formats by column
    subset, so a bank's re-export can carry these columns into any of
    them."""
    out = {}
    for k, v in raw.items():
        n = _norm_header(k)
        if v not in (None, "") and any(t in n for t in _ID_HEADERS):
            out[k] = str(v).strip()[-4:]
        else:
            out[k] = v
    return out


_SLASH_DATE = re.compile(r"^\s*(\d{1,2})[/.\-](\d{1,2})[/.\-](\d{2,4})\s*$")


def date_order(values) -> str:
    """Which way a column's two-number dates read: "dmy" when any first
    field exceeds 12 (a day), "mdy" when any second field does (a month
    cannot). A column that never disambiguates reads month-first — the
    common US export shape. Decided ONCE per file: parsed row by row, a
    day-first bank export would put 03/04 in March and 25/04 in April,
    and nothing would say so."""
    for v in values:
        m = _SLASH_DATE.match(v or "")
        if not m:
            continue
        a, b = int(m.group(1)), int(m.group(2))
        # only a cell that IS a date under the order it implies gets a
        # say — one garbage cell (99/01/2020, a shifted column) must not
        # flip every ambiguous row in the file
        if a > 12 and a <= 31 and 1 <= b <= 12:
            return "dmy"
        if b > 12 and b <= 31 and 1 <= a <= 12:
            return "mdy"
    return "mdy"


_DATE_FMTS = {
    "mdy": ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d/%m/%Y", "%d/%m/%y",
            "%Y/%m/%d", "%b %d, %Y", "%d %b %Y"),
    "dmy": ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%m/%d/%y",
            "%Y/%m/%d", "%d %b %Y", "%b %d, %Y"),
}


def _parse_date(s: str, order: str = "mdy") -> dt.date:
    s = s.strip()
    for fmt in _DATE_FMTS.get(order, _DATE_FMTS["mdy"]):
        try:
            d = dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
        # a well-formed year the rest of the app cannot read (0001, 9999)
        # is refused like any other bad date, never stored
        return as_date(d)
    raise ValueError(f"unrecognized date: {s!r}")


def _parse_amount(s: str) -> float:
    s = s.strip().replace("$", "").replace(",", "")
    if s.startswith("(") and s.endswith(")"):    # accounting negatives
        s = "-" + s[1:-1]
    return float(s)


def _cell(row: dict, header: list[str], key):
    if key is None:
        return None
    if isinstance(key, int):
        return row.get(header[key]) if 0 <= key < len(header) else None
    return row.get(key)


def import_csv(conn, account_id: str, text: str, mapping: dict, *,
               amount_sign: str = "bank", batch_id: str | None = None) -> dict:
    """Parse + upsert. mapping keys: date, name (required); amount OR
    debit/credit (some banks export two one-sided columns — Debit = money
    out, Credit = money in — instead of one signed Amount; the direction is
    unambiguous, so amount_sign is ignored for those); merchant, category
    (optional). Returns counters."""
    reader = csv.DictReader(io.StringIO(text))
    header = reader.fieldnames or []
    for req in ("date", "name"):
        if mapping.get(req) is None:
            raise ValueError(f"mapping is missing required column: {req}")
    dual = mapping.get("amount") is None
    if dual and mapping.get("debit") is None and mapping.get("credit") is None:
        raise ValueError(
            "mapping is missing required column: amount (or debit/credit)")
    parsed: list[tuple] = []
    # the date convention is a property of the FILE, read off the whole
    # column before any row is parsed (the cap bounds the list)
    rows = list(capped(reader, what="transactions"))
    order = date_order(_cell(r, header, mapping["date"]) for r in rows)
    for row in rows:
        raw_date = _cell(row, header, mapping["date"])
        name = (_cell(row, header, mapping["name"]) or "").strip()
        if not raw_date or not name:
            continue
        date = _parse_date(raw_date, order)
        if dual:
            raw_debit = _cell(row, header, mapping.get("debit"))
            raw_credit = _cell(row, header, mapping.get("credit"))
            if raw_debit in (None, "") and raw_credit in (None, ""):
                continue
            # engine sign directly: debit = out (+), credit = in (−)
            amount = ((_parse_amount(raw_debit)
                       if raw_debit not in (None, "") else 0.0)
                      - (_parse_amount(raw_credit)
                         if raw_credit not in (None, "") else 0.0))
        else:
            raw_amount = _cell(row, header, mapping["amount"])
            if raw_amount in (None, ""):
                continue
            amount = _parse_amount(raw_amount)
            # bank / investment CSVs: negative = money out in the file
            # investment activity exports usually use the same convention
            if amount_sign in ("bank", "investment"):
                amount = -amount                 # file sign → engine sign
        merchant = (_cell(row, header, mapping.get("merchant")) or "").strip()
        category = (_cell(row, header, mapping.get("category")) or "").strip()
        # the mapping names no account column (see web/pages.py CSV_HEADERS —
        # date/name/amount/debit/credit/merchant/category), so the header
        # names are the only signal there is for what must not be stored
        parsed.append((date, amount, name, merchant, category,
                       scrub_identifiers(row)))
    # cross-source dedup: one-to-one, name-aware (an aggregator row at the
    # same amount within ±3 days covers ONE csv row, and only with a name
    # signal unless the dates match exactly)
    deduper = Deduper(conn, account_id, "csv", [p[0] for p in parsed])
    txns: list[Transaction] = []
    occurrence: dict[str, int] = {}
    skipped_dupes = 0
    for date, amount, name, merchant, category, raw in parsed:
        if deduper.claim(date, amount, name, merchant):
            skipped_dupes += 1
            continue
        base = f"{account_id}|{date.isoformat()}|{round(amount * 100)}|{name.lower()}"
        occurrence[base] = occurrence.get(base, 0) + 1
        tid = "csv:" + hashlib.sha1(
            f"{base}|{occurrence[base]}".encode()).hexdigest()
        # flow strings (card payment / transfer) map to spend-excluded
        # categories; anything else passes through as before. The name
        # disambiguates mixed categories (Discover "Payments and Credits")
        primary, detailed = flowmap.flow_category(category, amount, name)
        if primary is None:
            primary, detailed = category or None, None
        txns.append(Transaction(
            id=tid, account_id=account_id, date=date, amount=amount,
            name=name, merchant_name=merchant or None,
            category_primary=primary, category_detailed=detailed,
            raw=raw))
    if batch_id:
        from . import batches
        batches.tag(conn, txns, batch_id)
    added = upsert_transactions(conn, txns)
    return {"imported": added, "rows": len(txns),
            "skipped_duplicates": skipped_dupes}
