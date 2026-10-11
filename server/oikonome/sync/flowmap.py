"""Shared vendor-string → flow-category mapping for the file importers.

The engine's spend definition (budget._spend_rows) excludes rows whose
category_detailed = 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT' or whose effective
category is TRANSFER_IN/TRANSFER_OUT. An importer that does not map its
vendor flow strings to those codes — YNAB/Monarch/Copilot/Simplifi/QIF/OFX/
generic-CSV all carry card payments and transfers with NULL or vendor-string
categories — lets them count as spend, and a history imported that way reads
as roughly double the real number. Every importer routes through this ONE
table so they all agree.

Direction rule (engine sign, positive = money out):
    amount > 0 → TRANSFER_OUT;  amount < 0 → TRANSFER_IN.
"""

from __future__ import annotations

CC_PAYMENT_DETAILED = "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"

# vendor strings that mean "credit-card settlement" (case-insensitive)
_CC_PAYMENT = {
    "credit card payment",
    "credit card payments",
}

# vendor strings that mean "internal transfer" (case-insensitive)
_TRANSFER = {
    "transfer",
    "transfers",
    "internal transfer",
    "balance adjustments",
    "transfer for cash spending",      # Mint's cash-spending shuffle
}


def transfer_category(amount: float) -> str:
    """TRANSFER_* by engine sign: money out → TRANSFER_OUT, in → TRANSFER_IN."""
    return "TRANSFER_OUT" if amount > 0 else "TRANSFER_IN"


import re as _re

# a TIGHT credit-card-payment name detector for importers (OFX/CSV)
# that carry no category. Deliberately narrow: it must see a card+payment
# signal together, so a real bill-pay to a merchant ("CITY ELECTRIC PAYMENT",
# "PAYMENT THANK YOU") stays spend, while "CHASE CREDIT CRD AUTOPAY",
# "CARDMEMBER SERV WEB PYMT", "AMEX EPAYMENT" read as the internal settlement
# they are (spend-excluded, so the card's own charges aren't double-counted).
_PAY = r"(?:payment|pymt|pmt|autopay|e-?pay(?:ment)?)"
_CC_PAYMENT_NAME = _re.compile(
    r"("
    r"credit\s*c(?:ar|r)?d.*" + _PAY +           # CREDIT CRD/CARD ... AUTOPAY
    r"|cardmember"                               # CARDMEMBER SERV ...
    r"|(?:amex|american express|discover|visa|mastercard).*" + _PAY +  # card-only brands
    r"|\bcard\b.*" + _PAY +                      # ... CARD PAYMENT
    r"|" + _PAY + r".*\bcard\b"                  # AUTOPAY ... CARD
    r")", _re.I)


def looks_like_card_payment(name: str | None) -> bool:
    """True when a bare transaction NAME clearly means a credit-card payment
    (internal transfer), not a merchant purchase. Conservative by design."""
    return bool(name and _CC_PAYMENT_NAME.search(name))


# Payment-shaped descriptions inside a mixed payments/credits category
# ("DIRECTPAY FULL BALANCE", "INTERNET PAYMENT - THANK YOU",
# "PHONE PAYMENT"). The category itself supplies the card signal, so a
# bare payment word suffices here — unlike looks_like_card_payment.
# "thank you" is NOT here. It is a pleasantry, not a payment word: card
# statements put it on refunds too ("RETURN - THANK YOU FOR SHOPPING"),
# and a genuine merchant refund must ride through as the credit it is. A
# refund swallowed as an internal settlement leaves the original purchase
# un-offset, so spend reads high and the household is told to spend less.
# Card payment descriptions carry a real payment word as well, so nothing
# is lost without it.
_PAYMENT_WORD = _re.compile(r"payment|pymt|pmt|autopay|directpay", _re.I)


def looks_like_card_payment_on_card(name: str | None) -> bool:
    """True for a MONEY-IN row on a CREDIT-CARD account whose description
    reads like a payment.

    The bare-name detector above has to be paranoid because it runs against
    checking rows, where "PAYMENT THANK YOU" could be any merchant. Here the
    account itself supplies the card signal — exactly like Discover's
    "Payments and Credits" category does — so a plain payment word is enough,
    and a genuine merchant refund (no payment word) still rides through as
    the credit it is.

    This exists because aggregators can mislabel these: a card settlement
    ("Payment Thank You - Web") may arrive as
    LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT, which is not in the spend
    exclusion list, so the row would read as money IN and inflate the flow
    picture.
    """
    return bool(name and _PAYMENT_WORD.search(name))


