"""Bank/card statement PDF import — tier 3 of the import hub (text-layer
PDFs only, NO OCR; scanned statements get a clear "export CSV instead").

Same conventions as csvimport (the shape to mirror):
  * amount sign: engine convention, POSITIVE = money out.
  * ids are content-hashed with an occurrence counter
    (pdf:<sha1(account|date|cents|name|occurrence)>) — the SAME file
    imports idempotently, two identical same-day charges survive.
  * cross-source dedup via the shared one-to-one, name-aware matcher
    (sync.dedup) — a PDF overlapping an aggregator's window must not
    double-count.
  * flow rows (card payments / transfers) route through sync.flowmap so
    they land spend-excluded.

The parser is a pure heuristic over extracted text — NO bank-specific
templates. A transaction line is: a leading date (MM/DD, MM/DD/YY,
MM/DD/YYYY, 'Jan 05' styles; a second posting date right after the first is
skipped), a description, and a TRAILING amount ($1,234.56 / 1234.56 /
(negatives) / -1,234.56 / 1,234.56- / trailing CR or DR). Two trailing
amount columns = amount + running balance: the FIRST is the amount.
Summary lines (TOTAL / BALANCE / MINIMUM / APR / Page N / rates) are
skipped, never counted as failures.

Sign heuristics (kind="bank" | "statement" | "card" | "investment"):
  * bank / statement: explicit CR marker → money in; DR → money out; else
    description keywords (deposit/payroll/refund → in; withdrawal/check/
    purchase/fee → out; "transfer from" in, "transfer to" out); a printed
    minus/parens means the balance went down → money out; unsigned with no
    signal defaults to money out (statement lines are mostly debits).
    ``statement`` is the PDF label; same heuristic as bank.
  * card: printed minus/parens/CR → money in (payments & credits);
    payment/credit/refund keywords → money in; else a charge (money out).
    Payment rows carry a "credit card payment" flow hint → flowmap maps
    them to LOAN_PAYMENTS_CREDIT_CARD_PAYMENT (spend-excluded).
  * investment: brokerage / IRA / 401(k) activity PDFs — bank-like base
    plus dividend/interest/sell/contribution → money in; buy/fee/
    withdrawal → money out. Buys/sells/contributions also get a transfer
    flow hint so they stay spend-excluded like aggregator investment rows.

Year inference for MM/DD dates: a 'statement period' / 'closing date'
line anchors the year (Dec rows on a Dec→Jan statement stay in the old
year); with no anchor, the most recent plausible year (not in the future).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import re

from pypdf import PdfReader

from . import flowmap
from .base import Transaction, upsert_transactions
from .dedup import WINDOW_DAYS as DEDUP_WINDOW_DAYS  # noqa: F401 (re-export)
from .dedup import Deduper
from .rowcap import check_len

# ---- text extraction --------------------------------------------------------

_NO_TEXT_MSG = ("this PDF has no text layer (it is likely a scanned image) — "
                "OCR is not attempted; export a CSV or OFX/QFX from your "
                "bank instead")
_ENCRYPTED_MSG = ("this PDF is password-protected — save an unlocked copy "
                  "(print to PDF) or export a CSV/OFX from your bank instead")


MAX_PAGES = 500          # statements are dozens of pages at most
MAX_TEXT_BYTES = 2_000_000  # decompression-bomb ceiling on extracted text


def extract_text(data: bytes) -> str:
    """Concatenated page text of a PDF blob. Raises ValueError with a
    user-facing message for encrypted or image-only (no text layer) PDFs."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            try:
                ok = reader.decrypt("")      # empty user password still opens
            except Exception:
                ok = None
            if not ok:
                raise ValueError(_ENCRYPTED_MSG)
        if len(reader.pages) > MAX_PAGES:
            raise ValueError(
                f"this PDF has {len(reader.pages)} pages — statements are "
                f"far smaller; split it or export CSV instead")
        parts, total = [], 0
        for page in reader.pages:
            chunk = page.extract_text() or ""
            total += len(chunk)
            if total > MAX_TEXT_BYTES:       # decompression-bomb ceiling
                raise ValueError(
                    "this PDF expands past the text-size limit — export a "
                    "CSV or OFX/QFX from your bank instead")
            parts.append(chunk)
        text = "\n".join(parts)
    except ValueError:
        raise
    except Exception as e:                   # malformed / not a PDF at all
        raise ValueError(
            f"could not read this PDF ({e.__class__.__name__}) — export a "
            "CSV or OFX/QFX from your bank instead") from e
    if not text.strip():
        raise ValueError(_NO_TEXT_MSG)
    return text


