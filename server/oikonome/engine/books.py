"""Business books — deduction-bucket classification, a P&L,
and the §195/§248 start-up-cost calculation.

The three-bucket split plain expense tracking gets wrong:
  organizational — cost of CREATING the entity (§248)
  startup_195    — other pre-operating start-up costs (§195)
  operating      — ordinary post-open operating expenses (Schedule C)
The entity's business_start_date is the hinge (costs before it are org/§195).

This is categorization + summary, NOT tax advice — the P&L and the start-up
figures are informational; the CPA decides. (Out-of-scope by design.)
"""
from __future__ import annotations

import datetime as dt

BUCKETS = ("organizational", "startup_195", "operating")

# a business transaction = assigned per-row, OR on an entity-assigned
# account WITH NO per-row override. The row-level entity_id is an OVERRIDE
# (entities.assign_transaction's contract: 'or None to clear the override —
# falls back to the account's assignment'), not a second membership.
# A plain OR would make it a UNION — a row on Acme's account
# reassigned to Beta would match Acme through the ACCOUNT clause and Beta
# through the ENTITY clause, counting one $2,000 payment as $4,000 of
# business expense across the two P&Ls (and on into retained earnings).
# expense/revenue classification (money out/in, excluding transfers + CC
# payments) is applied per-row in pnl() — it needs category_detailed, which
# entity_transactions() carries.
# SHADOW EXCLUSION, like every other money aggregate in this codebase
# (`reporting._live_accounts`, `retirement.current_buckets`, `forecast`):
# non-primary linked accounts are dropped before summing, so a dual-source
# account never counts twice. A business account linked through two
# aggregators (the flow links.py documents) leaves two rows sharing one
# group, and the Business page's account picker assigns each row on its
# own, so both can carry entity_id; without this predicate the overlapping
# history would reach the P&L twice.
#
# Same `app.shadow_ids` session variable reporting.py reads: set per
# tenant_connect, empty when nothing is linked, so this is a no-op for the
# overwhelming majority of tenants.
from . import reporting  # noqa: E402
from . import merchant_dedup as _md

_BIZ_TXN = ("(t.entity_id = %(eid)s OR (t.entity_id IS NULL "
            "AND t.account_id IN "
            "(SELECT id FROM accounts WHERE entity_id = %(eid)s)))"
            + reporting._NOT_SHADOW.format(col="t.account_id"))

# Money that MOVED but was never spent. `pnl()` skips these per-row (same
# double-count rule as the personal side), and the Schedule C review queue
# must skip them too — otherwise `unclassified_count` lights the "Needs
# attention" tile for card payments and inter-account transfers that
# classifying cannot resolve: pick any line and the P&L is unchanged, leave
# it and the tile stays lit.
#
# Expenses only (both callers filter `t.amount > 0`), so the exact-match
# form from `_pnl_core`'s expense branch is the right one.
_NOT_FLOW = """
               AND COALESCE(t.category_override, t.category_primary, '')
                   NOT IN ('TRANSFER_OUT', 'TRANSFER_IN')
               AND COALESCE(t.category_detailed, '')
                   != 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'"""


def default_bucket(business_start_date, txn_date) -> str:
    """Costs dated before the business opened are pre-operating (default the
    §195 start-up bucket — the user reclassifies filing/legal to organizational);
    on/after the start date they're operating."""
    if business_start_date and txn_date and txn_date < business_start_date:
        return "startup_195"
    return "operating"



# ---- Schedule C expense lines -------------------------------------------
# Part II of IRS Form 1040 Schedule C is a PRINTED list of expense lines, so
# unlike personal categories (open, user-invented) this taxonomy is closed:
# every operating expense has to land on one of these to be reportable.
# Stored on business_txn_class.sched_c_line and rolled up by pnl()'s
# operating_by_line. Values are the FORM'S OWN WORDING, not snake_case
# identifiers: they are displayed verbatim in the P&L and the CSV
# export, and existing rows already use this shape.
#
# NOT tax advice, and deliberately not clever: meals are partially
# deductible, some purchases are capital rather than expense, vehicle and
# home-office use have their own methods. This maps a transaction to a LINE;
# what is deductible on that line is the CPA's call.
SCHED_C_LINES = (
    "Advertising",
    "Car and truck expenses",
    "Commissions and fees",
    "Contract labor",
    "Depletion",
    "Depreciation",
    "Employee benefit programs",
    "Insurance",
    "Interest",
    "Legal and professional services",
    "Office expense",
    "Pension and profit-sharing plans",
    "Rent or lease",
    "Repairs and maintenance",
    "Supplies",
    "Taxes and licenses",
    "Travel",
    "Meals",
    "Utilities",
    "Wages",
    "Other expenses",
)

