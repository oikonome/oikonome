"""Business kinds: what a vendor IS, as a Schedule C line to suggest.

The bank's category says what kind of spending a row looks like to a
consumer ("general services"), which is no guide to the line a business
files it on: a web host, a payment API and an ad network all arrive as
general services and belong on three different lines. So a suggestion
comes from the vendor itself — the card-network merchant category code
(MCC) the merchant registered when the bank sends one, else words in its
name or website — matched against this short list of business kinds
(specs/business-books.md → Suggested lines).

Deliberately small and plain: a kind is suggested, never applied, and a
vendor that matches nothing gets no suggestion rather than a guess (the
owner's own line for it is then remembered for the next one). The lines
are the form's own wording (books.SCHED_C_LINES). Words, never brand
names: a list of vendors would be stale the month it shipped.
"""
from __future__ import annotations

import re

# (label, Schedule C line, merchant category codes, words). The first kind
# whose code matches wins; failing that, the first whose words appear in
# the vendor's name or website. Words match whole words (or a run of
# them), lowercased, with punctuation read as a space.
KINDS: tuple[tuple[str, str, frozenset, tuple], ...] = (
    ("advertising", "Advertising", frozenset({"7311", "7319"}),
     ("ads", "advertising", "ad network", "sponsored", "promoted",
      "marketing")),
    ("bank fees", "Other expenses", frozenset(),
     ("service fee", "service charge", "member fee", "membership fee",
      "monthly fee", "maintenance fee", "account fee", "wire fee",
      "overdraft", "nsf fee", "atm fee", "bank fee", "returned item")),
    ("payment processing fees", "Commissions and fees", frozenset(),
     ("processing fee", "merchant fee", "merchant service",
      "merchant services", "transaction fee", "payout fee")),
    ("government filing fees", "Taxes and licenses", frozenset({"9399"}),
     ("secretary of state", "sos", "department of state",
      "business license", "license fee", "licensing", "franchise tax",
      "county clerk", "annual report")),
    ("legal and accounting", "Legal and professional services",
     frozenset({"8111", "8931"}),
     ("attorney", "attorneys", "law office", "law firm", "legal", "cpa",
      "accounting", "accountant", "bookkeeping", "tax preparation")),
    ("insurance", "Insurance", frozenset({"6300", "6381", "6399"}),
     ("insurance",)),
    ("postage and mailing", "Office expense", frozenset({"9402"}),
     ("postal", "postage", "post office", "mailbox", "virtual mailbox",
      "mail forwarding", "shipping")),
    ("office supplies", "Office expense",
     frozenset({"5111", "5943", "5044"}),
     ("office supplies", "office supply", "stationery")),
    ("phone and internet", "Utilities", frozenset({"4814", "4899"}),
     ("wireless", "telecom", "broadband", "internet service", "fiber",
      "cable")),
    ("software and hosting", "Other expenses",
     frozenset({"4816", "5734", "5817", "5818", "7372", "7379"}),
     ("hosting", "host", "cloud", "software", "saas", "domain", "domains",
      "dns", "server", "servers", "web services", "api", "developer",
      "subscription", "app store", "email delivery")),
    ("workspace rent", "Rent or lease", frozenset({"6513"}),
     ("coworking", "co working", "office rent", "desk rental")),
    ("travel", "Travel",
     frozenset({"4511", "4722", "7011", "7012", "7512"}),
     ("airline", "airlines", "hotel", "lodging")),
    ("meals", "Meals", frozenset({"5812", "5813", "5814"}), ()),
    ("fuel", "Car and truck expenses", frozenset({"5541", "5542"}),
     ("fuel",)),
)

_SPLIT = re.compile(r"[^a-z0-9]+")


def _words(*parts) -> str:
    """The parts as one lowercased run of words, padded so a phrase can be
    found on word boundaries with a plain `in`."""
    return " " + " ".join(w for p in parts if p
                          for w in _SPLIT.split(str(p).lower()) if w) + " "


def kind_for(name: str | None, *, website: str | None = None,
             mcc: str | None = None) -> tuple[str, str] | None:
    """(kind label, Schedule C line) for a vendor, or None when it matches
    no kind. The merchant code first — it is what the merchant told its
    acquirer it does — then its name and website."""
    code = (mcc or "").strip()
    if code:
        for label, line, codes, _w in KINDS:
            if code in codes:
                return label, line
    hay = _words(name, website)
    for label, line, _c, words in KINDS:
        if any(_words(w) in hay for w in words):
            return label, line
    return None
