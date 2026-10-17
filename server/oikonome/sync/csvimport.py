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
import re

from ..engine.compat import as_date
from . import flowmap, reimport
from .base import (FileImportIds, Transaction, mask_long_digits,
                   drop_before_history, upsert_transactions)
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


def _identifier_tail(v):
    # text and numbers only, as the stored-row migration cuts them: a
    # nested value or a flag under an identifier-looking key is not a
    # number anyone could read back out of the last 4
    if (v not in (None, "") and isinstance(v, (str, int, float))
            and not isinstance(v, bool)):
        return str(v).strip()[-4:]
    return v


def scrub_identifiers(raw: dict) -> dict:
    """A copy of one source row with account/routing-shaped COLUMNS cut to
    their last 4 characters (see _ID_HEADERS for the why). Shared by the
    CSV-family importers — mint/ynab/qif detect their formats by column
    subset, so a bank's re-export can carry these columns into any of
    them.

    A row with more cells than its header has the extras under the key
    None, as a list (csv.DictReader's restkey): they are the last
    column's text cut at an unquoted comma, which is exactly where a
    statement prints an account number, so their long digit runs are
    masked like a stored name's."""
    out = {}
    for k, v in raw.items():
        n = _norm_header(k) if k is not None else ""
        if not n and isinstance(v, (list, tuple)):
            out[k] = [mask_long_digits(x) for x in v]
        elif n and any(t in n for t in _ID_HEADERS):
            out[k] = _identifier_tail(v)
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


# A bank's export runs to a few dozen columns at the very most. Every row
# becomes a dict over the WHOLE header (DictReader pads a short row with
# None), so the row cap alone does not bound the memory: a header of a
# thousand empty columns over the capped row count is a few MB of file and
# gigabytes of dicts, in the process every household shares. A header past
# this is refused before a row is read.
MAX_COLUMNS = 200


def check_width(fieldnames, limit: int = MAX_COLUMNS) -> None:
    """Refuse a header wider than `limit` columns, before any row is read."""
    if fieldnames is not None and len(fieldnames) > limit:
        raise ValueError(
            f"this file has {len(fieldnames):,} columns (max {limit}) — "
            f"export the transactions as your bank or app produces them, "
            f"without added columns")


# The header is parsed before anything knows which importer the file is
# for: format detection, auto-mapping and the mapping card all read it. A
# column count can only be checked once the row is a list, and by then a
# single 49 MB line of commas has already become tens of millions of cells.
# So the first lines are read under a character budget instead: a real
# header is a few hundred characters, and the mapping card's sample rows
# are only a handful of lines more.
MAX_HEADER_CHARS = 64 * 1024
MAX_SAMPLE_CHARS = 256 * 1024


class _OverBudget(ValueError):
    pass


def _lines_within(text: str, budget: int):
    """The lines of `text` (split on \\n, as io.StringIO splits them) while
    their running length stays within `budget`; raises _OverBudget at the
    first line that would pass it. Each search is bounded by the budget, so
    an oversized line costs nothing to discover."""
    pos, n = 0, len(text)
    while pos < n:
        end = text.find("\n", pos, pos + budget)
        if end < 0:
            if n - pos > budget:
                raise _OverBudget
            end = n - 1
        line = text[pos:end + 1]
        budget -= len(line)
        yield line
        pos = end + 1


def read_header(text: str) -> list[str]:
    """The CSV's first record, refused with a reason when it is longer than
    MAX_HEADER_CHARS or wider than MAX_COLUMNS — before any detector or
    mapping step builds anything over it."""
    try:
        header = next(csv.reader(_lines_within(text, MAX_HEADER_CHARS)), [])
    except _OverBudget:
        raise ValueError(
            f"the first line of this file is longer than "
            f"{MAX_HEADER_CHARS // 1024} KB — that is not a column header; "
            f"export the transactions as your bank or app produces them"
        ) from None
    check_width(header)
    return header