# Fallback when a merchant has no history: map the ledger's own category to a
# line. Low-confidence by construction — it knows the KIND of spend, not the
# business purpose — so suggestions from here are always confirm-first.
_CATEGORY_TO_LINE = {
    "TRANSPORTATION": "Car and truck expenses",
    "TRAVEL": "Travel",
    "FOOD_AND_DRINK": "Meals",
    "RENT_AND_UTILITIES": "Utilities",
    "GENERAL_SERVICES": "Legal and professional services",
    "PROFESSIONAL_SERVICES": "Legal and professional services",
    "GENERAL_MERCHANDISE": "Office expense",
    "HOME_IMPROVEMENT": "Repairs and maintenance",
    "GOVERNMENT_AND_NON_PROFIT": "Taxes and licenses",
    "INSURANCE": "Insurance",
}


# Money IN that is a merchant giving money BACK, not a customer paying. An
# inflow still carrying a spending category is the return of a purchase
# (a refunded chair keeps the store's GENERAL_MERCHANDISE), so the P&L nets
# it off the expense it reverses. Counted as revenue it would overstate
# gross receipts (Schedule C line 1) AND leave the expense line at its
# pre-refund figure — two wrong numbers whose difference happens to be
# right. Payment-in categories (INCOME, transfers) and user-invented or
# blank categories stay revenue: only a category that names a KIND OF
# SPENDING is evidence of a refund.
_REFUND_CATEGORIES = frozenset({
    "BANK_FEES", "ENTERTAINMENT", "FOOD_AND_DRINK", "GENERAL_MERCHANDISE",
    "GENERAL_SERVICES", "GOVERNMENT_AND_NON_PROFIT", "HOME_IMPROVEMENT",
    "MEDICAL", "PERSONAL_CARE", "RENT_AND_UTILITIES", "TRANSPORTATION",
    "TRAVEL", "PROFESSIONAL_SERVICES", "INSURANCE",
})


def _inflow_role(t: dict) -> str:
    """How a row of money IN counts in the books: "revenue", "refund" (it
    reverses a cost in its own bucket) or "skip" (capital, a transfer or a
    card payment — neither revenue nor a cost).

    The one rule for every place that reads inflows — the year-scoped P&L
    and the lifetime start-up scan both call it. A row whose ORIGINAL
    category was a transfer stays capital; a reader that netted it off
    start-up costs would shrink the year's deduction by the owner's own
    capital injection.

    - A deposit whose links claim only part of it is revenue for the
      unclaimed part whatever its category says (the same rule personal
      income applies, reporting.PARTLY_CLAIMED_DEPOSIT); its net figure is
      already that part.
    - A capital injection the aggregator labelled a transfer stays capital
      even after a merchant-wide rule rewrote its category (a rule written
      for a payment app's outflows also reaches its inflows) — it is
      revenue only when the user made it INCOME outright.
    - Otherwise a spending category marks a refund (_REFUND_CATEGORIES) and
      anything else is revenue."""
    if t.get("partly_claimed"):
        return "revenue"
    cat = (t.get("category") or "").upper()
    was_transfer = "TRANSFER" in (t.get("cat_original") or "").upper()
    is_cc_payment = (t.get("cat_detailed") or "") == \
        "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
    if "TRANSFER" in cat or is_cc_payment or (
            was_transfer and cat != "INCOME"):
        return "skip"
    if cat in _REFUND_CATEGORIES:
        return "refund"
    return "revenue"


# How far back a refund looks for the cost it reverses. Returns and
# billing corrections arrive within weeks; a year covers an annual renewal
# refunded late without pairing a refund with an unrelated purchase from
# the same store years earlier.
_REFUND_LOOKBACK = dt.timedelta(days=365)