# ---- heuristic statement parser --------------------------------------------

# The stored statement line rides /export verbatim, and bank statements
# routinely embed full account numbers in running text. The header-keyed
# scrub the CSV importers share cannot apply here (a PDF line has no
# headers), so any 8+ digit run is masked to its last 4 — amounts, dates
# and short check numbers all break under 8 consecutive digits, so real
# ledger content survives untouched.
_LONG_DIGITS_RE = re.compile(r"\d{8,}")


def _mask_long_digits(line: str) -> str:
    return _LONG_DIGITS_RE.sub(lambda m: "…" + m.group()[-4:], line or "")


_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}

# leading transaction date: MM/DD[/YY[YY]] or "Jan 05[, 2026]"
_DATE_RE = re.compile(
    r"""(?:
          (?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{4}|\d{2}))?
        | (?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?
          \s+(?P<d2>\d{1,2})(?:,?\s+(?P<y2>\d{4}))?
        )\b""", re.I | re.X)

# full (year-carrying) dates, for statement-period / closing-date lines
_FULL_DATE_RE = re.compile(
    r"""(?:
          (?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{4}|\d{2})
        | (?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?
          \s+(?P<d2>\d{1,2}),?\s+(?P<y2>\d{4})
        )\b""", re.I | re.X)

# money token: $1,234.56 | 1234.56 | (1,234.56) | -1,234.56 | 1,234.56- | 4.25 CR
_AMOUNT_RE = re.compile(
    r"""(?P<neg>-)?\$?(?P<open>\()?\s?\$?(?P<neg2>-)?
        (?P<num>\d{1,3}(?:,\d{3})+\.\d{2}|\d+\.\d{2})
        \s?(?P<close>\))?(?P<neg3>-)?
        (?:\s*(?P<mark>cr|dr)\b)?""", re.I | re.X)

# summary / non-transaction lines — skipped, never counted as failures
_SKIP_RE = re.compile(
    r"(sub)?total\b|\bbalance\b|\bminimum\b|\bapr\b|annual percentage"
    r"|interest (rate|charge)|periodic rate|\bpage \d+|average daily"
    r"|year[- ]to[- ]date|\bytd\b", re.I)

_PERIOD_KW = re.compile(
    r"statement period|billing period|billing cycle|statement date"
    r"|closing date|statement closing|opening date|activity (from|period)"
    r"|period:", re.I)

# bank-side direction keywords (money in checked FIRST: "check deposit"
# is a deposit)
# inbound "... from SOMEONE": Zelle/ACH/wire/transfer credits — checked
# before the out-keywords so "Zelle payment from J DOE" is money IN
_XFER_FROM_RE = re.compile(
    r"\b(transfer|xfer)\s+(in\b|from\b)"
    r"|\b(zelle|ach|wire|venmo|paypal)\b.{0,24}\bfrom\b"
    r"|\bpayment\s+(received\s+)?from\b", re.I)
_XFER_TO_RE = re.compile(r"\b(transfer|xfer)\s+(out\b|to\b)", re.I)
_BANK_IN_RE = re.compile(
    r"\b(deposit|direct dep|payroll|interest (paid|earned)|refund|reversal"
    r"|cash ?back)\b", re.I)