def sample_rows(text: str, header: list[str], n: int = 5) -> list[list[str]]:
    """Up to `n` data rows after the header, for the mapping card, each cut
    to the header's width. Reading stops early rather than failing when the
    rows run past MAX_SAMPLE_CHARS: the sample only illustrates the file."""
    rows: list[list[str]] = []
    rdr = csv.reader(_lines_within(text, MAX_SAMPLE_CHARS))
    try:
        next(rdr, None)
        for row in rdr:
            if len(rows) >= n:
                break
            rows.append(row[:len(header)])
    except (_OverBudget, csv.Error):
        pass
    return rows


# The header and sample bounds above cover only the first lines; a short
# header can still be followed by one enormous data row, and csv turns a
# 40 MB line of commas into millions of cells (a few hundred MB, measured)
# before the row cap has counted it as one. Every DATA record — every line
# of it, since a quoted field may run across many — is therefore read under
# its own character budget, the size csv itself allows a single
# field by default, far past any bank's row. A row may also carry only a
# few cells past its header — an unquoted comma or two in a memo is the
# honest case — so each row is refused past that before it becomes a dict
# and its cells are kept for the whole file.
MAX_ROW_CHARS = 128 * 1024
MAX_ROW_OVERFLOW = 50


class _BoundedLines:
    """The lines of `text` (split on \\n, as io.StringIO splits them) under
    a character budget per RECORD, not per line: a quoted field may hold
    newlines, so one record can run across any number of short lines, and
    csv would build all of its cells before the row's width is counted.
    The budget is spent by every line handed out and refilled by `reset()`,
    which the reader's tap calls before asking for each record — csv pulls
    lines only while it assembles one record, so everything spent between
    two resets belongs to that record. Each line-end search is bounded by
    what is left, so an oversized line or record costs nothing to discover."""

    def __init__(self, text: str, per_record: int = MAX_ROW_CHARS):
        self.text, self.pos, self.line_no = text, 0, 0
        self.per_record = per_record
        self.reset()

    def reset(self) -> None:
        self.left = self.per_record
        self.first_line = self.line_no + 1

    def __iter__(self):
        return self

    def __next__(self) -> str:
        text, pos, n = self.text, self.pos, len(self.text)
        if pos >= n:
            raise StopIteration
        self.line_no += 1
        end = text.find("\n", pos, pos + self.left)
        if end < 0:
            if n - pos > self.left:
                where = (f"line {self.line_no:,}"
                         if self.first_line == self.line_no else
                         f"the record starting at line {self.first_line:,}")
                raise ValueError(
                    f"{where} of this file is longer than "
                    f"{self.per_record // 1024} KB — that is not a "
                    f"transaction row; export the transactions as your bank "
                    f"or app produces them")
            end = n - 1
        line = text[pos:end + 1]
        self.left -= len(line)
        self.pos = end + 1
        return line


_INDEX_REF = re.compile(r"#(\d{1,4})")


class _Tap:
    """The csv.reader under a DictReader, keeping the last row it read as
    the list of cells. A header that repeats a name (common in bank
    exports: two Description or two Amount columns) gives the dict only
    the LAST such column; the mapping step names a column by its position
    ('#3') so either one can be chosen, and that reads the cells."""

    def __init__(self, it, lines: "_BoundedLines | None" = None):
        self.it, self.last, self.lines = it, [], lines
        self.width: int | None = None   # the header's, once it is read

    def __iter__(self):
        return self

    def __next__(self):
        if self.lines is not None:
            self.lines.reset()          # a fresh budget for this record
        self.last = next(self.it)
        if (self.width is not None
                and len(self.last) > self.width + MAX_ROW_OVERFLOW):
            raise ValueError(
                f"line {self.it.line_num:,} of this file has "
                f"{len(self.last):,} cells under a {self.width}-column "
                f"header — export the transactions as your bank or app "
                f"produces them")
        return self.last

    @property
    def line_num(self):
        return self.it.line_num