def _is_cost(t: dict) -> bool:
    """A row the books count as a cost (money out that is not a transfer
    or a card payment) — the expense branch's own test."""
    if (t["amount"] or 0) <= 0:
        return False
    if (t["category"] or "").upper() in ("TRANSFER_OUT", "TRANSFER_IN"):
        return False
    return (t.get("cat_detailed") or "") != "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"


def _row_date(t: dict) -> dt.date | None:
    d = t["date"]
    if d is None or isinstance(d, dt.date):
        return d
    return dt.date.fromisoformat(d)


def _cost_index(rows, start) -> dict:
    """payee -> [(date, gross amount, bucket, Schedule C line)] of every
    cost row, oldest first — what a refund searches for the cost it
    reverses. `rows` must be the entity's whole history: the cost can sit
    in an earlier year than the refund."""
    out: dict = {}
    for t in rows:
        if not t.get("payee") or not _is_cost(t):
            continue
        d = _row_date(t)
        out.setdefault(t["payee"], []).append(
            (d or dt.date.min, round(float(t["amount"]), 2),
             t["bucket"] or default_bucket(start, d), t["sched_c_line"]))
    for v in out.values():
        v.sort(key=lambda c: c[0])
    return out


def _reversed_cost(t: dict, index: dict):
    """The cost row a refund reverses, as a _cost_index entry, or None.

    The same merchant's charge dated on or before the refund and within
    _REFUND_LOOKBACK: the exact amount if one matches (the most recent
    such), else the most recent charge. A refund is not a cost of its own
    date — a pre-opening purchase returned after opening reverses the
    §195 total, and bucketing it by date instead would cut operating
    expenses while the start-up total kept a cost that came back."""
    d = _row_date(t)
    if d is None or not t.get("payee"):
        return None
    back = round(-float(t["amount"] or 0), 2)
    cands = [c for c in index.get(t["payee"], ())
             if d - _REFUND_LOOKBACK <= c[0] <= d]
    if not cands:
        return None
    exact = [c for c in cands if c[1] == back]
    return (exact or cands)[-1]


_CAPITALIZED = ("organizational", "startup_195")


def _refund_bucket(t: dict, start, index: dict, cutoff=None) -> str:
    """The bucket a refund nets against: the user's own class for the row
    first, else the bucket of the cost it reverses, else the date rule.

    `cutoff` is the last day of the year the §195/§248 deduction is taken
    (_deduction_year_end). A refund of a capitalized cost that arrives
    after it cannot reach back into that allowance — the earlier year's
    deduction is already on a filed return — so it is a recovery in its
    own year and counts as operating (a negative operating expense)."""
    if t["bucket"]:
        return t["bucket"]
    cost = _reversed_cost(t, index)
    if cost is not None:
        d = _row_date(t)
        # only a cost that was inside the deduction year has a filed
        # deduction to protect; a capitalized cost dated after the cutoff
        # still nets against its own bucket
        if (cost[2] in _CAPITALIZED and cutoff is not None
                and d is not None and d > cutoff and cost[0] <= cutoff):
            return "operating"
        return cost[2]
    return default_bucket(start, _row_date(t))


def _last_capitalized(rows, start) -> dt.date | None:
    """The date of the latest organizational or start-up cost (money out,
    by the same bucket rule every reader uses)."""
    last = None
    for t in rows:
        if not _is_cost(t):
            continue
        d = _row_date(t)
        if d and (t["bucket"] or default_bucket(start, d)) in _CAPITALIZED \
                and (last is None or d > last):
            last = d
    return last


def _deduction_year_end(start, rows) -> dt.date | None:
    """The last day of the tax year the §195/§248 deduction is taken (the
    year the business begins); None when there is no such year. `rows`
    is the entity's whole history."""
    begins = business_begins(start, {"last": _last_capitalized(rows, start)})
    return dt.date(begins.year, 12, 31) if begins else None


def _entity_start(conn, entity_id: str):
    ent = conn.execute(
        "SELECT business_start_date FROM business_entity WHERE id = %s",
        (entity_id,)).fetchone()
    return ent["business_start_date"] if ent else None