_BANK_OUT_RE = re.compile(
    r"\b(withdrawa?l|check|purchase|debit|payment|fee|atm|pos|bill ?pay)\b",
    re.I)
# Card-side credits (money in) for UNSIGNED amounts: bare "payment"/
# "credit" flip real charges at payment-named merchants ("UNIVERSAL
# PAYMENT CORP", "CREDIT KARMA"), so a
# credit needs settlement-shaped wording or a true refund marker.
_CARD_CREDIT_RE = re.compile(
    r"payment\s*[-–—]?\s*thank\s*you|thank\s*you.{0,16}payment"
    r"|\b(online|electronic|mobile|internet|phone) payment\b"
    r"|\bpayment received\b|\bautopay\b|\bauto-?pay\b"
    r"|\b(refund|reversal|rebate|cash ?back reward)\b"
    r"|\breturn\b(?!\s*(fee|check|item))", re.I)
_CARD_PAYMENT_RE = re.compile(
    r"payment\s*[-–—]?\s*thank\s*you|thank\s*you.{0,16}payment"
    r"|\b(online|electronic|mobile|internet|phone) payment\b"
    r"|\bpayment received\b|\bautopay\b|\bauto-?pay\b", re.I)
_TRANSFER_RE = re.compile(r"\b(transfer|xfer)\b", re.I)
# a dated BALANCE TRANSFER is a real transaction, not a summary line
_BALANCE_XFER_RE = re.compile(r"\bbalance transfer\b", re.I)
# Investment / brokerage statement activity (kind="investment")
_INV_IN_RE = re.compile(
    r"\b(dividend|div\.|interest (paid|earned|credit)|capital gain"
    r"|distribution|reinvest(ment)?|sell|sold|sale proceeds|redemption"
    r"|contribution|rollover in|deposit|transfer in|wire in)\b", re.I)
_INV_OUT_RE = re.compile(
    r"\b(buy|bought|purchase|fee|expense|commission|withdrawal"
    r"|distribution out|transfer out|wire out|management fee)\b", re.I)
_INV_FLOW_RE = re.compile(
    r"\b(buy|bought|sell|sold|contribution|reinvest|transfer|rollover"
    r"|deposit|withdrawal)\b", re.I)

# PDF amount_sign values accepted by import_pdf / parse_statement
STATEMENT_KINDS = frozenset({"bank", "statement", "card", "investment"})