def bounded_dict_reader(text: str) -> csv.DictReader:
    """A csv.DictReader over `text` with each data row bounded before it is
    built: every record (all its lines, quoted newlines included) is read
    under MAX_ROW_CHARS, and a row carrying more
    than MAX_ROW_OVERFLOW cells past its header is refused. Without this,
    one short header over a single line of millions of commas becomes
    millions of cells (kept under DictReader's restkey) before any row cap
    has counted it as one row. The header is read here, so the caller's
    own width check on `fieldnames` still runs before any data row.
    `reader.reader` is the _Tap, whose `last` is the current row's cells."""
    lines = _BoundedLines(text)
    reader = csv.DictReader(lines)
    tap = reader.reader = _Tap(reader.reader, lines)
    header = reader.fieldnames or []
    tap.width = len(header)
    return reader


def _cell(row: dict, header: list[str], key, cells: list | None = None):
    """One mapped cell. `key` is a column name, an int position, or a
    '#<position>' reference (what the mapping card sends, so duplicate
    names stay distinct); a name that is really in the header wins over
    the reference form."""
    if key is None:
        return None
    if isinstance(key, str) and key not in header:
        m = _INDEX_REF.fullmatch(key)
        if m:
            key = int(m.group(1))
    if isinstance(key, int):
        if not 0 <= key < len(header):
            return None
        if cells is not None:
            return cells[key] if key < len(cells) else None
        return row.get(header[key])
    return row.get(key)