def unclassified_count(conn, entity_id: str) -> int:
    """How many business transactions still need a Schedule C decision.

    Separate from `suggest_lines` because that one is CAPPED — it returns at
    most `limit` rows to keep the review queue workable, and a tile that
    counted that list would stall at the cap while the user worked through a
    longer backlog. A count is not a page of results.
    """
    return conn.execute(
        f"""SELECT COUNT(*) AS n
              FROM transactions t
              LEFT JOIN business_txn_class c ON c.txn_id = t.id
             WHERE {_BIZ_TXN} AND t.removed = 0 AND t.amount > 0
               AND c.txn_id IS NULL{_NOT_FLOW}""",
        {"eid": entity_id}).fetchone()["n"]


def suggest_lines(conn, entity_id: str, *, limit: int = 200) -> list[dict]:
    """Propose a Schedule C line for each UNCLASSIFIED business transaction.

    SUGGEST, never apply. The personal side auto-applies a learned merchant
    category because a wrong one is cosmetic; a wrong Schedule C line is a
    wrong number on a filed return, so this returns proposals for a human to
    confirm. Two sources, and the caller is told which:

      "merchant"  — this entity has classified the same canonical merchant
                    before. Strong: it is the user's own prior decision.
      "category"  — derived from the ledger category. Weak: it knows the
                    kind of spend, not its business purpose.

    Merchant history is scoped to THIS entity on purpose. Two businesses can
    legitimately book the same vendor to different lines, and a personal
    correction must never reach business classification at all.
    """
    ent = conn.execute(
        "SELECT business_start_date FROM business_entity WHERE id = %s",
        (entity_id,)).fetchone()
    start = ent["business_start_date"] if ent else None
    rows = conn.execute(
        f"""SELECT t.id, t.date, t.amount,
                   {_md.DISPLAY_MERCHANT} AS payee,
                   COALESCE(t.category_override, t.category_primary, '') AS cat
              FROM transactions t
              {_md.MC_JOIN}
              LEFT JOIN business_txn_class c ON c.txn_id = t.id
             WHERE {_BIZ_TXN} AND t.removed = 0 AND t.amount > 0
               AND c.txn_id IS NULL{_NOT_FLOW}
             ORDER BY t.date DESC LIMIT {int(limit)}""",
        {"eid": entity_id}).fetchall()
    if not rows:
        return []

    # what this entity has already decided, by canonical merchant
    learned: dict[str, str] = {}
    for r in conn.execute(
            f"""SELECT {_md.DISPLAY_MERCHANT} AS payee,
                       c.sched_c_line AS line, COUNT(*) AS n
                  FROM business_txn_class c
                  JOIN transactions t ON t.id = c.txn_id
                  {_md.MC_JOIN}
                 WHERE {_BIZ_TXN} AND c.sched_c_line IS NOT NULL
                 GROUP BY 1, 2 ORDER BY 3 DESC""",
            {"eid": entity_id}).fetchall():
        learned.setdefault(r["payee"], r["line"])   # most-used wins

    out = []
    for r in rows:
        payee = r["payee"] or ""
        line = learned.get(payee)
        source = "merchant" if line else None
        if not line:
            line = _CATEGORY_TO_LINE.get((r["cat"] or "").upper())
            source = "category" if line else None
        out.append({
            "txn_id": r["id"], "date": r["date"], "amount": float(r["amount"]),
            "payee": payee, "category": r["cat"],
            "suggested_line": line, "source": source,
            # the bucket is NOT a guess — it falls out of the entity's start
            # date (costs before it are pre-operating). Returned so a confirm
            # is one action: classify() requires a bucket, and asking the user
            # to restate a date-derived fact would be busywork.
            "suggested_bucket": default_bucket(start, r["date"]),
        })
    return out


def txn_belongs_to_entity(conn, entity_id: str, txn_id: str) -> bool:
    """Whether this transaction is this entity's to classify.

    business_txn_class is keyed by (tenant, txn_id) alone, so without this
    check one entity's classify endpoint could write — and, the write being
    an upsert, REWRITE — another entity's rows, including those of an
    archived (read-only) entity whose own endpoint refuses the write.
    Same scoping idea as equity.delete_movement ("scope by entity too, not
    just tenant via RLS"), and ownership is _BIZ_TXN's own notion: the
    per-row override first, else the account's assignment."""
    return conn.execute(
        f"SELECT 1 FROM transactions t WHERE t.id = %(tid)s AND {_BIZ_TXN}",
        {"eid": entity_id, "tid": txn_id}).fetchone() is not None