# Investment / brokerage activity. Every importer that reads an investment
# account (the PDF statement, the activity CSV) asks the same question of
# a line's description, so one account imported both ways lands the same.
_INV_FLOW_RE = _re.compile(
    r"\b(buy|bought|sell|sold|contribution|reinvest|transfer"
    r"|rollover|deposit|withdrawal)\b", _re.I)
# Shops whose names hold "buy" are spending, however the PDF spaced them;
# they are cut out before the movement words are read.
_BUY_SHOP_RE = _re.compile(r"\bbest\s+buy\b|\bbuy\s+buy\b", _re.I)
# More of the same kind of movement: the household's money changing form
# between its own holdings and accounts, never spent. Checked only after
# the income and fee words below, so a dividend or interest line keeps the
# category it always had and a fee stays spending.
_INV_MOVE_RE = _re.compile(
    r"\b(reinvest(ed|ment)?|exchanged?|exchanges|redemptions?|redeemed"
    r"|distributions?|withdrawals?|transfers?|transferred)\b", _re.I)
# "Purchase" and "sale" are also how a brokerage CASH account's debit card
# and register describe shopping, so they mean a trade only beside a word
# that names a security: a fund, shares, a fund family, a share class, or
# a ticker. "Index" alone is not one ("INDEX CARDS"); an index fund says
# fund or admiral beside it, and those count. A company's legal suffix
# alone does not either: a brokerage cash account prints "PURCHASE EXAMPLE
# STORES CORP" for a card swipe just as a trade confirmation would, and a trade
# that is one says shares, a quantity or a price beside the name — read as
# a trade, a swipe vanishes from spending, which is the worse mistake.
_INV_TRADE_RE = _re.compile(r"\b(purchased?|purchases|sale)\b", _re.I)
_INV_SECURITY_RE = _re.compile(
    r"\b(funds?|shares?|shs|shrs|etfs?|stocks?|bonds?|treasury"
    r"|t-?bills?|money market|securit(y|ies)|cusip|units|admiral"
    r"|vanguard|fidelity|schwab|ishares|spdr|blackrock|pimco|invesco"
    r"|t\.?\s?rowe|adrs?|class\s+[ab])\b"
    r"|@\s*\$?\d", _re.I)
# Tickers are matched case-sensitively: a mutual fund's (four capitals
# ending in X, but not an ordinary word of that shape such as INDEX) or one
# in parentheses. A bare short word after the trade word is not one: a
# card line reads "PURCHASE EXM" or "SALE EXAMPLE OUTFITTERS" just the same.
_FUND_TICKER_RE = _re.compile(
    r"\b(?!(?:INDEX|ANNEX|LATEX|RELAX)\b)[A-Z]{3}[A-Z]X\b|\([A-Z]{1,5}\)")
# What a SALE must carry before it is read as money IN (sale_of_security):
# the words above that only a trade prints — a fund, a share count, a
# price per share, a fund family, a CUSIP, a ticker the line marks as one.
# A company's legal suffix is not enough: a shop is a company too ("SALE
# SAMPLE GOODS CO").
_STRONG_SECURITY_RE = _re.compile(
    r"\b(funds?|etfs?|money\s+market|cusip|admiral"
    r"|vanguard|fidelity|schwab|ishares|spdr|blackrock|pimco|invesco"
    r"|t\.?\s?rowe)\b"
    r"|(?<![\d,.])\d[\d,]{0,20}(?:\.\d{1,8})?\s*(shares?|shs|shrs|sh)\b"
    r"|\b(shares?|shs|shrs|sh)\s*\d"
    r"|@\s*\$?\d", _re.I)
# Spending and pay on a brokerage cash account. A card, register, ATM or
# bill-pay line is spending however it is worded, and a paycheck landing
# in the account is income, so neither may be read as the household's
# money changing form. Checked before any movement word.
_INV_CARD_SPEND_RE = _re.compile(
    r"\b(debit\s+card|check\s?card|card\s+purchase|visa|mastercard|pos"
    r"|point\s+of\s+sale|atm|bill\s?pay(ment)?|recurring\s+card)\b", _re.I)
