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
names which — 'user' (a person's pin), a built-in store's key (an item
match from the store's own records; see store_match), 'bill' (a bill's transaction category on
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
    archive that has no override_source column — from the evidence that
    implies the kind: the manual_categories row (bill_id set = a bill,
    else a person), else the store prefix, else a person. Tenant-scoped by
    RLS on the caller's connection.

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

# One charge, several categories (transaction_splits, migration 138). A
# rollup BY CATEGORY joins the parts and reads them through PART_CAT /
# PART_AMOUNT: a split row fans out into one row per part, an unsplit row
# stays one row whose single part is the row itself — so every reader that
# joins agrees with every other one, and none of them carries a flag to
# remember. `t` must be the transactions alias, `sp` is reserved for the
# parts. Readers that list ledger rows ONE PER TRANSACTION must not join
# (the join multiplies split rows); they carry the parts as a JSON column.
SPLIT_JOIN = ("LEFT JOIN transaction_splits sp "
              "ON sp.tenant_id = t.tenant_id AND sp.txn_id = t.id")
PART_CAT = "COALESCE(sp.category, t.category_override, t.category_primary)"
PART_CAT_DISPLAY = (
    "REPLACE(COALESCE(sp.category, t.category_override, t.category_primary, '?'),"
    " '_', ' ')")
PART_AMOUNT = "COALESCE(sp.amount, t.amount)"


def part_net(net_expr: str) -> str:
    """A part's share of the row's reimbursement-netted amount. `net_expr`
    is the whole row's net (budget.NET_AMOUNT or reporting's joined form);
    an unsplit row is its net, a part is its own amount when nothing was
    netted (the common case, kept exact rather than multiplied and divided
    back through a double) and otherwise its pro-rata share rounded to the
    cent, with the rounding remainder on the largest part (the first such
    line on a tie) so the parts of a netted row add up to its net exactly.
    Correlated subqueries, not a window, so the expression stays legal
    inside the SUM of every category rollup that reads it."""
    share = "ROUND(({amt} * ({net}) / t.amount)::numeric, 2)"
    mine = share.format(amt="sp.amount", net=net_expr)
    others = share.format(amt="s2.amount", net=net_expr)
    same = ("FROM transaction_splits s2 WHERE s2.tenant_id = sp.tenant_id "
            "AND s2.txn_id = sp.txn_id")
    largest = (f"(SELECT s2.line {same} "
               "ORDER BY ABS(s2.amount) DESC, s2.line LIMIT 1)")
    rest = (f"ROUND(({net_expr})::numeric - COALESCE((SELECT SUM({others}) "
            f"{same} AND s2.line <> sp.line), 0), 2)")
    return (f"CASE WHEN sp.line IS NULL THEN {net_expr} "
            f"WHEN ({net_expr}) = t.amount OR t.amount = 0 THEN sp.amount "
            f"WHEN sp.line = {largest} THEN {rest}::float8 "
            f"ELSE {mine}::float8 END")

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
# it. The picker reads from this list so free text cannot fragment the
# category set.
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


# ---- how a category reads on a page ------------------------------------

# the joining words a title leaves small ("Food and Drink")
_SMALL_WORDS = frozenset({"and", "or", "of", "the", "to", "in", "for", "a",
                          "an", "at", "on"})


def display_label(stored: str | None) -> str:
    """A category as people read it: the bank's `FOOD_AND_DRINK` (or the
    spaced `FOOD AND DRINK` the reports carry) as "Food and Drink". A name
    somebody typed — anything with a lower-case letter in it — is theirs
    and passes through untouched; "no category" reads as that. The clients
    carry the same rule as catLabel."""
    s = (stored or "").replace("_", " ").strip()
    if not s or s == "?":
        return "Uncategorized"
    if s != s.upper() or s == s.lower():
        return s
    words = s.lower().split(" ")
    # a joining word stays small between two words, never as the last one
    # ("Transfer In" beside "Transfer Out")
    return " ".join(w if i and i < len(words) - 1 and w in _SMALL_WORDS
                    else w[:1].upper() + w[1:]
                    for i, w in enumerate(words))