def classify(conn, txn_id: str, bucket: str, *, sched_c_line=None,
             note=None) -> None:
    if bucket not in BUCKETS:
        raise ValueError(f"bucket must be one of {BUCKETS}")
    # closed taxonomy: Schedule C Part II is a printed list of lines, so an
    # invented one cannot be reported and must not be stored
    if sched_c_line is not None and sched_c_line not in SCHED_C_LINES:
        raise ValueError(f"sched_c_line must be one of {SCHED_C_LINES}")
    # COALESCE, not EXCLUDED: `sched_c_line`/`note` are OPTIONAL arguments, so
    # a caller that only changes the bucket (the Books tab's bucket dropdown)
    # passes None for them — and with EXCLUDED that None would overwrite a
    # Schedule C line the user had already confirmed, silently demoting the
    # row back to Unclassified on the P&L and the year-end package. Clearing
    # a line should be an explicit request, never a side effect of omitting
    # an argument.
    conn.execute(
        """INSERT INTO business_txn_class (txn_id, bucket, sched_c_line, note)
           VALUES (%s,%s,%s,%s)
           ON CONFLICT (tenant_id, txn_id) DO UPDATE SET
             bucket = EXCLUDED.bucket,
             sched_c_line = COALESCE(EXCLUDED.sched_c_line,
                                     business_txn_class.sched_c_line),
             note = COALESCE(EXCLUDED.note, business_txn_class.note),
             updated_at = now()""",
        (txn_id, bucket, sched_c_line, note))


# The row's amount net of partial reimbursement links, keeping the ledger
# sign: a charge counts only what was not paid back (reporting.NET_AMOUNT)
# and a deposit only the part no charge claimed (reporting.INCOME_NET). The
# books must net both sides the way the personal readers do — gross on
# both overstates Schedule C receipts and expenses by the repaid amount,
# and once links use up the whole deposit (it turns TRANSFER_IN and leaves
# revenue) a gross expense would understate net profit. `amount` stays
# gross beside it, because the ledger shows what the bank recorded.
_NET_SIGNED = (f"CASE WHEN t.amount > 0 THEN {reporting.NET_AMOUNT} "
               f"WHEN t.amount < 0 THEN -({reporting.INCOME_NET}) "
               "ELSE t.amount END")


def _net(t: dict) -> float:
    """A worksheet row's netted amount; falls back to the gross for a row
    built without it (a caller-supplied list)."""
    n = t.get("net_amount")
    return t["amount"] if n is None else n


def entity_transactions(conn, entity_id: str, year: int | None = None) -> list[dict]:
    """Every business transaction (expense or revenue) with its bucket — the
    classification worksheet + P&L input.

    `bucket` is the user's class (None when unclassified);
    `effective_bucket` is the one the P&L counts the row in — the class,
    else the start-date default for a cost, else (a refund) the bucket of
    the cost it reverses — "operating" for a refund of an organizational
    or start-up cost that arrives after the year the deduction is taken,
    a recovery in its own year; None for money in that is not a refund. A client
    that totals a period from these rows reads `effective_bucket` rather
    than re-deriving it, so it cannot re-bucket a refund by its date."""
    rows = _select_rows(conn, entity_id, year)
    start = _entity_start(conn, entity_id)
    refunds = [r for r in rows if r["amount"] < 0
               and _inflow_role(r) == "refund"]
    index: dict = {}
    cutoff = None
    if any(not r["bucket"] for r in refunds):
        # a year's refund may reverse last year's cost: search the whole
        # history, read only when a refund actually needs it
        history = rows if year is None else _select_rows(conn, entity_id, None)
        index = _cost_index(history, start)
        cutoff = _deduction_year_end(start, history)
    refund_ids = {r["id"] for r in refunds}
    for r in rows:
        if r["id"] in refund_ids:
            r["effective_bucket"] = _refund_bucket(r, start, index, cutoff)
        elif r["amount"] > 0:
            r["effective_bucket"] = r["bucket"] or default_bucket(
                start, _row_date(r))
        else:
            r["effective_bucket"] = None
    return rows


