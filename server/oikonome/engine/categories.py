"""Transaction category resolution — max-accuracy policy.

Layers (highest wins for *effective* spend category):

  1. ``category_override`` — per-transaction user correction (sacred;
     survives every sync; ``apply()`` never touches these rows).
  2. ``category_primary`` — working machine category after flow/seed/LLM/
     user-merchant ``apply()``.
  3. ``category_plaid`` — last Plaid personal_finance_category primary.
     Written only by bank sync; our refinements do not overwrite it.

What may refine ``category_primary`` (see ``llm_categorize.apply`` and
``mcc_classify``; the tests in test_llm_categorize pin each line):

  * A machine merchant rule (seed / LLM / model) reassigns a row ONLY when
    Plaid's original primary was GENERIC (GENERAL_MERCHANDISE / SERVICES /
    OTHER) or already agrees with the rule — at ANY confidence. A concrete
    Plaid label, even at LOW confidence, is never moved by a name-guessing
    layer: most such moves are wrong (rent → transportation, a burger
    stand → utilities because its name reads like a telecom).
  * The MCC pass — the merchant's registered line of business, direct
    evidence rather than a name guess — may sharpen a concrete Plaid label
    when Plaid's confidence is LOW / MEDIUM / missing, never HIGH /
    VERY_HIGH, and never a row the statement line labelled as fuel.
  * User-merchant rules (``merchant_categories.source='user'``) rewrite
    any non-flow, non-override row, over HIGH-confidence Plaid included —
    the person taught that merchant deliberately.

Flow categories (TRANSFER_*, INCOME, LOAN_PAYMENTS) stay FLOW-GUARDed.

``category_override`` is written by four hands, and ``override_source``
names which — 'user' (a person's pin), 'amazon' / 'costco' (an item match
from the store's own records), 'bill' (a bill's transaction category on
the rows it matches). Each writer states its kind and never overwrites a
kind that outranks it: a person > an item match > a bill. A store's
"<Store> - Unmatched" is the ABSENCE of an item match and ranks with a
bill's stamp. Nothing depends on who wrote last.
"""
from __future__ import annotations

# Who wrote an override, highest rank first. A writer of rank r may replace
# an override of rank >= r (its own kind included) and never one above it.
OVERRIDE_RANK = {"user": 0, "amazon": 1, "costco": 1, "bill": 2}
STORE_SOURCES = ("amazon", "costco")


def override_outranks(existing: str | None, writer: str,
                      existing_override: str | None = None) -> bool:
    """May `writer` replace an override of kind `existing`? None is free.
    A store's "… - Unmatched" is no item match at all: it holds the
    store's kind but yields to a bill like an empty slot."""
    if existing is None or writer == "user":
        return True
    if (existing in STORE_SOURCES and existing_override
            and existing_override.endswith(" - Unmatched")):
        return writer in STORE_SOURCES or writer == "bill"
    return OVERRIDE_RANK.get(writer, 9) <= OVERRIDE_RANK.get(existing, 9)


def backfill_override_source(conn) -> int:
    """Classify overrides that carry no kind — rows restored from an
    archive written before the column existed — from the evidence that
    used to imply the kind: the manual_categories row (bill_id set = a
    bill, else a person), else the store prefix, else a person. The same
    rule migration 127 applied to existing ledgers. Tenant-scoped by RLS
    on the caller's connection.

    A CLEARED row has no override to classify and still needs its kind: a
    person's empty pin (bill_id NULL) over a NULL override is their answer
    of "no category", and without the kind back on the row the next bill
    or store pass reads it as one nobody ever touched and restamps it."""
    return conn.execute(
        """UPDATE transactions t
              SET override_source = CASE
                    WHEN m.transaction_id IS NOT NULL AND m.bill_id IS NULL THEN 'user'
                    WHEN m.transaction_id IS NOT NULL THEN 'bill'
                    WHEN t.category_override LIKE 'Amazon - %%' THEN 'amazon'
                    WHEN t.category_override LIKE 'Costco - %%' THEN 'costco'
                    ELSE 'user' END
             FROM transactions t2
             LEFT JOIN manual_categories m ON m.transaction_id = t2.id
            WHERE t.id = t2.id
              AND t.override_source IS NULL
              AND (t.category_override IS NOT NULL
                   OR (m.transaction_id IS NOT NULL AND m.bill_id IS NULL))""").rowcount


# Effective category for budget/spend/UI — override wins, else working primary.
EFFECTIVE_SQL = "COALESCE(t.category_override, t.category_primary)"
EFFECTIVE_SQL_SPACED = (
    "REPLACE(COALESCE(t.category_override, t.category_primary, '?'), '_', ' ')"
)

# Plaid confidences we treat as authoritative for sharp labels.
PLAID_TRUSTED = frozenset({"HIGH", "VERY_HIGH"})
PLAID_TRUSTED_SQL = "('HIGH', 'VERY_HIGH')"

# Vague aggregator buckets seed/LLM always may refine.
GENERIC = ("GENERAL_MERCHANDISE", "GENERAL_SERVICES", "OTHER")
GENERIC_SQL = "('GENERAL_MERCHANDISE', 'GENERAL_SERVICES', 'OTHER')"

FLOW = ("TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "INCOME")
FLOW_SQL = "('TRANSFER_IN', 'TRANSFER_OUT', 'LOAN_PAYMENTS', 'INCOME')"

# The 16 Plaid personal_finance_category PRIMARY values — the canonical set
# a user may pick from. Split for the picker: spend primaries first (what a
# merchant almost always is), flow behind them (an escrow wire really is
# TRANSFER_OUT, so they stay reachable). BANK_FEES
# is spend for budget purposes even though ``llm_categorize`` never assigns
# it. Free text here produced one-off fragmented categories; the picker
# reads from this list.
PLAID_SPEND = (
    "BANK_FEES", "ENTERTAINMENT", "FOOD_AND_DRINK", "GENERAL_MERCHANDISE",
    "GENERAL_SERVICES", "GOVERNMENT_AND_NON_PROFIT", "HOME_IMPROVEMENT",
    "MEDICAL", "PERSONAL_CARE", "RENT_AND_UTILITIES", "TRANSPORTATION",
    "TRAVEL",
)
PLAID_PRIMARIES = PLAID_SPEND + FLOW


def plaid_confidence(raw: dict | None, col: str | None = None) -> str:
    """Normalized confidence string from column or raw PFC blob."""
    if col:
        return (col or "").strip().upper()
    pfc = (raw or {}).get("personal_finance_category") or {}
    return (pfc.get("confidence_level") or "").strip().upper()


def plaid_is_trusted(confidence: str | None) -> bool:
    return (confidence or "").strip().upper() in PLAID_TRUSTED


def effective(override: str | None, primary: str | None) -> str | None:
    return override or primary
