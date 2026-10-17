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

Sign: the statement's own signals first, because they are positive
evidence and keyword rules are not. A printed minus, parentheses or a
CR/DR marker wins. Then, on a
bank or brokerage statement, the running balance: when exactly one
assignment of in/out to the unsigned amounts since the last printed
balance makes the balances add up to the cent, that is their sign (a
card's balance is what is owed and is not read). Then the column: when
the PDF draws its cells as separate runs under a header naming a
withdrawals/debits and a deposits/credits column (or one of them beside
a balance column), an unsigned amount takes the sign of the column it
sits under. Then the section heading the line sits under (Withdrawals,
Checks paid, Deposits and additions, …). Only where all of these are
silent do the keyword heuristics apply, and the warnings count those
lines:

Sign heuristics (kind="bank" | "statement" | "card" | "investment"):
  * bank / statement: explicit CR marker → money in; DR → money out; else
    description keywords (deposit/payroll/refund/cash-back reward → in;
    withdrawal/check/purchase/fee → out; "transfer from" in, "transfer
    to" out; cash back taken with a purchase is money out); a printed
    minus/parens means the balance went down → money out; unsigned with no
    signal defaults to money out (statement lines are mostly debits).
    ``statement`` is the PDF label; same heuristic as bank.
  * card: printed minus/parens/CR → money in (payments & credits);
    payment/credit/refund keywords → money in; else a charge (money out).
    Payment rows carry a "credit card payment" flow hint → flowmap maps
    them to LOAN_PAYMENTS_CREDIT_CARD_PAYMENT (spend-excluded).
  * investment: brokerage / IRA / 401(k) activity PDFs — bank-like base
    plus dividend/interest/sell/contribution → money in; buy/fee/
    withdrawal and a retirement account's own distribution (IRA, RMD,
    Roth, "distribution out" — never a dividend or capital gain a fund
    distributes) → money out; a printed minus/parens is money out, as on
    a bank statement. Buys, purchases and sales of a
    security, sells, exchanges, reinvestments, distributions,
    contributions and transfers also get a transfer flow hint so they stay
    spend-excluded like aggregator investment rows; fees and commissions
    stay spending, and so do a cash-management account's card, register,
    ATM and bill-pay lines, while a direct deposit of pay stays income
    (flowmap.investment_flow).

Year inference for MM/DD dates: a 'statement period' / 'closing date'
line anchors the year (Dec rows on a Dec→Jan statement stay in the old
year); with no anchor, the most recent plausible year (not in the future).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re

from ..engine.compat import as_date
from . import flowmap, reimport
from .base import (FileImportIds, Transaction, mask_long_digits,
                   drop_before_history, upsert_transactions)
from .dedup import WINDOW_DAYS as DEDUP_WINDOW_DAYS  # noqa: F401 (re-export)
from .dedup import Deduper
from .rowcap import check_len

# ---- text extraction --------------------------------------------------------

# Extraction itself — pypdf, its budgets, and the child process it runs in
# — lives in pdf_sandbox, which imports nothing of the app so that the
# child can load it alone. The names below are re-exported for callers
# and tests that read them from here.
from .pdf_sandbox import (DECODER_LIMITS, LAYOUT_NEWLINE_LIMIT,  # noqa: F401
                          LAYOUT_SECONDS, LAYOUT_SPACE_LIMIT,
                          LAYOUT_TEXT_BYTES, LAYOUT_TEXT_OPS,
                          MAX_FONTS_PER_PAGE, MAX_PAGES, MAX_TEXT_BYTES,
                          PAGE_CONTENT_LIMIT, STREAM_LIMIT, PdfReader,
                          _charge_font_names, _page_content_bytes,
                          first_page_within_budget)
from .pdf_sandbox import run as _run_extraction

# A statement line is a date, a description and a few amounts — well under
# 200 characters. Anything far longer is not a transaction, and every
# heuristic regex below is only cheap on lines of that ordinary size, so
# a longer line is set aside before any of them runs.
MAX_LINE_CHARS = 1_000


def extract_text(data: bytes) -> str:
    """Concatenated page text of a PDF blob. Raises ValueError with a
    user-facing message for encrypted or image-only (no text layer) PDFs,
    and for any PDF whose content would expand past the limits in
    pdf_sandbox — the extraction runs in a child process there, so what a
    PDF makes pypdf do cannot cost this process its memory or its time."""
    return _run_extraction(data, with_layout=False)[0]


# The hint for a line on a page whose layout pass the time budget skipped:
# its column is not "none", it is not known (see pdf_sandbox._extract).
UNKNOWN_SIGN = "unknown"


def extract_statement(data: bytes) -> tuple[str, list[str | None] | None]:
    """The text a statement parse reads, plus — when the statement prints
    its figures under a column header — a sign hint per line: 'in' or
    'out' for a line whose amount sits under a deposits/credits or
    withdrawals/debits column, None where the layout says nothing.

    The text is the plain extraction, exactly what extract_text returns:
    a row's name, its stored line and so its id are cut from it, and a
    statement imported again must produce the same ids it did before
    hints existed. The hints are read from pypdf's LAYOUT rendering
    (fixed-width, every run at the character column its position on the
    page maps to) and carried onto the plain line holding the same
    characters — compared with all whitespace removed, on the same page.
    A layout line with no plain twin (a sidebar drawn on the same row, a
    line the two modes split differently) gives no hint, so the keywords
    decide that line, as they do for a PDF with no layout at all."""
    text, plain_parts, laid_parts = _run_extraction(data, with_layout=True)
    if not any(laid_parts) and False not in laid_parts:
        return text, None
    # each page's hinted layout lines, keyed by their characters; a page
    # the time budget skipped (False) marks every line on it UNKNOWN_SIGN
    twins: list[dict[str, list[str]] | None] = []
    for laid in laid_parts:
        if laid is False:
            twins.append(None)
            continue
        page: dict[str, list[str]] = {}
        laid_lines = (laid or "").splitlines()
        for ln, h in zip(laid_lines, _column_hints(laid_lines)):
            if h is not None:
                page.setdefault(_squeeze(ln), []).append(h)
        twins.append(page)
    # walk the joined text's own lines, knowing which page each starts on
    starts, off = [], 0
    for chunk in plain_parts:
        starts.append(off)
        off += len(chunk) + 1
    hints: list[str | None] = []
    page_no = pos = 0
    for ln in text.splitlines(keepends=True):
        while page_no + 1 < len(starts) and pos >= starts[page_no + 1]:
            page_no += 1
        pos += len(ln)
        page_twins = twins[page_no] if page_no < len(twins) else {}
        if page_twins is None:
            hints.append(UNKNOWN_SIGN)
            continue
        queue = (page_twins.get(_squeeze(ln))
                 if len(ln) <= MAX_LINE_CHARS else None)
        hints.append(queue.pop(0) if queue else None)
    return text, hints


def _squeeze(line: str) -> str:
    return "".join(line.split())


# the words a statement prints over its money columns
_COL_RE = re.compile(
    r"(?P<out>\b(?:withdrawals?|debits?|payments|charges|subtractions"
    r"|purchases|money out|paid out|amount debited)\b)"
    r"|(?P<in>\b(?:deposits?|credits?|additions|money in|paid in"
    r"|amount credited)\b)"
    r"|(?P<bal>\bbalance\b)", re.I)


def _column_hints(lines: list[str]) -> list[str | None]:
    """A sign hint per line of ONE page's layout rendering, from the column
    its amount sits under.

    A header line names at least one money-out and one money-in column
    (or one of them beside a balance column) and carries no money token;
    the centre character of each word becomes a column, and the columns
    carry on until the next header. A later line's chosen amount — the
    first of its trailing money tokens, as the parser picks it — is read
    by the nearest column centre, when that is within a dozen characters
    and is not the balance column. A statement that prints one Amount
    column names no direction and gets no hint. The layout rendering
    scales each page on its own, so a page that prints no header of its
    own gets no hints, and the keywords decide. A line longer than any
    statement line is not read at all (the parser skips it too)."""
    hints: list[str | None] = []
    cols: dict[str, float] | None = None
    for ln in lines:
        if len(ln) > MAX_LINE_CHARS:
            hints.append(None)
            continue
        words: dict[str, float] = {}
        for m in _COL_RE.finditer(ln):
            kind = m.lastgroup
            if kind and kind not in words:
                words[kind] = (m.start() + m.end()) / 2
        if words and not _AMOUNT_RE.search(ln) and (
                ("out" in words and "in" in words)
                or ("bal" in words and ("out" in words or "in" in words))):
            cols = words
            hints.append(None)
            continue
        if not cols:
            hints.append(None)
            continue
        tail = _trailing_amounts(ln)
        if not tail:
            hints.append(None)
            continue
        amt = _chosen_amount(tail)
        centre = (amt.start() + amt.end()) / 2
        dist, kind = min((abs(centre - x), k) for k, x in cols.items())
        hints.append(None if dist > 12 or kind == "bal" else kind)
    return hints



# ---- heuristic statement parser --------------------------------------------

# The stored statement line rides /export verbatim, and bank statements
# routinely embed full account numbers in running text. The header-keyed
# scrub the CSV importers share cannot apply here (a PDF line has no
# headers), so any 8+ digit run is masked to its last 4 — amounts, dates
# and short check numbers all break under 8 consecutive digits, so real
# ledger content survives untouched. The description cut from the line
# (the row's name) gets the same cut; the rule itself lives beside the
# upsert, which applies it to every file importer's names.
def _mask_long_digits(line: str) -> str:
    return mask_long_digits(line or "")


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
# The number may only START where a digit run starts (no digit or group
# comma before it). Without that, a long digit run with no decimal point
# is tried from every position inside it, and each try scans to the end
# of the run before failing — quadratic in the run's length.
_AMOUNT_RE = re.compile(
    r"""(?P<neg>-)?\$?(?P<open>\()?\s?\$?(?P<neg2>-)?
        (?<![\d,])
        (?P<num>\d{1,3}(?:,\d{3})+\.\d{2}|\d{1,15}\.\d{2})
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
# A bank's word for money arriving. "Cash back" is money in only as a
# reward — credited, redeemed, earned, a bonus. Bare, it is cash taken at
# a register with a purchase ("POS PURCHASE W/CASH BACK", "DEBIT CARD
# CASHBACK", "CASH BACK <store>"): money out, which the out-words or the
# money-out default then read.
_BANK_IN_RE = re.compile(
    r"\b(deposit|direct dep|payroll|interest (paid|earned)|refund|reversal)\b"
    r"|\bcash\s?back\s+(rewards?|bonus(es)?|credits?|deposits?"
    r"|redemptions?|redeemed|earned)\b", re.I)
# Money-out lines whose wording carries a money-in keyword, checked before
# _BANK_IN_RE. Each alternative is a bank's own fixed descriptor for a
# deposited item the bank took back, never a loose keyword: anything
# looser catches real inflows ("security deposit return", "returned
# deposit from <landlord>"), so a new shape is added here only by its
# exact wording.
_BANK_IN_UNDONE_RE = re.compile(
    r"\bdeposit\s+reversal\b"
    r"|\bdeposited\s+item\s+returned\b|\breturned\s+deposit(ed)?\s+item\b",
    re.I)
# The two above as an earlier reading had them, kept only to recompute the
# ids that reading stored (_legacy_amount) — never used to sign a row.
_LEGACY_BANK_IN_RE = re.compile(
    r"\b(deposit|direct dep|payroll|interest (paid|earned)|refund|reversal"
    r"|cash ?back)\b", re.I)
_LEGACY_BANK_IN_UNDONE_RE = re.compile(
    r"\bpurchase\b(.*\s)?with\s+cash\s?back\b"
    r"|\bdeposit\s+reversal\b"
    r"|\bdeposited\s+item\s+returned\b|\breturned\s+deposit(ed)?\s+item\b",
    re.I)
_BANK_OUT_RE = re.compile(
    r"\b(withdrawa?l|check|purchase|debit|payment|fee|atm|pos|bill ?pay)\b",
    re.I)
# Card-side credits (money in) for UNSIGNED amounts: bare "payment"/
# "credit" flip real charges at payment-named merchants ("EXAMPLE
# PAYMENT CORP", "CREDIT CHECK CO"), so a
# credit needs settlement-shaped wording or a true refund marker.
_CARD_CREDIT_RE = re.compile(
    r"payment\s*[-–—]?\s*thank\s*you|thank\s*you.{0,16}payment"
    r"|\b(online|electronic|mobile|internet|phone) payment\b"
    r"|\bpayment received\b|\bautopay\b|\bauto-?pay\b"
    r"|\b(refund|reversal|rebate|cash ?back rewards?)\b"
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
# A retirement account paying out, checked before _INV_IN_RE (whose bare
# "distribution" would read it as money in): "distribution out" and a
# distribution named by the account or the rule that paid it. Income
# that a fund distributes to its holder — a dividend, a capital gain,
# interest, a reinvestment — is never this, whatever account it names
# (_INV_INCOME_RE vetoes it), and "distribution paid/to" is left out on
# purpose: funds print "CAP GAIN DISTRIBUTION PAID" for income.
_INV_DISTRIBUTION_OUT_RE = re.compile(
    r"\bdistribution\s+out\b"
    r"|\b(ira|roth(\s+ira)?|sep(\s+ira)?|simple(\s+ira)?|rmd"
    r"|required\s+minimum|normal|early|premature|qualified|non-?qualified"
    r"|periodic|systematic|401\(?k\)?|403\(?b\)?|retirement)"
    r"\s+distribution\b", re.I)
_INV_INCOME_RE = re.compile(
    r"\b(dividends?|capital gains?|cap gains?|interest|income"
    r"|reinvest\w*)\b|\bdiv\.", re.I)
# Which activity is a transfer rather than spending is flowmap's call
# (flowmap.investment_flow), so the PDF and CSV of one account agree.
# A bare "SALE" is money in only beside a word that only a trade prints
# (flowmap.sale_of_security: "SALE 10 SHS VANGUARD 500 INDEX FUND"): on a
# cash account's card or register "SALE" is shopping ("SALE EXAMPLE OUTFITTERS",
# "SALE SAMPLE GOODS CO"), and reading it as money in would turn spending
# into income.
_INV_SALE_RE = re.compile(r"\bsale\b", re.I)

# PDF amount_sign values accepted by import_pdf / parse_statement
STATEMENT_KINDS = frozenset({"bank", "statement", "card", "investment"})


def _date_or_none(y: int, m: int, d: int) -> dt.date | None:
    # as_date also refuses a real calendar date in a year the app cannot
    # read, so such a line is skipped like any unparseable one
    try:
        return as_date(dt.date(y, m, d))
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


# A lone "opening date" names where the cycle STARTS (or, on some
# statements, when the account was opened) — never the period end.
_OPENING_KW = re.compile(r"opening date", re.I)
# The longest gap between an opening date and a closing date that still
# reads as one statement cycle; anything wider is an account-opening date.
_MAX_CYCLE = dt.timedelta(days=100)


def _find_period(lines: list[str]) -> tuple[dt.date | None, dt.date | None]:
    """(start, end) anchors from statement-period / closing-date lines.
    A two-date period line wins. Otherwise a lone opening date is the
    start and a lone closing/statement date the end: an opening date read
    as the end dates the January rows of a Dec→Jan cycle a year early. An
    opening date with no closing date anchors nothing (it may be when the
    account was opened, years back), and one too far before the closing
    date to be a cycle start is dropped for the same reason."""
    open_d = close_d = None
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
        if len(found) >= 2:
            return min(found), max(found)
        if len(found) == 1:
            opening = (_OPENING_KW.search(line)
                       and not re.search(r"closing|statement date", line,
                                         re.I))
            if opening and open_d is None:
                open_d = found[0]
            elif not opening and close_d is None:
                close_d = found[0]
    if close_d is None:
        return None, None
    if open_d is not None and open_d > close_d:
        # the two labels were swapped or mis-read; order them as a range
        open_d, close_d = close_d, open_d
    if open_d is not None and close_d - open_d > _MAX_CYCLE:
        open_d = None
    return open_d, close_d


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
                  explicit: str | None,
                  column: str | None = None) -> tuple[float, str | None]:
    """(engine-signed amount, flow hint). Engine sign: positive = money
    out. Flow hints ('credit card payment' / 'transfer') feed flowmap.

    `column` is the statement's own answer for an UNSIGNED amount — 'in'
    or 'out' from its running balance, the column it was drawn under
    (extract_statement) or the section heading over it (parse_statement
    merges them). It is the positive signal the keywords never were: the
    statement itself says which way the figure went, so 'POS PURCHASE
    W/CASH BACK' under Withdrawals is money out and 'IRA DISTRIBUTION'
    under Deposits is money in, whatever the words suggest. A printed
    sign or CR/DR marker still wins over it — a statement that prints
    one is saying the same thing more directly."""
    if explicit is None and column in ("in", "out"):
        amount = -mag if column == "in" else mag
        if kind == "card":
            flow = ("credit card payment"
                    if amount < 0 and _CARD_PAYMENT_RE.search(desc)
                    else "transfer" if _TRANSFER_RE.search(desc) else None)
        elif kind == "investment":
            flow = flowmap.investment_flow(desc)
        else:
            flow = "transfer" if _TRANSFER_RE.search(desc) else None
        return amount, flow
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
        # Bank-like: a CR/DR marker is unambiguous, then the activity
        # keywords decide. A printed minus or parentheses carries the bank
        # meaning (cash left the account) — brokerage activity statements
        # print buys, withdrawals and fees negative, so reading it as money
        # in would flip the same buy depending on whether a sign was shown.
        if explicit == "cr":
            amount = -mag
        elif explicit in ("dr", "neg"):
            amount = mag
        elif (_INV_DISTRIBUTION_OUT_RE.search(desc)
              and not _INV_INCOME_RE.search(desc)):
            amount = mag                       # the account paying out
        elif _INV_IN_RE.search(desc) or (
                _INV_SALE_RE.search(desc)
                and flowmap.sale_of_security(desc)):
            amount = -mag                      # dividend, sell, contribution
        elif _INV_OUT_RE.search(desc):
            amount = mag                       # buy, fee, withdrawal
        elif _XFER_FROM_RE.search(desc) or _BANK_IN_RE.search(desc):
            amount = -mag
        else:
            # transfer-to / bank-out keywords, or no signal at all: money out
            amount = mag
        return amount, flowmap.investment_flow(desc)
    # bank / statement (checking & savings PDFs). A printed minus or
    # parentheses means the balance went down, so it is money out before
    # any keyword is read: the wording of a debit can carry an inbound
    # word ("cash back", "deposit reversal") but the sign cannot lie.
    # Unsigned lines with no balance, column or heading to go by are read
    # by keyword, where only exact debit descriptors may override an
    # inbound word (_BANK_IN_UNDONE_RE).
    if explicit == "cr":
        amount = -mag
    elif explicit in ("dr", "neg"):
        amount = mag
    elif _XFER_FROM_RE.search(desc):
        amount = -mag
    elif _XFER_TO_RE.search(desc):
        amount = mag
    elif _BANK_IN_UNDONE_RE.search(desc):
        amount = mag                           # deposit taken back out
    elif _BANK_IN_RE.search(desc):
        amount = -mag
    elif _BANK_OUT_RE.search(desc):
        amount = mag
    else:
        # unsigned with no signal defaults to money out (statement lines
        # are mostly debits)
        amount = mag
    flow = "transfer" if _TRANSFER_RE.search(desc) else None
    return amount, flow


def _legacy_amount(kind: str, desc: str, mag: float, explicit: str | None,
                   column: str | None, era: str) -> float:
    """The amount an earlier reading of the parser gave a line — only to
    recompute the id that reading stored, so a re-import corrects that
    row in place (see import_pdf). Never used to sign a row.

    era "column": a line's column hint, then the keywords as they stood
    before the running balance, the section headings and the narrower
    cash-back and distribution words. "keywords": the same without the
    columns. "minus_in": as "keywords", but an investment line printed
    with a minus or parentheses read as money in. "pre_minus": a printed
    minus read by the wording like an unsigned line, and no exact debit
    descriptors before the inbound words."""
    if era == "column" and explicit is None and column in ("in", "out"):
        return -mag if column == "in" else mag
    if kind == "card":
        return _resolve_sign(kind, desc, mag, explicit)[0]
    if explicit == "cr":
        return -mag
    if explicit == "dr":
        return mag
    if explicit == "neg":
        if era == "minus_in" and kind == "investment":
            return -mag
        if era != "pre_minus":
            return mag
    if kind == "investment":
        if _INV_IN_RE.search(desc) or (
                _INV_SALE_RE.search(desc) and flowmap.sale_of_security(desc)):
            return -mag
        if _INV_OUT_RE.search(desc):
            return mag
        if _XFER_FROM_RE.search(desc) or _LEGACY_BANK_IN_RE.search(desc):
            return -mag
        return mag
    if _XFER_FROM_RE.search(desc):
        return -mag
    if _XFER_TO_RE.search(desc):
        return mag
    if era != "pre_minus" and _LEGACY_BANK_IN_UNDONE_RE.search(desc):
        return mag
    if _LEGACY_BANK_IN_RE.search(desc):
        return -mag
    return mag


# the readings _legacy_amount knows, newest first (see import_pdf)
_PRIOR_ERAS = ("column", "keywords", "minus_in", "pre_minus")


def _chosen_amount(tail: list[re.Match]) -> re.Match:
    """The amount among a line's trailing money tokens. Multi-column
    layouts print the amount FIRST and the running balance LAST.
    Three-column statements print 0.00 in the unused withdrawals/deposits
    cell — a zero token is never the amount when a nonzero one follows.
    Foreign-currency rows (foreign amount + billed amount, no balance)
    remain ambiguous; the first is picked and the confidence machinery is
    the guard. …but only in a THREE-column layout: with exactly two
    trailing tokens the first is the amount even when it is 0.00 (a
    waived fee, a reversed authorization) — skipping it took the running
    balance as the transaction."""
    if len(tail) >= 3:
        return next((m for m in tail if float(m["num"].replace(",", ""))),
                    tail[0])
    return tail[0]


def _scan_line(s: str, dm: re.Match, start: dt.date | None,
               end: dt.date | None,
               prior: tuple[dt.date | None, dt.date | None] | None
               ) -> dict | None:
    """One dated statement line, read up to its sign: date, description,
    magnitude, printed sign and — when it prints one — running balance."""
    parts = _date_from_match(dm)
    if parts is None:
        return None
    month, day, year = parts
    date = (_date_or_none(year, month, day) if year
            else _infer_year(month, day, start, end))
    if date is None:
        return None
    # the date the earlier reading of the period anchors gave this yearless
    # line, when it differs — import_pdf re-dates the row that reading
    # stored, if it can prove the row is this statement's
    prior_date = (_infer_year(month, day, *prior)
                  if prior is not None and not year else None)
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
    amt_match = _chosen_amount(tail)
    desc = s[offset:amt_match.start()].strip(" \t.*:")
    if not re.search(r"[a-zA-Z]", desc):
        return None
    mag, explicit = _amount_parts(amt_match)
    balance = None
    if len(tail) >= 2 and tail[-1] is not amt_match:
        bmag, bsign = _amount_parts(tail[-1])
        balance = round((-bmag if bsign in ("neg", "dr") else bmag) * 100)
    out = {"date": date, "name": desc, "mag": mag, "explicit": explicit,
           "balance": balance, "line": s, "had_year": year is not None}
    if prior_date is not None and prior_date != date:
        out["prior_date"] = prior_date
    return out


# ---- the statement's own signals -------------------------------------------

# A line that states the balance the activity starts from.
_SEED_RE = re.compile(
    r"\b(beginning|opening|previous|starting)\s+balance\b"
    r"|\bbalance\s+(forward|brought\s+forward)\b", re.I)
# The most unknown signs one balance may settle (2**8 assignments tried).
_MAX_UNKNOWN = 8


def _solve(events: list[tuple]) -> tuple[dict[int, str], int]:
    """Signs the running balance proves, walking `events` in order, and
    how many balance groups were consistent.

    An event is ("seed", cents) for an opening balance, or ("row", i,
    magnitude cents, fixed delta cents or None, balance cents or None).
    The unsigned rows since the last balance form a group; a group is
    settled only when exactly ONE in/out assignment of them makes the
    previous balance plus every amount equal this balance to the cent.
    A misread quantity or price column does not add up, so it settles
    nothing — never a wrong sign."""
    hints: dict[int, str] = {}
    solved = 0
    prev: int | None = None
    pend: list[tuple[int, int]] = []
    fixed = 0
    for ev in events:
        if ev[0] == "seed":
            prev, pend, fixed = ev[1], [], 0
            continue
        _, i, mag, delta, bal = ev
        if delta is None:
            pend.append((i, mag))
        else:
            fixed += delta
        if bal is None:
            continue
        if prev is not None and len(pend) <= _MAX_UNKNOWN:
            target = bal - prev - fixed
            found = []
            for mask in range(1 << len(pend)):
                total = sum(m if mask >> k & 1 else -m
                            for k, (_, m) in enumerate(pend))
                if total == target:
                    found.append(mask)
                    if len(found) > 1:
                        break
            if len(found) == 1:
                solved += 1
                for k, (row, _) in enumerate(pend):
                    hints[row] = "in" if found[0] >> k & 1 else "out"
        prev, pend, fixed = bal, [], 0
    return hints, solved


def _balance_hints(events: list[tuple], kind: str) -> dict[int, str]:
    """{row index: 'in' | 'out'} from the running balance a bank or
    investment statement prints. A card's balance is what is owed, so it
    moves the other way, and is not read at all.

    Statements list oldest-first or newest-first; the events are solved
    both ways and the order that settles more groups is the statement's.
    When neither settles more, the balance says nothing about the file."""
    if kind not in ("bank", "investment"):
        return {}
    fwd, n_fwd = _solve(events)
    back, n_back = _solve(events[::-1])
    if n_fwd > n_back:
        return fwd
    if n_back > n_fwd:
        return back
    return {}


_HEAD_TAIL = r"\s*(?:\(\s*continued\s*\)|-?\s*continued)?\s*:?\s*$"
_BANK_OUT_HEAD_RE = re.compile(
    r"^\s*(?:(?:(?:atm\s*(?:&|and)\s*debit\s+card|electronic|other|card|pos)"
    r"\s+)?withdrawals?(?:\s+and\s+(?:other\s+)?(?:subtractions|debits))?"
    r"|checks(?:\s+paid)?"
    r"|(?:other\s+)?debits(?:\s+and\s+other\s+subtractions)?"
    r"|(?:service\s+)?fees(?:\s+and\s+charges)?"
    r"|service\s+charges)" + _HEAD_TAIL, re.I)
_BANK_IN_HEAD_RE = re.compile(
    r"^\s*(?:deposits(?:\s+and\s+(?:other\s+)?(?:additions|credits))?"
    r"|(?:other\s+|electronic\s+)?(?:deposits|credits)"
    r"|additions)" + _HEAD_TAIL, re.I)
_INV_OUT_HEAD_RE = re.compile(
    r"^\s*(?:withdrawals(?:\s+and\s+distributions)?|distributions)"
    + _HEAD_TAIL, re.I)
_INV_IN_HEAD_RE = re.compile(
    r"^\s*(?:deposits|contributions)" + _HEAD_TAIL, re.I)
# a heading over activity of both directions ends any section
_BOTH_HEAD_RE = re.compile(
    r"^\s*(?:account\s+)?(?:transactions?|activity)"
    r"(?:\s+(?:history|details?|summary))?" + _HEAD_TAIL, re.I)
_SECTION_END_RE = re.compile(
    r"^\s*(?:sub)?total\b|\b(?:ending|closing)\s+balance\b", re.I)
_MAX_HEADING = 60


def _section_after(s: str, kind: str, current: str | None) -> str | None:
    """The section an undated line leaves the statement in. A heading is a
    whole line naming withdrawals or deposits (a summary line carries an
    amount, so it never is one); a heading naming both directions, a
    total or an ending balance ends the section."""
    if _SECTION_END_RE.search(s):
        return None
    if len(s) > _MAX_HEADING or _AMOUNT_RE.search(s):
        return current
    out_re, in_re = ((_INV_OUT_HEAD_RE, _INV_IN_HEAD_RE)
                     if kind == "investment"
                     else (_BANK_OUT_HEAD_RE, _BANK_IN_HEAD_RE))
    if out_re.match(s):
        return "out"
    if in_re.match(s):
        return "in"
    named = {m.lastgroup for m in _COL_RE.finditer(s)}
    if _BOTH_HEAD_RE.match(s) or {"in", "out"} <= named:
        return None
    return current


def _finish_row(p: dict, kind: str, hint: str | None,
                column: str | None) -> dict:
    """A scanned line signed: `hint` is the merged positive signal (running
    balance, column, section), `column` the layout's own hint, which an
    earlier reading signed by."""
    mag, explicit, desc = p["mag"], p["explicit"], p["name"]
    amount, flow = _resolve_sign(kind, desc, mag, explicit, hint)
    row = {"date": p["date"], "name": desc, "amount": round(amount, 2),
           "flow": flow, "line": p["line"], "had_year": p["had_year"],
           # the amounts earlier readings gave this line, newest first —
           # what an earlier import stored it as (see import_pdf)
           "prior_amounts": [
               round(_legacy_amount(kind, desc, mag, explicit, column, era),
                     2) for era in _PRIOR_ERAS],
           "sign_unknown": explicit is None and hint == UNKNOWN_SIGN}
    if "prior_date" in p:
        row["prior_date"] = p["prior_date"]
    return row


def _find_period_opening_as_end(
        lines: list[str]) -> tuple[dt.date | None, dt.date | None]:
    """The anchors an earlier reading took: the first two-date period
    line, else the first lone period date of ANY label — an opening date
    included — as the end. Kept only so a statement imported under that
    reading can be re-dated in place on re-import (see import_pdf); never
    used to date a row."""
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


def _latest_full_date(lines: list[str]) -> dt.date | None:
    full = []
    for raw_line in lines:
        dm = _DATE_RE.match(raw_line.strip())
        parts = _date_from_match(dm) if dm else None
        if parts and parts[2]:
            d = _date_or_none(parts[2], parts[0], parts[1])
            if d:
                full.append(d)
    return max(full) if full else None


def parse_statement(text: str, *, kind: str = "bank",
                    hints: list[str | None] | None = None) -> dict:
    """Heuristic parse of extracted statement text (no bank templates).

    kind: 'bank'/'statement' (checking/savings), 'card' (credit), or
    'investment' (brokerage/IRA activity) — drives the sign heuristic;
    see module docstring. `hints`, one per line of `text` as
    extract_statement built them, names the column an unsigned amount
    was drawn under.

    An unsigned amount's sign comes from the statement's own signals
    first: the running balance (exact, and only when one assignment adds
    up), then the column, then the section heading the line sits under;
    the wording decides only where all three are silent, and the
    warnings say how many lines that was.

    Returns {"rows": [{date, name, amount, flow, line, …}], "confidence":
    0-1 (fraction of candidate lines parsed + a date-monotonicity bonus),
    "period": (start, end) or None, "warnings": [...]}. Amounts are
    engine-signed (positive = money out). A yearless row that an earlier
    reading of the period anchors dated differently also carries that
    date as "prior_date"."""
    if kind not in STATEMENT_KINDS:
        raise ValueError(f"unknown statement kind: {kind!r}")
    if kind == "statement":
        kind = "bank"
    all_lines = text.splitlines()
    all_hints = (list(hints) + [None] * len(all_lines))[:len(all_lines)] \
        if hints else [None] * len(all_lines)
    kept = [(ln, h) for ln, h in zip(all_lines, all_hints)
            if len(ln) <= MAX_LINE_CHARS]
    lines = [ln for ln, _ in kept]
    line_hints = [h for _, h in kept]
    overlong = len(all_lines) - len(lines)
    start, end = _find_period(lines)
    if end is None:
        # No statement period found. Anchor yearless rows to the latest
        # FULL date anywhere in the file rather than to the wall clock: a
        # guess that depends on the day the file is imported dates a
        # decade of history differently on every re-import and breaks the
        # dedup that makes re-importing safe. Only a file with no year at
        # all falls back to today (and says so in its warnings).
        end = _latest_full_date(lines)
    # The earlier reading of the period anchors, with the same fallback;
    # only carried when it differs, so the rows it dated otherwise can be
    # found on re-import.
    prior = _find_period_opening_as_end(lines)
    if prior[1] is None:
        prior = (prior[0], _latest_full_date(lines))
    prior = None if prior == (start, end) else prior
    scanned: list[tuple[dict, str | None, str | None]] = []
    events: list[tuple] = []
    candidates = 0
    dated_summaries = 0
    section: str | None = None
    signals = kind in ("bank", "investment")
    for raw_line, column in zip(lines, line_hints):
        s = raw_line.strip()
        if not s:
            continue
        if signals:
            if _SEED_RE.search(s):
                tail = _trailing_amounts(s)
                if tail:
                    bmag, bsign = _amount_parts(tail[-1])
                    events.append(("seed", round(
                        (-bmag if bsign in ("neg", "dr") else bmag) * 100)))
            if not _DATE_RE.match(s):
                section = _section_after(s, kind, section)
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
        p = _scan_line(s, dm, start, end, prior)
        if p is None:
            continue
        explicit = p["explicit"]
        delta = (None if explicit is None
                 else round(p["mag"] * 100) * (1 if explicit == "cr" else -1))
        events.append(("row", len(scanned), round(p["mag"] * 100), delta,
                       p["balance"]))
        sec = section
        if (sec == "out" and kind == "investment"
                and _INV_INCOME_RE.search(p["name"])):
            sec = None                      # income is never paid out
        scanned.append((p, column, sec))
    by_balance = _balance_hints(events, kind)
    rows: list[dict] = []
    yearless = False
    disagreed = worded = 0
    for i, (p, column, sec) in enumerate(scanned):
        hint = by_balance.get(i)
        if hint is not None:
            if column in ("in", "out") and column != hint:
                disagreed += 1
        elif column in ("in", "out"):
            hint = column
        elif sec is not None:
            hint = sec
        else:
            hint = column
        if (signals and p["explicit"] is None
                and hint not in ("in", "out")):
            worded += 1
        row = _finish_row(p, kind, hint, column)
        yearless = yearless or not row.pop("had_year")
        rows.append(row)
    warnings: list[str] = []
    if candidates == 0:
        warnings.append("no transaction-like lines found — this may not be "
                        "a bank or card statement")
    elif len(rows) < candidates:
        warnings.append(f"{candidates - len(rows)} line(s) looked like "
                        "transactions but could not be parsed")
    if overlong:
        warnings.append(f"{overlong} line(s) too long to be a statement "
                        "line were skipped")
    if yearless and end is None:
        warnings.append("no statement period or closing date found — the "
                        "year for dated lines was guessed")
    if dated_summaries:
        warnings.append(f"{dated_summaries} dated line(s) containing "
                        "summary words (balance/total/minimum) were "
                        "skipped — check the statement's totals against "
                        "the imported rows")
    if disagreed:
        warnings.append(f"{disagreed} line(s): the running balance and the "
                        "column disagreed; the balance was used")
    if worded:
        warnings.append(f"{worded} line(s) printed no sign, column or "
                        "running balance; their direction was read from "
                        "the wording — check them")
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


def _cents(x) -> int:
    return round(float(x) * 100)


def _adopt_earlier_ids(conn, account_id: str, txns: list[Transaction],
                       minted: list[str],
                       prior_of: list[list[list[tuple[str, float]]]],
                       flipped_of: list[list[tuple[str, float]]],
                       kept_of: list[list[tuple[str, float]]],
                       redated_of: list[list[tuple[str, float, dt.date]]]
                       ) -> tuple[list[Transaction], int]:
    """Land each row on the row an earlier import of the same statement
    made, when that import read the line differently; the rows corrected.

    The readings have been corrected over time — the sign a column, a
    running balance, a section heading, a printed minus or the wording
    gives a line, and the year a Dec->Jan statement's January line falls
    in. The id hashes the date and the SIGNED amount and its occurrence
    among identical rows, so re-importing such a statement computes a new
    id, and the corrected row would land beside its old twin, counting
    the money twice. Each earlier row found is RE-KEYED to the new id
    (reimport.land): kept on its old id, a corrected row would stand
    under the id of a real, different line — a refund and a purchase of
    one amount on one day, or last year's January line — and a later
    file holding that line would overwrite it.

    `prior_of[g][i]` is row i's id under earlier reading g (newest first),
    every row read that way and numbered in file order exactly as that
    import numbered them (a same-day in/out pair of one amount was
    occurrences 1 and 2 of one shape; now each is occurrence 1 of its own
    sign), with the amount that reading gave. The stored rows came from
    one reading, so whichever explains most of them — the current one on
    a tie — decides the whole file: under an earlier one, every row whose
    old id stands for the same printed line at that reading's amount is
    moved to its new id. Otherwise each row whose new id is missing may
    still take an earlier reading's row, or the opposite-signed row
    (`flipped_of`, the shape every earlier sign rule left), when no other
    row of this file has that id as its own. A row whose new id already
    stands beside such an old one (an earlier re-import added it) retires
    the old one onto it.

    `kept_of[i]` is set only for a row whose page the extraction's time
    budget denied column hints and that no balance or heading signed (its
    sign is unknown, not absent): if the opposite-signed row stands for
    the same line and the new id does not, the row keeps that id AND that
    stored sign — a re-import on a loaded host must not rewrite a sign an
    idle one read from the columns.

    `redated_of[i]` names row i's year-early id(s) when the earlier period
    reading dated its yearless line a year before. That id is also last
    year's real row of the same printed line, so a year-early row moves
    only when every batch that owns it imported THIS statement — proved
    by owning this file's rows whose ids did not move — and never when it
    carries no batch at all."""
    probe = {t.id for t in txns}
    for gen in prior_of:
        probe |= {c for cands in gen for c, _ in cands}
    probe |= {c for cands in flipped_of for c, _ in cands}
    probe |= {c for cands in kept_of for c, _ in cands}
    probe |= {c for cands in redated_of for c, _, _ in cands}
    if len(probe) == len(txns):
        return txns, 0
    have = reimport.stored(conn, account_id, probe)

    def same(r, t):
        return r is not None and r["line"] == t.raw.get("line")

    def standing(cands, t):
        return next((c for c, amt in cands
                     if same(have.get(c), t)
                     and _cents(have[c]["amount"]) == _cents(amt)), None)

    here = [same(have.get(t.id), t) for t in txns]
    olds = [[standing(gen[i], t) for i, t in enumerate(txns)]
            for gen in prior_of]
    best, best_hits = None, sum(here)
    for g, st in enumerate(olds):
        hits = sum(1 for x in st if x is not None)
        if hits > best_hits:
            best, best_hits = g, hits
    taken = {t.id for t in txns}
    claimed: set[str] = set()
    moves: list[tuple[str, int]] = []           # (old id, row)
    out = list(txns)
    for i, t in enumerate(txns):
        o = olds[best][i] if best is not None else None
        if o is not None and o not in claimed:
            claimed.add(o)
            if o != t.id:
                moves.append((o, i))
            continue
        if not here[i] and kept_of[i]:
            k = standing(kept_of[i], t)
            if k is not None and k not in claimed and k not in taken:
                claimed.add(k)
                primary, detailed = flowmap.flow_category(
                    t.raw.get("flow"), -t.amount)
                out[i] = dataclasses.replace(
                    t, id=k, amount=-t.amount, category_primary=primary,
                    category_detailed=detailed)
                continue
        for c in [st[i] for st in olds] + [standing(flipped_of[i], t)]:
            if c is None or c == t.id or c in claimed or c in taken:
                continue
            claimed.add(c)
            moves.append((c, i))
            break
    vacated = {o for o, _ in moves}
    if any(redated_of):
        unchanged = [t.id for i, t in enumerate(txns)
                     if not redated_of[i] and here[i] and t.id == minted[i]
                     and t.id not in vacated
                     and not any(j == i for _, j in moves)]
        owners = reimport.same_file_batches(conn, account_id, "pdf",
                                            unchanged)
        for i, t in enumerate(out if owners else ()):
            if t.id != txns[i].id:
                continue                    # kept its stored sign above
            for c, amt, then in redated_of[i]:
                r = have.get(c)
                if (r is None or c in claimed or c in taken
                        or not same(r, t) or r["date"] != then
                        or _cents(r["amount"]) != _cents(amt)):
                    continue
                own = reimport.batch_owners(r["raw"])
                if not own or not own <= owners:
                    continue
                claimed.add(c)
                moves.append((c, i))
                break
    rekeys, retires = [], []
    for o, i in moves:
        n = txns[i].id
        if here[i] and n not in vacated:
            # the corrected row already stands beside the old one
            if reimport.retirable(have.get(o)):
                retires.append((n, o))
        else:
            rekeys.append((o, n))
    # one landing per new id: a second candidate for a row that a move
    # already lands is a twin of it
    seen: set[str] = set()
    final: list[tuple[str, str]] = []
    for o, n in rekeys:
        if n in seen:
            if reimport.retirable(have.get(o)):
                retires.append((n, o))
            continue
        seen.add(n)
        final.append((o, n))
    return out, reimport.land(conn, final, retires)


def _retire_absorbed(conn, account_id: str, txns: list[Transaction],
                     scanned: list[tuple[dict, dt.date | None, bool]],
                     gone: list[int],
                     prior_of: list[list[list[tuple[str, float]]]],
                     flipped_of: list[list[tuple[str, float]]],
                     redated_of: list[list[tuple[str, float, dt.date]]]
                     ) -> int:
    """Retire the row an earlier reading stored for each line of this
    file the bank-feed dedup now absorbs; the rows retired.

    The corrected reading of such a line matches the feed's row and is
    skipped before _adopt_earlier_ids sees it, so the earlier file row —
    a deposit stored as spending, a January line stored a year early —
    would stay beside the feed's row and count the money twice. It goes
    onto the feed's row (reimport.retire_absorbed) under the same proofs
    a re-key takes: the earlier id recomputed exactly, the stored row on
    this account at that reading's amount and the same printed line, no
    row of this file standing under that id now, and a year-early id
    moved only when every batch that owns it imported this statement."""
    taken = {t.id for t in txns}
    owners: set[str] | None = None

    def file_batches() -> set[str]:
        nonlocal owners
        if owners is None:
            unchanged = [t.id for t, (r, pd, _) in zip(
                txns, [s for s in scanned if not s[2]]) if not pd]
            have = reimport.stored(conn, account_id, unchanged)
            owners = reimport.same_file_batches(
                conn, account_id, "pdf",
                [i for i in unchanged if i in have])
        return owners

    absorbed = []
    for i in gone:
        r = scanned[i][0]
        line = _mask_long_digits(r["line"])
        then = {}
        for c, _, pd in redated_of[i]:
            then.setdefault(c, pd)
        cands = [ca for gen in prior_of for ca in gen[i]]
        cands += list(flipped_of[i])
        cands += [(c, a) for c, a, _ in redated_of[i]]
        cands = list(dict.fromkeys(cands))

        def accept(row, line=line, then=then):
            if row["line"] != line:
                return False
            pd = then.get(row["id"])
            if pd is None:
                return True
            own = reimport.batch_owners(row["raw"])
            return (row["date"] == pd and bool(own)
                    and own <= file_batches())

        absorbed.append((r["date"], r["amount"], (r["name"],), cands,
                         accept))
    return reimport.retire_absorbed(conn, account_id, "pdf", absorbed,
                                    taken=taken)


def import_pdf(conn, account_id: str, data: bytes, *,
               amount_sign: str = "bank", batch_id: str | None = None) -> dict:
    """Extract → parse → dedup → upsert one statement PDF into
    `account_id`. amount_sign: bank|statement|card|investment (parser
    sign heuristic). Returns csvimport-shaped counters plus the parser's
    confidence, warnings and period; `corrected` counts rows an earlier
    import read differently that were corrected in place."""
    if amount_sign not in STATEMENT_KINDS:
        raise ValueError(f"unknown amount_sign: {amount_sign!r}")
    text, hints = extract_statement(data)
    parsed = parse_statement(text, kind=amount_sign, hints=hints)
    # same row cap as every other importer — MAX_PAGES/MAX_TEXT_BYTES bound
    # the PDF itself, but 2 MB of dense text still parses to far more
    # transaction lines than any sibling importer would accept, and each
    # row costs a dedup scan and a synchronous upsert inside the request
    rows = check_len(parsed["rows"], what="transactions")
    # The household's pairing and identity locks, taken here and not by the
    # caller: the extraction above runs in a child that can take most of a
    # minute, and every edit and rename in the household waits on these
    # two, so they are held around the writes only. Still before the first
    # ledger row, in the order every other writer takes them (see
    # web/bulk_import.hold_ledger_write_locks). Re-entrant when a caller
    # already holds them; on an autocommit connection they release at once,
    # as every lock there does. Imported lazily: the sync package carries
    # no load-time dependency on the web layer.
    from ..web.bulk_import import hold_ledger_write_locks
    hold_ledger_write_locks(conn)
    deduper = Deduper(conn, account_id, "pdf", [r["date"] for r in rows])
    txns: list[Transaction] = []
    ids = FileImportIds("pdf", account_id)
    minted: list[str] = []
    landed_rows: list[tuple[dict, dt.date | None]] = []
    # every row read, in file order, with whether the dedup absorbed it
    scanned: list[tuple[dict, dt.date | None, bool]] = []
    # opposite-signed ids for rows whose sign the time budget left unknown
    kept_ids = FileImportIds("pdf", account_id)
    kept_of: list[list[tuple[str, float]]] = []
    skipped_dupes = 0
    for r in rows:
        prior_date = r.pop("prior_date", None)
        if deduper.claim(r["date"], r["amount"], r["name"]):
            skipped_dupes += 1
            scanned.append((r, prior_date, True))
            continue
        scanned.append((r, prior_date, False))
        # the id hashes the MASKED name (see FileImportIds)
        name = mask_long_digits(r["name"])
        tid = ids.id_for(r["date"], r["amount"], r["name"])
        minted.append(tid)
        landed_rows.append((r, prior_date))
        if r.get("sign_unknown"):
            kid = kept_ids.id_for(r["date"], -r["amount"], r["name"])
            kept_of.append([(x, -r["amount"])
                            for x in (kid, kept_ids.legacy_id(kid)) if x])
        else:
            kept_of.append([])
        # flow rows (card payments / transfers) land spend-excluded
        primary, detailed = flowmap.flow_category(r["flow"], r["amount"])
        txns.append(Transaction(
            id=tid, account_id=account_id, date=r["date"],
            amount=r["amount"], name=name,
            category_primary=primary, category_detailed=detailed,
            raw={"line": _mask_long_digits(r["line"]),
                 "flow": r["flow"]}))
    txns = ids.resolve(conn, txns)

    # The ids these rows had under earlier readings, each numbered over
    # every row the way that import numbered them (see _adopt_earlier_ids).
    # A row the dedup absorbs now was numbered by an earlier import that
    # read it with another amount or date — the feed's row could not have
    # absorbed it then, its dedup cell being the signed cents on the day —
    # so it counts; one read the same way as now was absorbed then too.
    dates = [r["date"] for r, _, _ in scanned]
    now = [r["amount"] for r, _, _ in scanned]

    def numbered(amounts, on):
        f = FileImportIds("pdf", account_id)
        out = []
        for (r, _, gone), amt, d, cur, today in zip(scanned, amounts, on,
                                                    now, dates):
            if gone and _cents(amt) == _cents(cur) and d == today:
                out.append([])
                continue
            x = f.id_for(d, amt, r["name"])
            out.append([(y, amt) for y in (x, f.legacy_id(x)) if y])
        return out

    # the first import of a statement, and every re-import no reading
    # changed, has nothing an earlier reading left behind: no account row
    # of this importer that this file does not mint now
    earlier = bool(scanned) and conn.execute(
        "SELECT 1 FROM transactions WHERE account_id = %s"
        "   AND left(id, 4) = 'pdf:' AND id <> ALL(%s) LIMIT 1",
        (account_id, [t.id for t in txns])).fetchone() is not None
    readings: list[list[float]] = []
    for g in range(len(_PRIOR_ERAS) if earlier else 0):
        vec = [(r.get("prior_amounts") or [r["amount"]] * len(_PRIOR_ERAS))[g]
               for r, _, _ in scanned]
        if vec != now and vec not in readings:
            readings.append(vec)
    prior_all = [numbered(vec, dates) for vec in readings]
    flipped_all = (numbered([-a for a in now], dates) if earlier
                   else [[] for _ in scanned])
    redated_all: list[list[tuple[str, float, dt.date]]] = [
        [] for _ in scanned]
    if earlier and any(pd for _, pd, _ in scanned):
        then = [pd or r["date"] for r, pd, _ in scanned]
        for vec in [now] + readings:
            for i, cands in enumerate(numbered(vec, then)):
                pd = scanned[i][1]
                if pd:
                    redated_all[i] += [(x, a, pd) for x, a in cands]
    kept = [i for i, (_, _, gone) in enumerate(scanned) if not gone]
    txns, corrected = _adopt_earlier_ids(
        conn, account_id, txns, minted,
        [[gen[i] for i in kept] for gen in prior_all],
        [flipped_all[i] for i in kept], kept_of,
        [redated_all[i] for i in kept])
    gone = [i for i, (_, _, g) in enumerate(scanned) if g]
    if earlier and gone:
        corrected += _retire_absorbed(
            conn, account_id, txns, scanned, gone, prior_all, flipped_all,
            redated_all)
    # the household's history window (Settings → how far back): rows
    # dated before it are left out and counted, never written
    txns, skipped_older = drop_before_history(conn, txns)
    if batch_id:
        from . import batches
        batches.tag(conn, txns, batch_id)
    added = upsert_transactions(conn, txns)
    return {"imported": added, "rows": len(txns),
            "skipped_duplicates": skipped_dupes,
            "skipped_older": skipped_older,
            "corrected": corrected,
            "confidence": parsed["confidence"],
            "warnings": parsed["warnings"],
            "period": parsed["period"]}