def _select_rows(conn, entity_id: str, year: int | None) -> list[dict]:
    params: dict = {"eid": entity_id}
    yr = ""
    if year:
        yr = " AND EXTRACT(YEAR FROM t.date) = %(yr)s"
        params["yr"] = year
    rows = conn.execute(
        f"""SELECT t.id, t.date, t.amount, {_NET_SIGNED} AS net_amount,
                   {_md.DISPLAY_MERCHANT} AS payee, {_md.MERCHANT_LOGO} AS merchant_logo,
                   COALESCE(t.category_override, t.category_primary) AS category,
                   t.category_detailed AS cat_detailed,
                   {reporting.ORIGINAL_CAT} AS cat_original,
                   {reporting.PARTLY_CLAIMED_DEPOSIT} AS partly_claimed,
                   tn.note AS note,
                   c.bucket, c.sched_c_line
            FROM transactions t {_md.MC_JOIN}
                 LEFT JOIN business_txn_class c ON c.txn_id = t.id
                 LEFT JOIN transaction_notes tn ON tn.txn_id = t.id
            WHERE {_BIZ_TXN} AND t.removed = 0{yr}
            ORDER BY t.date DESC, t.amount DESC""", params).fetchall()
    return [{"id": r["id"], "date": r["date"].isoformat() if r["date"] else None,
             "amount": float(r["amount"] or 0),
             "net_amount": float(r["net_amount"] or 0), "payee": r["payee"],
             "merchant_logo": r["merchant_logo"],
             "category": r["category"], "cat_detailed": r["cat_detailed"],
             "cat_original": r["cat_original"], "note": r["note"],
             "partly_claimed": bool(r["partly_claimed"]),
             "bucket": r["bucket"], "sched_c_line": r["sched_c_line"]}
            for r in rows]


def startup_deduction(total: float) -> dict:
    """§195/§248: up to $5,000 deductible in year one, phased out dollar-for-
    dollar once total costs exceed $50,000; the remainder amortizes over 180
    months. Informational — not tax advice."""
    total = round(float(total or 0), 2)
    immediate = max(0.0, min(5000.0 - max(0.0, total - 50000.0), total))
    amortizable = round(total - immediate, 2)
    return {"total": total, "immediate": round(immediate, 2),
            "amortizable": amortizable,
            "monthly_amortization": round(amortizable / 180, 2)
            if amortizable else 0.0}


def _lifetime_scan(conn, entity_id: str, rows=None) -> dict:
    """Every year's organizational and start-up cost for this entity, plus
    the date of the latest one. One pass over the entity's rows, applying
    the same bucket rule and refund netting the year-scoped P&L uses.
    `rows` is the entity's all-history list when the caller has it."""
    if rows is None:
        rows = entity_transactions(conn, entity_id, None)
    start = _entity_start(conn, entity_id)
    index = _cost_index(rows, start)
    cutoff = _deduction_year_end(start, rows)
    out = {"organizational": 0.0, "startup_195": 0.0,
           "last": _last_capitalized(rows, start)}
    for t in rows:
        amt = t["amount"]
        if amt is None or amt == 0:
            continue
        if amt < 0:
            # money in touches a capitalized total only as a refund of a
            # cost — by the same rule the year-scoped P&L applies
            if _inflow_role(t) != "refund":
                continue
        else:
            cat = (t["category"] or "").upper()
            if cat in ("TRANSFER_OUT", "TRANSFER_IN"):
                continue
            if (t.get("cat_detailed") or "") == \
                    "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT":
                continue
        d = dt.date.fromisoformat(t["date"]) if t["date"] else None
        # SAME bucket rule the year-scoped loop uses — explicit class first,
        # else the default from the business start date; a refund takes
        # the bucket of the cost it reverses — unless it arrives after the
        # deduction year, when it is that year's recovery, not a change to
        # an allowance already claimed
        kind = (_refund_bucket(t, start, index, cutoff) if amt < 0
                else t["bucket"] or default_bucket(start, d))
        if kind in _CAPITALIZED:
            out[kind] += _net(t)
    out["organizational"] = round(out["organizational"], 2)
    out["startup_195"] = round(out["startup_195"], 2)
    return out