def _date_or_none(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def _century(y2: int) -> int:
    return 2000 + y2 if y2 < 70 else 1900 + y2


def _date_from_match(m: re.Match) -> tuple[int, int, int | None] | None:
    """(month, day, year-or-None) from a _DATE_RE/_FULL_DATE_RE match."""
    if m["m"]:
        month, day = int(m["m"]), int(m["d"])
        year = m["y"] and (_century(int(m["y"])) if len(m["y"]) == 2
                           else int(m["y"]))
    else:
        month, day = _MONTHS[m["mon"].lower()[:3]], int(m["d2"])
        year = m["y2"] and int(m["y2"])
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return month, day, year


def _find_period(lines: list[str]) -> tuple[dt.date | None, dt.date | None]:
    """(start, end) anchors from a statement-period / closing-date line.
    A two-date period line wins; a lone closing/statement date anchors
    only the end."""
    start = end = None
    for line in lines:
        if not _PERIOD_KW.search(line):
            continue
        found = []
        for m in _FULL_DATE_RE.finditer(line):
            parts = _date_from_match(m)
            if parts and parts[2]:
                d = _date_or_none(parts[2], parts[0], parts[1])
                if d:
                    found.append(d)
        if len(found) >= 2 and start is None:
            start, end = min(found), max(found)
        elif len(found) == 1 and end is None:
            end = found[0]
    return start, end


def _infer_year(month: int, day: int, start: dt.date | None,
                end: dt.date | None) -> dt.date | None:
    """Year for an MM/DD date: inside the statement period when anchored
    (Dec rows on a Dec→Jan statement stay in the old year); otherwise the
    most recent year that doesn't put the date in the future."""
    if end:
        years = [end.year, end.year - 1]
        if start and start.year not in years:
            years.append(start.year)
        slack = dt.timedelta(days=5)
        lo = (start or end - dt.timedelta(days=45)) - slack
        hi = end + slack
        best = None
        for y in years:
            d = _date_or_none(y, month, day)
            if d is None:
                continue
            if lo <= d <= hi:
                return d
            if best is None or abs((d - end).days) < abs((best - end).days):
                best = d
        return best
    today = dt.date.today()
    d = _date_or_none(today.year, month, day)
    if d and d > today + dt.timedelta(days=7):
        d = _date_or_none(today.year - 1, month, day)
    return d


def _trailing_amounts(s: str) -> list[re.Match]:
    """Maximal run of money tokens at the END of the line (whitespace-only
    gaps). Two+ tokens = amount + running balance; interior numbers in the
    description never qualify."""
    matches = list(_AMOUNT_RE.finditer(s))
    tail: list[re.Match] = []
    pos = len(s)
    for m in reversed(matches):
        if s[m.end():pos].strip() == "":
            tail.append(m)
            pos = m.start()
        else:
            break
    tail.reverse()
    return tail


def _amount_parts(m: re.Match) -> tuple[float, str | None]:
    """(magnitude, explicit-sign) — explicit is 'cr' / 'dr' (markers) or
    'neg' (minus / accounting parens) or None."""
    mag = float(m["num"].replace(",", ""))
    mark = (m["mark"] or "").lower()
    if mark in ("cr", "dr"):
        return mag, mark
    if m["neg"] or m["neg2"] or m["neg3"] or (m["open"] and m["close"]):
        return mag, "neg"
    return mag, None


def _resolve_sign(kind: str, desc: str, mag: float,
                  explicit: str | None) -> tuple[float, str | None]:
    """(engine-signed amount, flow hint). Engine sign: positive = money
    out. Flow hints ('credit card payment' / 'transfer') feed flowmap."""
    if kind == "card":
        if explicit in ("cr", "neg"):
            amount = -mag
        elif explicit == "dr":
            amount = mag
        elif _CARD_CREDIT_RE.search(desc):
            amount = -mag
        else:
            amount = mag                       # a charge
        if amount < 0 and _CARD_PAYMENT_RE.search(desc):
            flow = "credit card payment"       # card settlement, not a refund
        elif _TRANSFER_RE.search(desc):
            flow = "transfer"
        else:
            flow = None
        return amount, flow
    if kind == "investment":
        if explicit == "cr" or explicit == "neg":
            amount = -mag
        elif explicit == "dr":
            amount = mag
        elif _INV_IN_RE.search(desc):
            amount = -mag                      # dividend, sell, contribution
        elif _INV_OUT_RE.search(desc):
            amount = mag                       # buy, fee, withdrawal
        elif _XFER_FROM_RE.search(desc) or _BANK_IN_RE.search(desc):
            amount = -mag
        elif _XFER_TO_RE.search(desc) or _BANK_OUT_RE.search(desc):
            amount = mag
        else:
            amount = mag if not explicit else (-mag if explicit == "neg"
                                               else mag)
        flow = "transfer" if _INV_FLOW_RE.search(desc) else None
        return amount, flow
    # bank / statement (checking & savings PDFs)
    if explicit == "cr":
        amount = -mag
    elif explicit == "dr":
        amount = mag
    elif _XFER_FROM_RE.search(desc):
        amount = -mag
    elif _XFER_TO_RE.search(desc):
        amount = mag
    elif _BANK_IN_RE.search(desc):
        amount = -mag
    elif _BANK_OUT_RE.search(desc):
        amount = mag
    else:
        # printed minus/parens = balance went down = money out; unsigned
        # with no signal defaults to money out (statement lines are
        # mostly debits)
        amount = mag
    flow = "transfer" if _TRANSFER_RE.search(desc) else None
    return amount, flow


def _parse_line(s: str, dm: re.Match, start: dt.date | None,
                end: dt.date | None, kind: str) -> dict | None:
    parts = _date_from_match(dm)
    if parts is None:
        return None
    month, day, year = parts
    date = (_date_or_none(year, month, day) if year
            else _infer_year(month, day, start, end))
    if date is None:
        return None
    # a second date right after the first is a posting date — skip it
    offset = dm.end()
    while offset < len(s) and s[offset].isspace():
        offset += 1
    dm2 = _DATE_RE.match(s, offset)
    if dm2:
        offset = dm2.end()
    tail = _trailing_amounts(s)
    if not tail or tail[0].start() <= offset:
        return None
    # Multi-column layouts: FIRST trailing token = amount, LAST = running
    # balance. Three-column statements print 0.00 in the unused
    # withdrawals/deposits cell — a zero token is never the amount when a
    # nonzero one follows. Foreign-currency
    # rows (foreign amount + billed amount, no balance) remain ambiguous;
    # the parser picks the first and the confidence machinery is the guard.
    # …but only in a THREE-column layout. With exactly two trailing tokens
    # the first is the amount even when it is 0.00 (a waived fee, a
    # reversed authorization) — skipping it took the running balance as
    # the transaction.
    amt_match = (next((m for m in tail if float(m["num"].replace(",", ""))),
                      tail[0]) if len(tail) >= 3 else tail[0])
    desc = s[offset:amt_match.start()].strip(" \t.*:")
    if not re.search(r"[a-zA-Z]", desc):
        return None
    mag, explicit = _amount_parts(amt_match)
    amount, flow = _resolve_sign(kind, desc, mag, explicit)
    return {"date": date, "name": desc, "amount": round(amount, 2),
            "flow": flow, "line": s, "had_year": year is not None}


def parse_statement(text: str, *, kind: str = "bank") -> dict:
    """Heuristic parse of extracted statement text (no bank templates).

    kind: 'bank'/'statement' (checking/savings), 'card' (credit), or
    'investment' (brokerage/IRA activity) — drives the sign heuristic;
    see module docstring.

    Returns {"rows": [{date, name, amount, flow, line}], "confidence": 0-1
    (fraction of candidate lines parsed + a date-monotonicity bonus),
    "period": (start, end) or None, "warnings": [...]}. Amounts are
    engine-signed (positive = money out)."""
    if kind not in STATEMENT_KINDS:
        raise ValueError(f"unknown statement kind: {kind!r}")
    if kind == "statement":
        kind = "bank"
    lines = text.splitlines()
    start, end = _find_period(lines)
    if end is None:
        # No statement period found. Anchor yearless rows to the latest
        # FULL date anywhere in the file rather than to the wall clock: a
        # guess that depends on the day the file is imported dates a
        # decade of history differently on every re-import and breaks the
        # dedup that makes re-importing safe. Only a file with no year at
        # all falls back to today (and says so in its warnings).
        full = []
        for raw_line in lines:
            dm = _DATE_RE.match(raw_line.strip())
            parts = _date_from_match(dm) if dm else None
            if parts and parts[2]:
                d = _date_or_none(parts[2], parts[0], parts[1])
                if d:
                    full.append(d)
        if full:
            end = max(full)
    rows: list[dict] = []
    candidates = 0
    yearless = False
    dated_summaries = 0
    for raw_line in lines:
        s = raw_line.strip()
        if not s:
            continue
        if _SKIP_RE.search(s) and not _BALANCE_XFER_RE.search(s):
            # summary/total lines are not candidates — but a DATED line
            # with a money token may be a real transaction the skip words
            # ate ("01/05 TOTAL WINE 89.20"); surface it instead of
            # silently dropping
            if _DATE_RE.match(s) and _AMOUNT_RE.search(s):
                dated_summaries += 1
            continue
        dm = _DATE_RE.match(s)
        if not dm:
            continue
        candidates += 1
        row = _parse_line(s, dm, start, end, kind)
        if row is None:
            continue
        yearless = yearless or not row.pop("had_year")
        rows.append(row)
    warnings: list[str] = []
    if candidates == 0:
        warnings.append("no transaction-like lines found — this may not be "
                        "a bank or card statement")
    elif len(rows) < candidates:
        warnings.append(f"{candidates - len(rows)} line(s) looked like "
                        "transactions but could not be parsed")
    if yearless and end is None:
        warnings.append("no statement period or closing date found — the "
                        "year for dated lines was guessed")
    if dated_summaries:
        warnings.append(f"{dated_summaries} dated line(s) containing "
                        "summary words (balance/total/minimum) were "
                        "skipped — check the statement's totals against "
                        "the imported rows")
    base = len(rows) / candidates if candidates else 0.0
    bonus = 0.0
    if len(rows) >= 2:
        pairs = list(zip(rows, rows[1:]))
        mono = sum(1 for a, b in pairs if b["date"] >= a["date"]) / len(pairs)
        bonus = 0.1 * mono
    return {"rows": rows,
            "confidence": round(min(1.0, base + bonus), 3),
            "period": (start, end) if start and end else None,
            "warnings": warnings}


# ---- import -----------------------------------------------------------------


def import_pdf(conn, account_id: str, data: bytes, *,
               amount_sign: str = "bank", batch_id: str | None = None) -> dict:
    """Extract → parse → dedup → upsert one statement PDF into
    `account_id`. amount_sign: bank|statement|card|investment (parser
    sign heuristic). Returns csvimport-shaped counters plus the parser's
    confidence, warnings and period."""
    if amount_sign not in STATEMENT_KINDS:
        raise ValueError(f"unknown amount_sign: {amount_sign!r}")
    text = extract_text(data)
    parsed = parse_statement(text, kind=amount_sign)
    # same row cap as every other importer — MAX_PAGES/MAX_TEXT_BYTES bound
    # the PDF itself, but 2 MB of dense text still parses to far more
    # transaction lines than any sibling importer would accept, and each
    # row costs a dedup scan and a synchronous upsert inside the request
    rows = check_len(parsed["rows"], what="transactions")
    deduper = Deduper(conn, account_id, "pdf", [r["date"] for r in rows])
    txns: list[Transaction] = []
    occurrence: dict[str, int] = {}
    skipped_dupes = 0
    for r in rows:
        if deduper.claim(r["date"], r["amount"], r["name"]):
            skipped_dupes += 1
            continue
        base = (f"{account_id}|{r['date'].isoformat()}|"
                f"{round(r['amount'] * 100)}|{r['name'].lower()}")
        occurrence[base] = occurrence.get(base, 0) + 1
        tid = "pdf:" + hashlib.sha1(
            f"{base}|{occurrence[base]}".encode()).hexdigest()
        # flow rows (card payments / transfers) land spend-excluded
        primary, detailed = flowmap.flow_category(r["flow"], r["amount"])
        txns.append(Transaction(
            id=tid, account_id=account_id, date=r["date"],
            amount=r["amount"], name=r["name"],
            category_primary=primary, category_detailed=detailed,
            raw={"line": _mask_long_digits(r["line"]),
                 "flow": r["flow"]}))
    if batch_id:
        from . import batches
        batches.tag(conn, txns, batch_id)
    added = upsert_transactions(conn, txns)
    return {"imported": added, "rows": len(txns),
            "skipped_duplicates": skipped_dupes,
            "confidence": parsed["confidence"],
            "warnings": parsed["warnings"],
            "period": parsed["period"]}