_INV_PAY_RE = _re.compile(
    r"\b(direct\s+dep(osit)?|dir\s+dep|payroll|salary|wages|paycheck)\b",
    _re.I)
_INV_INCOME_RE = _re.compile(
    r"\b(dividends?|div\.|interest|capital gains?)\b", _re.I)
_INV_FEE_RE = _re.compile(r"\b(fees?|commissions?|expenses?)\b", _re.I)

# No statement line is longer than this; the PDF importer drops longer
# lines for the same reason. CPython's regex engine holds the GIL while it
# backtracks, so one pathological description from an uploaded CSV must
# never reach the patterns above at full length.
MAX_DESC_CHARS = 1_000


def _bounded(desc: str | None) -> str:
    return (desc or "")[:MAX_DESC_CHARS]


def investment_flow(desc: str | None) -> str | None:
    """The flow hint of an investment-account line. A buy, sale of a
    security, exchange, reinvestment, distribution or transfer moves the
    household's own money between its holdings and accounts, so it is a
    transfer: left without one, a fund purchase counts as spending
    wherever spending is summed. Fees and commissions are what the account
    really costs and stay spending, as do card, register, ATM and bill-pay
    lines on a cash-management account; a direct deposit of pay stays
    income; dividend and interest lines keep the hint they always had.
    Only the category is decided here; the sign is decided apart."""
    desc = _bounded(desc)
    if (_INV_FEE_RE.search(desc) or _INV_CARD_SPEND_RE.search(desc)
            or _INV_PAY_RE.search(desc)):
        return None
    if _INV_FLOW_RE.search(_BUY_SHOP_RE.sub(" ", desc)):
        return "transfer"
    if _INV_INCOME_RE.search(desc):
        return None
    if _INV_MOVE_RE.search(desc):
        return "transfer"
    trade = _BUY_SHOP_RE.sub(" ", desc)
    if _INV_TRADE_RE.search(trade) and (_INV_SECURITY_RE.search(trade)
                                        or _STRONG_SECURITY_RE.search(trade)
                                        or _FUND_TICKER_RE.search(trade)):
        return "transfer"
    return None


def sale_of_security(desc: str | None) -> bool:
    """Whether an unsigned investment-account SALE line is the household
    selling a security, so money in. A sale is also how a cash account's
    card and register print shopping, and reading shopping as money in
    turns spending into income, so only the words only a trade prints
    count here — the same evidence that makes investment_flow call the
    line a trade (a fund, shares, a bond or treasury, a share count, a
    price per share, a fund family, a CUSIP or a marked ticker) — never a
    company suffix or a short word that merely looks like a ticker, and
    never a card, register, pay or fee line. Sign and category then agree:
    a line read as a trade is never booked as money out."""
    desc = _BUY_SHOP_RE.sub(" ", _bounded(desc))
    if (_INV_FEE_RE.search(desc) or _INV_CARD_SPEND_RE.search(desc)
            or _INV_PAY_RE.search(desc)):
        return False
    return bool(_INV_TRADE_RE.search(desc)
                and (_INV_SECURITY_RE.search(desc)
                     or _STRONG_SECURITY_RE.search(desc)
                     or _FUND_TICKER_RE.search(desc)))


def flow_category(category: str | None, amount: float,
                  name: str | None = None) -> tuple[str | None, str | None]:
    """(category_primary, category_detailed) when `category` is a vendor
    flow string (card payment / transfer); (None, None) otherwise —
    callers fall through to their own mapping/pass-through. `name` (the
    transaction description) disambiguates vendor categories that mix
    flows, like Discover's "Payments and Credits"."""
    s = (category or "").strip().lower()
    if s in _CC_PAYMENT:
        return "LOAN_PAYMENTS", CC_PAYMENT_DETAILED
    if s in _TRANSFER:
        return transfer_category(amount), None
    if s == "payments and credits":
        # Discover's activity CSV: ONE category for two different
        # things — card payments (internal settlement; letting them count
        # alongside the card's own charges double-counts) and merchant
        # credits/refunds (real money back that must offset spend).
        # Discover names payments explicitly, so the description decides;
        # non-payment rows ride through untouched as refunds.
        if _PAYMENT_WORD.search(name or ""):
            return "LOAN_PAYMENTS", CC_PAYMENT_DETAILED
        return None, None
    return None, None