def _lifetime_startup(conn, entity_id: str, kind: str,
                      year: int | None, this_year: float,
                      scan: dict | None = None) -> float:
    """Every year's organizational / start-up cost for this entity, not just
    `year`'s.

    §195 and §248 are a ONE-TIME allowance over the life of the business:
    $5,000 immediate, phased out dollar-for-dollar past $50,000, remainder
    amortized. Applying that rule to a single year's slice hands the
    allowance out again every year the business books pre-opening costs.

    When `year` is None the caller already has the lifetime figure, so this
    is a pass-through.
    """
    if year is None:
        return this_year
    if scan is None:
        scan = _lifetime_scan(conn, entity_id)
    return scan[kind]


def business_begins(start, scan: dict) -> dt.date | None:
    """When the business began, for §195/§248 timing: the entity's start
    date, else (no start date recorded) the month after its latest
    pre-opening cost — the costs are pre-opening by construction."""
    if start:
        return start
    last = scan.get("last")
    if last is None:
        return None
    return (dt.date(last.year + 1, 1, 1) if last.month == 12
            else dt.date(last.year, last.month + 1, 1))


def deduction_for_year(ded: dict, year: int, begins: dt.date | None) -> dict:
    """One tax year's share of a lifetime §195/§248 allowance.

    The immediate part is taken once, in the year the business begins; the
    remainder amortizes over the 180 months starting that month. Any other
    year shows no immediate deduction — repeating the lifetime figure in
    every year that books a pre-opening cost would claim it once per year.
    `total` and `amortizable` stay lifetime figures (they describe the
    allowance); `immediate`, `amortization_this_year` and
    `deductible_this_year` are this year's."""
    out = dict(ded)
    if begins is None:
        out.update(immediate=0.0, amortization_this_year=0.0,
                   deductible_this_year=0.0, begins=None)
        return out
    immediate = ded["immediate"] if year == begins.year else 0.0
    first = begins.year * 12 + begins.month - 1
    last = first + 179
    months = max(0, min(last, year * 12 + 11) - max(first, year * 12) + 1)
    amort = round(ded["monthly_amortization"] * months, 2)
    out.update(immediate=round(immediate, 2), amortization_this_year=amort,
               deductible_this_year=round(immediate + amort, 2),
               begins=begins.isoformat())
    return out


def pnl(conn, entity_id: str, year: int | None = None, txns=None) -> dict:
    """Full P&L, including the capital block."""
    # The all-history rows are read ONCE and shared: the year's §195/§248
    # scan and the capital account's cumulative net income both need
    # them, and each reading its own copy made one Business page load
    # scan the entity's whole history several times over.
    if year is None:
        if txns is None:
            txns = entity_transactions(conn, entity_id, None)
        lifetime = txns
    else:
        lifetime = entity_transactions(conn, entity_id, None)
    out = _pnl_core(conn, entity_id, year, txns, lifetime=lifetime)
    from . import equity
    out["capital"] = equity.capital_summary(conn, entity_id,
                                            lifetime=lifetime)
    return out


