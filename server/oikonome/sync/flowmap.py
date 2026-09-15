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


# payment-shaped descriptions inside a mixed payments/credits category
# (Discover: "DIRECTPAY FULL BALANCE", "INTERNET PAYMENT - THANK YOU",
# "PHONE PAYMENT"). The category itself supplies the card signal, so a
# bare payment word suffices here — unlike looks_like_card_payment.
# "thank you" is NOT here. It is a pleasantry, not a payment word: card
# statements put it on refunds too ("RETURN - THANK YOU FOR SHOPPING"), and
# matching it made this function contradict its own docstring, which promises
# a genuine merchant refund rides through as the credit it is. A refund
# swallowed as an internal settlement leaves the original purchase
# un-offset, so spend reads high and the household is told to spend less.
# Nothing is lost by dropping it: every payment example these rules were
# built from — Chase "Payment Thank You - Web", Discover "INTERNET PAYMENT -
# THANK YOU", "DIRECTPAY FULL BALANCE" — carries a real payment word too.
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