def import_csv(conn, account_id: str, text: str, mapping: dict, *,
               amount_sign: str = "bank", batch_id: str | None = None) -> dict:
    """Parse + upsert. mapping keys: date, name (required); amount OR
    debit/credit (some banks export two one-sided columns — Debit = money
    out, Credit = money in — instead of one signed Amount; the direction is
    unambiguous, so amount_sign is ignored for those); merchant, category
    (optional). Returns counters."""
    reader = bounded_dict_reader(text)
    tap = reader.reader
    header = reader.fieldnames or []
    check_width(header)
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
    rows = [(row, tap.last) for row in capped(reader, what="transactions")]
    order = date_order(_cell(r, header, mapping["date"], c) for r, c in rows)
    for row, cells in rows:
        def at(key, row=row, cells=cells):
            return _cell(row, header, key, cells)
        raw_date = at(mapping["date"])
        name = (at(mapping["name"]) or "").strip()
        if not raw_date or not name:
            continue
        date = _parse_date(raw_date, order)
        if dual:
            raw_debit = at(mapping.get("debit"))
            raw_credit = at(mapping.get("credit"))
            if raw_debit in (None, "") and raw_credit in (None, ""):
                continue
            # engine sign directly: debit = out (+), credit = in (−).
            # The column already says the direction, so the printed sign
            # is dropped: some card exports write credits as -150.00 in the
            # Credit column (and some banks write debits negative), and
            # subtracting that signed value would book a refund as a charge.
            debit = (_parse_amount(raw_debit)
                     if raw_debit not in (None, "") else 0.0)
            credit = (_parse_amount(raw_credit)
                      if raw_credit not in (None, "") else 0.0)
            amount = abs(debit) - abs(credit)
            # what an import before that rule stored: the printed signs
            # subtracted as they stand (see reimport.adopt_rule_change)
            signed_as = debit - credit
        else:
            raw_amount = at(mapping["amount"])
            if raw_amount in (None, ""):
                continue
            amount = _parse_amount(raw_amount)
            # bank / investment CSVs: negative = money out in the file
            # investment activity exports usually use the same convention
            if amount_sign in ("bank", "investment"):
                amount = -amount                 # file sign → engine sign
            signed_as = amount
        merchant = (at(mapping.get("merchant")) or "").strip()
        category = (at(mapping.get("category")) or "").strip()
        # the mapping names no account column (see web/pages.py CSV_HEADERS —
        # date/name/amount/debit/credit/merchant/category), so the header
        # names are the only signal there is for what must not be stored
        parsed.append((date, amount, name, merchant, category,
                       scrub_identifiers(row), signed_as))
    # cross-source dedup: one-to-one, name-aware (an aggregator row at the
    # same amount within ±3 days covers ONE csv row, and only with a name
    # signal unless the dates match exactly)
    deduper = Deduper(conn, account_id, "csv", [p[0] for p in parsed])
    txns: list[Transaction] = []
    ids = FileImportIds("csv", account_id)
    # the ids the rows had when a sign printed inside a Debit or Credit
    # cell still flipped the row, numbered over every row the way that
    # import numbered them; only a row whose amount the rule changed has
    # a candidate (see reimport.adopt_rule_change)
    old_ids = FileImportIds("csv", account_id)
    old_of: list[list[tuple[str, float]]] = []
    rule_changed = any(round(p[6] * 100) != round(p[1] * 100)
                       for p in parsed)
    skipped_dupes = 0
    absorbed = []   # lines the feed now holds whose earlier file row must go
    for date, amount, name, merchant, category, raw, signed_as in parsed:
        if deduper.claim(date, amount, name, merchant):
            skipped_dupes += 1
            if rule_changed and round(signed_as * 100) != round(amount * 100):
                # the earlier import landed (and numbered) this row: its
                # file row would stay beside the feed row that absorbs it
                oid = old_ids.id_for(date, signed_as, name)
                absorbed.append((date, amount, (name, merchant),
                    [(x, signed_as) for x in (oid, old_ids.legacy_id(oid)) if x],
                    lambda row, raw=raw, n=name, m=merchant:
                        reimport.same_line_raw(row["raw"], raw, n, m)))
            continue
        tid = ids.id_for(date, amount, name)
        if rule_changed:
            oid = old_ids.id_for(date, signed_as, name)
            old_of.append(
                [(x, signed_as) for x in (oid, old_ids.legacy_id(oid)) if x]
                if round(signed_as * 100) != round(amount * 100) else [])
        else:
            old_of.append([])
        # flow strings (card payment / transfer) map to spend-excluded
        # categories; anything else passes through as before. The name
        # disambiguates mixed categories (one issuer's "Payments and Credits")
        primary, detailed = flowmap.flow_category(category, amount, name)
        # a brokerage export's buys, sales, reinvestments and transfers are
        # the household's money changing form, read by the same rule as the
        # account's PDF statement — otherwise the CSV of one account books
        # a fund purchase as spending that its PDF books as a transfer
        if (primary is None and amount_sign == "investment"
                and flowmap.investment_flow(name) == "transfer"):
            primary, detailed = flowmap.transfer_category(amount), None
        if primary is None:
            primary, detailed = category or None, None
        txns.append(Transaction(
            id=tid, account_id=account_id, date=date, amount=amount,
            name=name, merchant_name=merchant or None,
            category_primary=primary, category_detailed=detailed,
            raw=raw))
    txns = ids.resolve(conn, txns)
    corrected = reimport.adopt_rule_change(
        conn, account_id, txns, old_of, same_line=reimport.same_line_raw)
    corrected += reimport.retire_absorbed(conn, account_id, "csv", absorbed,
                                          taken={t.id for t in txns})
    # the household's history window (Settings → how far back): rows
    # dated before it are left out and counted, never written
    txns, skipped_older = drop_before_history(conn, txns)
    if batch_id:
        from . import batches
        batches.tag(conn, txns, batch_id)
    added = upsert_transactions(conn, txns)
    return {"imported": added, "rows": len(txns),
            "skipped_duplicates": skipped_dupes, "corrected": corrected,
            "skipped_older": skipped_older}