def _pnl_core(conn, entity_id: str, year: int | None = None,
              txns=None, lifetime=None) -> dict:
    """Revenue vs categorized expenses over a period, WITHOUT the capital
    block. Split out because the capital account includes retained
    earnings, so equity.capital_summary needs net income — and calling the
    full pnl() from there would recurse (pnl → capital_summary → pnl).
    Org and start-up costs are broken out separately (they're capitalized,
    not ordinary operating deductions — see startup_deduction). `txns` lets
    a caller (the CSV export) pass an already-fetched list, `lifetime`
    the entity's all-history list for the §195/§248 scan."""
    if txns is None:
        txns = entity_transactions(conn, entity_id, year)
    start = _entity_start(conn, entity_id)
    # The entity's whole history, read once: the §195/§248 scan needs it,
    # and so does a refund whose reversed cost sits in an earlier year.
    if lifetime is None:
        lifetime = (txns if year is None
                    else entity_transactions(conn, entity_id, None))
    index = _cost_index(lifetime, start)
    cutoff = _deduction_year_end(start, lifetime)

    revenue = 0.0
    operating = 0.0
    org_total = 0.0
    startup_total = 0.0
    by_line: dict[str, float] = {}
    # A refund with no Schedule C line of its own lands on the line its
    # merchant's charges were filed under, so a returned purchase takes the
    # line back to zero instead of opening a negative line beside it.
    merchant_line: dict = {}
    for t in txns:
        if (t["amount"] or 0) > 0 and t["sched_c_line"] and t.get("payee"):
            merchant_line.setdefault(t["payee"], t["sched_c_line"])
    for t in txns:
        amt = t["amount"]
        d = dt.date.fromisoformat(t["date"]) if t["date"] else None
        cat = (t["category"] or "").upper()
        # credit-card payments are a double-count (checked on the DETAILED
        # category, like every other spend query) — never revenue or expense
        is_cc_payment = (t.get("cat_detailed") or "") == \
            "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
        if amt < 0:                                   # money in = revenue
            role = _inflow_role(t)                    # (see _inflow_role)
            if role == "revenue":
                revenue += -_net(t)
            elif role == "refund":
                # a vendor refund reverses an expense (see
                # _REFUND_CATEGORIES); _net is negative here
                # it nets against the bucket (and line) of the cost it
                # reverses, not the bucket of its own date
                net = _net(t)
                bucket = _refund_bucket(t, start, index, cutoff)
                if bucket == "organizational":
                    org_total += net
                elif bucket == "startup_195":
                    startup_total += net
                else:
                    operating += net
                    cost = (None if t["bucket"]
                            else _reversed_cost(t, index))
                    line = (t["sched_c_line"]
                            or (cost[3] if cost else None)
                            or merchant_line.get(t.get("payee"))
                            or (t["category"] or "Uncategorized"))
                    by_line[line] = round(by_line.get(line, 0.0) + net, 2)
            continue
        # expense: bucket = explicit class, else default from start date
        bucket = t["bucket"] or default_bucket(start, d)
        if cat in ("TRANSFER_OUT", "TRANSFER_IN") or is_cc_payment:
            continue
        net = _net(t)
        if bucket == "organizational":
            org_total += net
        elif bucket == "startup_195":
            startup_total += net
        elif net > 0:
            # a charge paid back in full nets to nothing — no empty line
            operating += net
            line = t["sched_c_line"] or (t["category"] or "Uncategorized")
            by_line[line] = round(by_line.get(line, 0.0) + net, 2)

    # A refund and the charge it reverses net a line to zero whichever the
    # rows arrive in (newest first, so the refund usually comes first):
    # drop the empty line once every row is in, not only when a refund
    # happens to be the last to touch it.
    by_line = {k: v for k, v in by_line.items() if abs(v) >= 0.005}
    revenue = round(revenue, 2)
    operating = round(operating, 2)
    scan = (_lifetime_scan(conn, entity_id, lifetime)
            if year is not None else None)
    org_ded = startup_deduction(
        _lifetime_startup(conn, entity_id, "organizational",
                          year, org_total, scan))
    su_ded = startup_deduction(
        _lifetime_startup(conn, entity_id, "startup_195",
                          year, startup_total, scan))
    if year is not None:
        begins = business_begins(start, scan)
        org_ded = deduction_for_year(org_ded, year, begins)
        su_ded = deduction_for_year(su_ded, year, begins)
    return {"year": year, "revenue": revenue,
            "operating_expenses": operating,
            "operating_by_line": by_line,
            "net_operating": round(revenue - operating, 2),
            "organizational": round(org_total, 2),
            "startup_195": round(startup_total, 2),
            # LIFETIME totals, not this year's slice. §195/§248 is a
            # one-time allowance over the life of the business: judged on
            # a single `year`'s transactions, cost spread over several years
            # would claim the immediate allowance once per year and never
            # meet the $50k phase-out. For one year, the immediate part is
            # that year's only if the business began in it
            # (deduction_for_year).
            "organizational_deduction": org_ded,
            "startup_deduction": su_ded}
