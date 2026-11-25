"""Reporting engine: net worth, spending, cash flow & savings, fees.

Computes the four report families (net worth, spending, cash flow & savings,
investment fees). Reuses the project's spend definition and budget engine —
no business logic forked.

Net-worth CURRENT is exact (account balances). Net-worth OVER TIME is a
holdings-based reconstruction (see `_reconstruct_trend`): every account is
anchored to its exact current value and walked backward — investments through
their share-level transaction history valued at historical prices (so market
growth and drawdowns are captured), cash/cards through their own raw flows. It
is a reconstruction, not a recorded balance history, and labelled as such.

Structure worth knowing:
  * reports are computed on demand (web/reporting_api.py) plus a nightly
    snapshot job — there is no materialized report cache.
  * table DDL (holdings, crypto_holdings, networth_snapshot,
    networth_recorded, income_annual, merchant_canonical) lives in the
    schema/migrations — engine code never CREATEs tables.
  * merchant_dedup's MC_JOIN / DISPLAY_MERCHANT SQL fragments are imported
    from that module and re-exported below, so there is one definition.
"""
from __future__ import annotations

import bisect
import calendar
import datetime as dt
import hashlib
import json
import logging
import math
import threading
import re

import psycopg

from . import budget
from . import categories as _categories
from . import coverage
from . import paychecks as _paychecks

log = logging.getLogger(__name__)
from .compat import as_date, as_dict, jsonb

# ---- spend definition (mirrors budget._spend_rows) ----------------------
# shadow exclusion: drop transactions belonging to a
# linked account that isn't its group's primary — app.shadow_ids is set
# per tenant_connect (empty when nothing is linked → no-op).
_NOT_SHADOW = ("""
  AND ((SELECT NULLIF(current_setting('app.shadow_ids', true), '')) IS NULL
       OR NOT ({col} = ANY((SELECT string_to_array(
               current_setting('app.shadow_ids', true), ','))::text[])))""")
# The session variable is read through scalar subqueries on purpose: written
# bare, current_setting() is re-evaluated for every row of a full-table scan;
# as a subquery it becomes an InitPlan the planner evaluates once per
# statement. Same value, same meaning, a fraction of the per-row cost.
SPEND_WHERE = ("""t.removed = 0 AND t.amount > 0
  AND COALESCE(t.category_detailed,'') != 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
  AND COALESCE(t.category_override, t.category_primary, '')
      NOT IN ('TRANSFER_OUT','TRANSFER_IN')
  AND """ + budget.NOT_A_NON_SPEND_ACCOUNT_SQL
  + _NOT_SHADOW.format(col="t.account_id")
  # exclude business-entity money from personal reports (same
  # predicate _spend_rows uses — imported from budget so they can't drift).
  + budget.PERSONAL_ONLY_SQL)
# REPLACE spans the whole COALESCE so underscore-form overrides
# ('RENT_AND_UTILITIES') display-merge with native primaries ('RENT AND
# UTILITIES'); Amazon overrides carry no underscores so are unaffected.
EFF_CAT = "REPLACE(COALESCE(t.category_override, t.category_primary,'?'),'_',' ')"
# The same, per PART of a hand-split row (categories.SPLIT_JOIN in the
# query, and the amount read through PART_NET_JOINED): a rollup BY
# CATEGORY takes these two together, so a split row lands in each of its
# categories for its share. A rollup by anything else (month, merchant)
# keeps EFF_CAT / NET_AMOUNT_JOINED and no join — the parts sum to the row,
# and joining would only multiply it.
PART_CAT = _categories.PART_CAT_DISPLAY
SPLIT_JOIN = _categories.SPLIT_JOIN

# What the AGGREGATOR called the row, before any rule touched it: Plaid's own
# personal_finance_category primary (the raw payload outlives every later
# re-stamp), else the category_plaid column bank sync wrote. NULL for rows
# no aggregator labelled (CSV imports), so a predicate built on it must be
# a no-op there. `t` must be the transactions alias.
ORIGINAL_CAT = ("COALESCE(t.raw #>> '{personal_finance_category,primary}', "
                "t.category_plaid)")
# Money in is only INCOME when the aggregator did not call it a transfer. A
# user's merchant rule may reclassify a TRANSFER row into a spend category
# (a person paid through a payment app is "Child care" to the household),
# and a merchant-wide rule reaches the occasional inflow under the same
# merchant too — a partial refund sent back. That inflow is no longer a
# transfer to the effective-category test, but it is still not income: a
# reclassified transfer counts as income only when the user explicitly made
# it INCOME (a per-row override — rules refuse flow categories).
NOT_RECLASSIFIED_TRANSFER_IN = f"""
  AND NOT (COALESCE({ORIGINAL_CAT}, '') IN ('TRANSFER_IN','TRANSFER_OUT')
           AND COALESCE(t.category_override, t.category_primary, '')
               <> 'INCOME')"""

# partial reimbursements net the received money off their expense
# row — the remainder stays real spend. Full pairs stay excluded by
# category (TRANSFER_*) as before; this expression only bites rows with
# partial pairs (correlated on the reimbursements PK prefix).
# partial-reimbursement net, floored at 0. Canonical definition
# lives in budget (reporting imports budget, not the reverse) — re-exported
# here under the name callers already use so the two cannot drift apart.
NET_AMOUNT = budget.NET_AMOUNT

# The deposit side of the same netting: a deposit partial-linked to charges
# counts as income only in the part no charge claimed. The claimed part is
# already netted off those charges' spend, so counting it as income too
# would count the repayment twice; dropping the whole deposit (a
# TRANSFER_IN category) would lose the part that really was income — a
# paycheck that carried one expense back with it. Floored at 0 for the
# same reason NET_AMOUNT is. `t` must be the transactions alias.
# A link to a charge that has left the ledger claims nothing (see
# budget.NET_AMOUNT).
INCOME_NET = """GREATEST(-t.amount - COALESCE((SELECT SUM(pr.amount)
    FROM reimbursements pr
    JOIN transactions pe ON pe.id = pr.expense_id AND pe.removed = 0
    WHERE pr.reimburse_id = t.id AND pr.partial = 1), 0), 0)"""

# The same net, as a join. NET_AMOUNT's correlated subquery is re-run for
# every output row (a SubPlan), which on an all-history aggregate means tens
# of thousands of probes of a table that is usually empty. Pre-aggregating
# the partial reimbursements once and LEFT JOINing them on the expense id
# computes the identical value — SUM of the same rows, COALESCEd to 0 and
# floored at 0 — in a single hash probe per row. Use the pair together:
# `FROM transactions t {REIMB_PARTIAL_JOIN}` and `SUM({NET_AMOUNT_JOINED})`.
# The full-scan aggregates in this module and the lenses use it; the budget
# engine's row reader has its own copy under another alias
# (budget.REIMB_NET_JOIN), and per-row readers of a few rows keep NET_AMOUNT.
# Like NET_AMOUNT, only links on a deposit still on the ledger count.
REIMB_PARTIAL_JOIN = ("LEFT JOIN (SELECT r.expense_id, SUM(r.amount) AS s "
                      "FROM reimbursements r JOIN transactions rd "
                      "ON rd.id = r.reimburse_id AND rd.removed = 0 "
                      "WHERE r.partial = 1 "
                      "GROUP BY r.expense_id) pr ON pr.expense_id = t.id")
NET_AMOUNT_JOINED = "GREATEST(t.amount - COALESCE(pr.s, 0), 0)"
# a part's share of the joined net (see PART_CAT)
PART_NET_JOINED = _categories.part_net(NET_AMOUNT_JOINED)

# merchant_dedup fragments — imported, ONE definition. Callers alias
# transactions as `t`, LEFT JOIN via MC_JOIN, group/label by DISPLAY_MERCHANT.
from . import merchant_dedup as _md  # noqa: E402
MC_JOIN = _md.MC_JOIN
DISPLAY_MERCHANT = _md.DISPLAY_MERCHANT
MERCHANT_LOGO = _md.MERCHANT_LOGO

# ROUND(double precision, n) doesn't exist on Postgres; the numeric
# round-trip keeps 2-decimal SQL rounding with float JSON output.
_R2 = "ROUND(({0})::numeric, 2)::float8"


def excluded_accounts_sql(conn, memo: dict | None = None) -> tuple[str, tuple]:
    """The Accounts page 'excl' toggle (budget config `excluded_accounts`) as
    a (SQL fragment, params) pair every report in this module appends to its
    predicate over `transactions t`.

    One helper, and every entry point calls it: every report applies the
    exclusion so figures on one page agree — the Overview's "went out" and
    the Spending tab's total for the same window drop the same rows. The
    fragment is empty when nothing is excluded, so a caller splices both
    parts unconditionally:

        excl, ex_p = excluded_accounts_sql(conn)
        conn.execute(f"... WHERE {SPEND_WHERE} AND t.date >= %s {excl}",
                     (start, *ex_p))

    `memo` is the request-scoped budget cache, when the caller already has
    one — the config must be read once per call, or the spend total and the
    fixed split can be judged under two different configs."""
    excluded = budget.excluded_account_ids(
        budget.load_config(conn, memo=memo))
    return (("AND NOT (t.account_id = ANY(%s))", (excluded,)) if excluded
            else ("", ()))


def _iso(v) -> str:
    """DATE columns arrive as datetime.date; the trend machinery compares
    ISO strings. Normalize at the row boundary."""
    if isinstance(v, dt.date):
        return v.isoformat()
    return str(v)[:10]


def asset_class(typ: str, subtype: str | None) -> str:
    subtype = (subtype or "").lower()
    if typ == "depository":
        return "Cash"
    if typ == "credit":
        return "Card debt"
    if typ == "investment":
        if subtype == "crypto":
            return "Crypto"
        if subtype in ("401a", "403b", "457b", "401k", "457", "ira",
                       "roth", "roth ira", "roth 401k", "roth 403b",
                       "sep ira", "simple ira", "pension", "retirement",
                       "keogh", "sarsep", "profit sharing plan",
                       "thrift savings plan"):
            return "Retirement"
        # tax-advantaged wrappers that are neither a retirement plan nor
        # taxable money
        if subtype in ("hsa", "hra"):
            return "Health savings"
        if subtype in ("529", "education savings account"):
            return "Education savings"
        return "Taxable investments"
    return "Other"


# ---- net worth ----------------------------------------------------------
def _live_accounts(conn, owner: str | None = None):
    """Accounts with a real current balance (exclude synthetic/historical items
    whose balances are NULL: imported history, closed lineages).
    Linked shadow accounts are excluded — the same real-world
    money must not be counted once per source. An owner filter restricts
    the result to that owner's accounts (a reporting lens, not a money
    change)."""
    from . import links
    shadows = links.shadow_ids(conn)
    q = """SELECT a.id, COALESCE(a.display_name, a.name) AS name,
                  a.type, a.subtype, a.balance_current AS bal,
                  -- a connection that never said its institution's name
                  -- is listed as one, never as a nameless row; whether it
                  -- gave one keeps it apart from an institution that is
                  -- named "Other"
                  COALESCE(NULLIF(i.institution_name, ''), 'Other') AS inst,
                  NULLIF(i.institution_name, '') IS NULL AS inst_unnamed
           FROM accounts a JOIN items i ON i.id=a.item_id
           WHERE a.balance_current IS NOT NULL
             AND (""" + budget.COMBINE_OR + """a.entity_id IS NULL)  -- business accounts out of personal net worth (unless combined)
             AND NOT (a.id = ANY(%s))"""
    args: list = [shadows]
    if owner:
        q += " AND a.owner = %s"
        args.append(owner)
    return conn.execute(q, args).fetchall()


# A typed-in loan balance asks for an update once this many calendar
# months have begun since the month it was entered: a quarter.
LOAN_UPDATE_MONTHS = 3


def _entered(as_of, today: dt.date) -> tuple[str | None, bool, dt.date | None]:
    """(words, update due, the day) for a hand-entered loan balance last
    stated on `as_of`: "entered 06/26", and "entered 06/26 — update" once
    LOAN_UPDATE_MONTHS have begun since. (None, False, None) when the row
    carries no readable date."""
    # A balance stamped before the editor kept the day carries the month
    # alone ("2026-06"); it is as readable an age as a full date.
    text = str(as_of)[:10]
    try:
        day = dt.date.fromisoformat(text if len(text) > 7 else f"{text}-01")
    except (TypeError, ValueError):
        return None, False, None
    due = ((today.year * 12 + today.month) - (day.year * 12 + day.month)
           >= LOAN_UPDATE_MONTHS)
    words = f"entered {day:%m/%y}"
    return (f"{words} — update" if due else words), due, day


def _property_items(conn, live_loans, today: dt.date | None = None) -> list[dict]:
    """The property layer: real estate + vehicles − mortgage, as rows.

    Values from tenant config `manual_assets` (user-editable). A live
    aggregator loan account replaces the manual mortgage entry automatically
    once linked. Shared by the report and the nightly snapshot, so the
    property net a snapshot records is the one the page showed that day.

    A hand-entered mortgage says how old its balance is (`entered_text`,
    `update_due`; specs/counted-figures.md, "A typed-in loan balance says
    how old it is"); every other row carries neither."""
    today = today or _household_today(conn)
    try:
        manual = budget.load_config(conn).get("manual_assets", [])
    except Exception:
        manual = []
    # A live MORTGAGE supersedes a manual mortgage entry — an auto or student
    # loan does not. Testing `live_loans` as a whole would let any loan of any
    # subtype delete the manual mortgage from net worth and then relabel that
    # unrelated loan "mortgage": a $300k home loan vanishing because a $20k
    # car loan arrived, overstating net worth by the mortgage and lying about
    # what the remaining row is.
    live_mortgages = [r for r in live_loans if (r["subtype"] or "") == "mortgage"]
    prop_items = []
    for m in manual:
        if not isinstance(m, dict):
            continue
        if m.get("kind") == "mortgage" and live_mortgages:
            continue                              # live balance supersedes
        # The Settings-save path runs every asset value through _num(), but a
        # restored/hand-edited config can carry a non-numeric value straight
        # into the tenant — a bad row must be skipped, not 500 the net-worth
        # dashboard (one of the hottest endpoints) and the AI assistant.
        try:
            value = round(float(m.get("value") or 0), 2)
        except (ValueError, TypeError):
            continue
        words, due, day = (_entered(m.get("as_of"), today)
                           if m.get("kind") == "mortgage"
                           else (None, False, None))
        prop_items.append({"name": m.get("name", "?"), "kind": m.get("kind", "?"),
                           "value": value,
                           "source": f"manual ({m.get('as_of', '?')})",
                           "entered_text": words, "update_due": due,
                           "_entered_on": day})
    for ln in live_loans:
        # Carry the real subtype through — NetWorth.tsx filters property_items
        # on kind == 'mortgage' for its "incl. mortgage payoff" caption, so a
        # mislabelled auto loan is a wrong number on the page, not just a tag.
        prop_items.append({"name": f"{ln['inst']} {ln['name']}",
                           "kind": (ln["subtype"] or "loan") or "loan",
                           "value": round(-(ln["bal"] or 0), 2), "source": "live (Plaid)",
                           "entered_text": None, "update_due": False,
                           "_entered_on": None})
    return prop_items


def _mortgage_entered_text(prop_items: list) -> str | None:
    """The oldest typed-in mortgage's age words, for beside the payoff
    figure the mortgage rows add up to; None when none is typed in."""
    dated = [p for p in prop_items if p.get("_entered_on")]
    return (min(dated, key=lambda p: p["_entered_on"])["entered_text"]
            if dated else None)


# The Over-time card's timeframes, counted back from the trend's last point
# INCLUSIVE (the series keeps the month in progress): "cur" is the change
# since the last month-end, "1m" the month before that one.
_TREND_RANGE_MONTHS = {"cur": 1, "1m": 2, "3m": 3, "6m": 6, "1y": 12,
                       "3y": 36, "5y": 60, "all": None}


def _ym_back(ym: str, n: int) -> str:
    t = int(ym[:4]) * 12 + int(ym[5:7]) - 1 - n
    return f"{t // 12:04d}-{t % 12 + 1:02d}"


def _trend_window(points: list, range_key: str) -> dict:
    """One Over-time timeframe over a monthly series: `start` is the index
    of the baseline point (the last one at or before the cutoff), `end` the
    last point drawn — the newest, except for "1m", the last COMPLETE
    month, which ends at the previous month-end — `gain` the change
    between them, `pct` it over a positive base, `full` true when the
    window is the whole series (the picker greys it). The figure the card
    prints; clients draw points[start..end] and print these."""
    n = len(points)
    if n < 2:
        return {"start": 0, "end": max(n - 1, 0), "gain": None, "pct": None,
                "full": True, "from_text": None}

    def at(cutoff: str) -> int:
        for i in range(n - 1, -1, -1):
            if points[i][0] <= cutoff:
                return i
        return -1

    start, end = 0, n - 1
    months = _TREND_RANGE_MONTHS[range_key]
    if months is not None:
        last = points[-1][0][:7]
        start = max(0, at(_ym_back(last, months)))
        if range_key == "1m":
            close = at(_ym_back(last, 1))
            if close > start:
                end = close
    base = points[start][1]
    gain = round(points[end][1] - base, 2)
    return {"start": start, "end": end, "gain": gain,
            "pct": round(100 * gain / base, 2) if base > 0 else None,
            "full": start == 0,
            "from_text": _from_words(base, points[start][0])}


def _from_words(base: float, month: str) -> str | None:
    """Where a window's gain is measured from, said when no percent can be
    (the base is not above zero): "from −$4,200 in 03/08". None when the
    percent gives the base (specs/counted-figures.md, "A gain with no
    percent says what it is measured from")."""
    if base > 0:
        return None
    d = whole_dollars(base)
    return (f"from {'−' if d < 0 else ''}${abs(d):,} "
            f"in {month[5:7]}/{month[2:4]}")


def shown_parts(values: list, total: float) -> list[float]:
    """`values` rounded to the cent so that, as shown, they add up exactly
    to `total` as shown (rounded to the cent): largest-remainder rounding.
    Each part is first cut down to the cent below it; the cents still
    missing go one each to the parts that lost the most (earlier parts
    first on a tie), or are taken back from those that lost the least.
    Rounding each part on its own lets parts carrying fractions of a cent
    (a feed reports a balance to three or four places) miss the total by a
    cent (specs/invariants-and-fuzzing.md, D1)."""
    import math
    if not values:
        return []
    cents = [round(float(v) * 100, 6) for v in values]
    base = [math.floor(c) for c in cents]
    rem = [c - b for c, b in zip(cents, base)]
    k = int(round(float(total) * 100)) - sum(base)
    order = sorted(range(len(base)), key=lambda i: (-rem[i], i))
    i = 0
    while k > 0:
        base[order[i % len(base)]] += 1
        k -= 1
        i += 1
    i = 0
    while k < 0:
        base[order[-1 - (i % len(base))]] -= 1
        k += 1
        i += 1
    return [b / 100 for b in base]


def _apportion(nums: list[int], den: int, target: int) -> list[int]:
    """Whole parts of `nums[i] / den` that add up to `target`: each cut
    down to the whole below it, the units still missing given one each to
    the parts that lost the most (earlier parts first on a tie), or taken
    back from those that lost the least."""
    base = [n // den for n in nums]
    order = sorted(range(len(nums)), key=lambda i: (-(nums[i] % den), i))
    k = target - sum(base)
    for i in range(abs(k)):
        j = order[i % len(nums)] if k > 0 else order[-1 - (i % len(nums))]
        base[j] += 1 if k > 0 else -1
    return base


def whole_dollars(amount: float) -> int:
    """`amount` in whole dollars, halves to even: what the clients'
    `money()` prints for it."""
    q, r = divmod(int(round(float(amount) * 100)), 100)
    return q + (1 if r > 50 or (r == 50 and q % 2) else 0)


def shown_dollars(values: list, total: float, *,
                  dollars: int | None = None) -> list[int]:
    """`values` (cent figures adding up to `total`) in whole dollars that,
    as shown, add up to `total` in whole dollars — or to `dollars`, when
    the total is itself a row of a list above: largest-remainder rounding
    (specs/counted-figures.md, "Lists add up in the dollars they show").
    Each part lands on the whole dollar below or above it, so a row is at
    most a dollar off its own rounding."""
    if not values:
        return []
    return _apportion([int(round(float(v) * 100)) for v in values], 100,
                      whole_dollars(total) if dollars is None else dollars)


def year_dollars(income: float | None, spend: float, saved: float | None,
                 *, every_month: bool) -> dict:
    """A year's income, spend and left over in the whole dollars the pages
    print — the Year lens's totals, its Total spend row and its change,
    Cash Flow's Left over by year and the yearly email read this one rule
    (specs/shown-figures.md, "A year prints its income and spend once").
    Income and spend are each their own whole dollars, as every other
    place that prints them alone rounds them; when every month's income is
    known, left over is income less spend, so it is the printed income
    less the printed spend (a dollar off its own rounding at most) —
    apportioning the three instead moved income or spend, so one year read
    two incomes on two pages whose spend differed by cents. With some
    month's income unknown, left over counts other months than spend, and
    each figure is its own rounding."""
    if every_month and income is not None and saved is not None:
        inc, out = whole_dollars(income), whole_dollars(spend)
        return {"income": inc, "spend": out, "saved": inc - out}
    return {"income": whole_dollars(income) if income is not None else None,
            "spend": whole_dollars(spend),
            "saved": whole_dollars(saved) if saved is not None else None}


def shown_shares(values: list, base: float | None = None) -> list[int | None]:
    """Each positive value's whole-percent share of the positive values'
    sum, adding up to 100 by largest remainder; None for the others (a
    ring draws only what is owned).

    With `base`, the shares are of that figure instead (a list whose
    parts are not the whole of what the percent is of): they add up to the
    positive values' own share of `base` in whole percent — 100 when they
    are all of it — still by largest remainder, so a row is at most a
    point off its own rounding."""
    cents = [int(round(float(v) * 100)) for v in values]
    drawn = [i for i, c in enumerate(cents) if c > 0]
    out: list[int | None] = [None] * len(values)
    if not drawn:
        return out
    whole = sum(cents[i] for i in drawn)
    den = whole if base is None else int(round(float(base) * 100))
    if den <= 0:
        return out
    q, r = divmod(100 * whole, den)
    target = q + (1 if 2 * r > den or (2 * r == den and q % 2) else 0)
    for i, p in zip(drawn, _apportion([100 * cents[i] for i in drawn],
                                      den, target)):
        out[i] = p
    return out


def change(current: float | None, previous: float | None) -> dict | None:
    """A figure against the same figure for an earlier period, as the
    page prints it (specs/shown-figures.md, "Changes vs before"): the
    `amount` is the shown figure minus the shown figure (each in whole
    dollars by `whole_dollars`), so it never disagrees with the two numbers
    beside it, and a $0 amount is `unchanged`. The `pct` is of the exact
    figures, rounded to the nearest whole percent with halves to even —
    the same way up as down, so a rise of 2.5% and a fall of 2.5% both
    read 2% — signed by the direction of the change; null when it is
    unchanged, when there was nothing before to compare with, and when
    the percent says nothing a reader can use: one that prints as 0%, or
    one at ±1000% and past it, or one measured from an earlier figure
    under CHANGE_PCT_FLOOR dollars (the dollars beside it carry those).
    None when there is no earlier figure."""
    if previous is None or current is None:
        return None
    amount = whole_dollars(current) - whole_dollars(previous)
    if amount == 0:
        return {"amount": 0, "pct": None, "unchanged": True}
    cur_c = int(round(float(current) * 100))
    prev_c = int(round(float(previous) * 100))
    pct = None
    if abs(prev_c) >= CHANGE_PCT_FLOOR * 100:
        num, den = 100 * abs(cur_c - prev_c), abs(prev_c)
        q, r = divmod(num, den)
        q += 1 if 2 * r > den or (2 * r == den and q % 2) else 0
        if 0 < q < CHANGE_PCT_CEILING:
            pct = q if amount > 0 else -q
    return {"amount": amount, "pct": pct, "unchanged": False}


# a change's percent is printed below this; at it and past it the dollars
# say what a four-digit percent cannot
CHANGE_PCT_CEILING = 1000
# and only when the earlier figure it is measured from is at least this
# many dollars: a percent of a few dollars describes nothing
CHANGE_PCT_FLOOR = 50


def _listed(ranked: list) -> list:
    """The ranked payers a tab lists as rows (`_fold_merchants` output):
    one whose window amount prints as $0 — a few cents of interest, a
    charge of a few cents — is not a row. Totals and shares are
    decided over every payer before this cut."""
    return [(k, f) for k, f in ranked if whole_dollars(f["amt"])]


# what "What changed" lists: the four largest category moves and the three
# largest merchants paid only before, each moved by at least this much
MOVER_FLOOR = 25.0
MOVER_SHARE = 0.005


def _movers(total: float, categories: list, dropped: list) -> list[dict]:
    """The rows the Spending tab's "What changed" card prints: every
    category (one spent on only before included, at 0) and every merchant
    paid only before, kept when the exact change is at least
    max($25, 0.5% of the window's spend), the four largest categories
    then the three largest merchants by the size of that change, each
    with its change by `change`."""
    floor = max(MOVER_FLOOR, total * MOVER_SHARE)

    def pick(rows, kind, n):
        moved = [(label, cur, prev or 0.0) for label, cur, prev in rows]
        moved = [m for m in moved if abs(m[1] - m[2]) >= floor]
        moved.sort(key=lambda m: -abs(m[1] - m[2]))
        return [{"kind": kind, "label": label, "current": cur,
                 "previous": prev, "change": change(cur, prev)}
                for label, cur, prev in moved[:n]]
    return (pick([(c, v, p) for c, v, p in categories], "category", 4)
            + pick([(r[0], r[1], r[3]) for r in dropped], "merchant", 3))


def _ring(rows: list) -> dict:
    """A breakdown ring's figures: what it draws (the owned rows), what is
    listed under it as owed, and the net — which is the page's Financial
    accounts total, the sum of every row. `shown` is the same list as the
    page prints it: whole dollars adding up to the net, and the drawn
    rows' shares adding up to 100."""
    owned = round(sum(v for _k, v in rows if v > 0), 2)
    owed = round(sum(v for _k, v in rows if v < 0), 2)
    net = round(owned + owed, 2)
    values = [v for _k, v in rows]
    shown = [{"label": k, "dollars": d, "share": p, "owed": v < 0}
             for (k, v), d, p in zip(rows, shown_dollars(values, net),
                                     shown_shares(values))]
    return {"owned": owned, "owed": owed, "net": net,
            "shown": {"rows": shown,
                      "owned": sum(r["dollars"] for r in shown
                                   if r["share"] is not None),
                      "owed": sum(r["dollars"] for r in shown if r["owed"]),
                      "net": whole_dollars(net)}}


def _networth_figures(series: list, full_series: list, estimated_until: int,
                      by_class: list, by_inst: list,
                      prop_items: list, added: list = (),
                      manual_step: float | None = None) -> dict:
    """The figures the Net Worth page prints over its series, decided here
    once (specs/counted-figures.md): the headline's change this month on
    the total it sits beside (property included), the Financial accounts
    tile's on the financial-only trend, the property tile's as the
    difference (a mortgage payment is cash down there and loan down here),
    each ring's net, and the Over-time card's gain per timeframe — over
    `full_trend`, the same total as the headline, except across a month
    the hand-entered property value moved (`added`, _property_steps;
    _like_for_like)."""
    def month_change(s):
        return (round(s[-1][1] - s[-2][1], 2)
                if len(s) >= 2 and s[-1][1] is not None
                and s[-2][1] is not None else None)
    total_ch = month_change(full_series)
    fin_ch = month_change(series)
    prop_ch = (round(total_ch - fin_ch, 2)
               if total_ch is not None and fin_ch is not None else None)
    # a hand-entered value typed in, taken out or re-valued this month is
    # not money gained or lost this month (specs/counted-figures.md, "The
    # headline's change this month follows the same rule"): that step —
    # and only it — comes off the headline and the property tile, so a
    # linked loan's paydown the same month still counts
    if (total_ch is not None and fin_ch is not None and manual_step
            and any(e[0] == full_series[-1][0][:7] for e in added)):
        total_ch = round(total_ch - manual_step, 2)
        prop_ch = round(prop_ch - manual_step, 2)
    # full_trend starts where `trend` does, less the months cut off before
    # the last guessed property value: the estimate boundary rebased
    offset = len(series) - len(full_series)
    return {
        "month_change": total_ch,
        "financial_month_change": fin_ch,
        "property_month_change": prop_ch,
        # what the mortgage rows take off the property layer, as owed
        "mortgage_payoff": round(-sum(p["value"] for p in prop_items
                                      if p.get("kind") == "mortgage"), 2),
        "full_trend_estimated_until": max(0, estimated_until - offset),
        "trend_windows": {k: _like_for_like(_trend_window(full_series, k),
                                            full_series, series[offset:],
                                            added)
                          for k in _TREND_RANGE_MONTHS},
        "rings": {"by_asset_class": _ring(by_class),
                  "by_institution": _ring(by_inst)},
    }


def compute_networth(conn, owner: str | None = None,
                     today: dt.date | None = None, *,
                     trend: bool = True, history: bool = False) -> dict:
    """The household's net worth now, by institution and asset class,
    with the property layer — and, unless `trend` is off, the history:
    the holdings walk back through every investment row, the recorded
    and nightly overlays, and the cumulative-savings proxy. The history
    is most of the cost, and a caller that wants only today's figures
    (the integrations summary, the Prometheus gauges) leaves it out; the
    series keys and the P/L panels are then present and empty, so the
    shape holds. The nightly-snapshot series and the cumulative-savings
    proxy are `history`: no page draws them today, and the savings proxy
    alone is three full-ledger scans."""
    rows = _live_accounts(conn, owner)
    by_inst, by_class = {}, {}
    total = 0.0
    live_loans = []          # loan accounts (e.g. mortgage) → property layer
    for r in rows:
        if r["type"] == "loan":
            live_loans.append(r)
            continue         # mortgages live in the property layer, not financial
        signed = (-r["bal"]) if r["type"] == "credit" else r["bal"]
        total += signed
        # grouped by the name AND whether it was given: the unnamed
        # connections are one "Other" row, and an institution named
        # "Other" is a row of its own (specs/counted-figures.md)
        key = (r["inst"], bool(r["inst_unnamed"]))
        by_inst[key] = by_inst.get(key, 0) + signed
        cls = asset_class(r["type"], r["subtype"])
        by_class[cls] = by_class.get(cls, 0) + signed
    total = round(total, 2)

    # ---- property layer: real estate + vehicles − mortgage ----------------
    # Values from tenant config `manual_assets` (user-editable). A live
    # aggregator loan account replaces the manual mortgage entry automatically
    # once linked.
    # property + the historical trend are HOUSEHOLD-level and not
    # owner-tagged, so an owner lens shows only that owner's live accounts.
    # each list, as shown, adds up to the total above it
    inst_rows = sorted(((label, part) for (label, _unnamed), part in zip(
                           by_inst, shown_parts(list(by_inst.values()),
                                                total))),
                       key=lambda x: -x[1])
    class_rows = sorted(zip(by_class, shown_parts(list(by_class.values()),
                                                  total)),
                        key=lambda x: -x[1])
    inst_rows = [list(r) for r in inst_rows]
    class_rows = [list(r) for r in class_rows]
    if owner:
        return {
            "current_total": total,
            "property_items": [], "property_net": 0.0,
            "full_total": total,
            "by_institution": inst_rows,
            "by_asset_class": class_rows,
            **_networth_figures([], [], 0, class_rows, inst_rows, []),
            "month_change_text": None, "month_withdrawn_text": None,
            "mortgage_entered_text": None,
            "trend": [], "full_trend": [], "coinbase_pl": [],
            "retirement_combined": {},
            "trend_estimated_until": 0, "snapshot_trend": [],
            "savings_trend": [], "owner": owner,
        }
    prop_items = _property_items(conn, live_loans, today)
    property_net = round(sum(p["value"] for p in prop_items), 2)
    entered_text = _mortgage_entered_text(prop_items)
    for p in prop_items:
        del p["_entered_on"]          # the day is internal; the words go out

    series = (_reconstruct_trend(conn, total, today=today)   # anchored-exact-today, back-cast
              if trend else [])
    # the same months on the base of the Total net worth figure: what the
    # page's headline change is measured on, so a mortgage payment (cash
    # out, loan balance down) reads as the interest it cost, not as the
    # whole payment lost
    full_series = []
    added = []
    manual_step = None
    if series:
        months = [m for m, _ in series]
        prop, manual = _property_layers(
            conn, months,
            _held_with_cash(conn, months,
                            _property_trend(conn, months, property_net,
                                            live_loans)),
            live_loans)
        # the months the hand-entered value moved, read off the layer the
        # line is drawn from
        added = _property_steps(
            months, manual,
            _manual_entry_months(conn, live_loans, months[-1]),
            exact=not live_loans)
        full_series = [[m, None if p is None else round(v + p, 2)]
                       for (m, v), p in zip(series, prop)]
        # this month's move in the hand-entered part (the headline takes
        # it off when it is a step, _networth_figures)
        if len(manual) >= 2 and None not in manual[-2:]:
            manual_step = round(manual[-1] - manual[-2], 2)
    # A month whose property value is only a guess has no figure, and the
    # clients measure the change off the last two points; so the series
    # starts after the last such month — when that is last month, the
    # current month stands alone and the page shows no change rather than
    # a guessed one.
    unknown = [i for i, (_, v) in enumerate(full_series) if v is None]
    if unknown:
        full_series = full_series[unknown[-1] + 1:]
    est_until = (_trend_estimated_until(conn, [m for m, _ in series])
                 if trend else 0)
    figures = _networth_figures(series, full_series, est_until, class_rows,
                                inst_rows, prop_items, added, manual_step)
    return {
        "current_total": total,
        "property_items": prop_items,
        "property_net": property_net,
        "full_total": round(total + property_net, 2),
        "by_institution": inst_rows,
        "by_asset_class": class_rows,
        **figures,
        # what the month's change is made of, and money withdrawn to the
        # bank that no linked account has received (specs/
        # counted-figures.md, "The month's change says what moved")
        "month_change_text": _month_change_text(conn, by_class, series,
                                                figures),
        "month_withdrawn_text": (
            _unarrived_withdrawals(conn, today or _household_today(conn))
            if figures["month_change"] is not None else None),
        "mortgage_entered_text": entered_text,
        "trend": series,
        # `trend` is financial accounts only (current_total's base);
        # `full_trend` adds the property layer month by month and ends at
        # full_total
        "full_trend": full_series,
        # the page's P/L panels, left out with the history: today's-figures
        # callers never read them, and the Coinbase one decodes every
        # crypto row's whole raw payload
        "coinbase_pl": _coinbase_pl(conn) if trend else [],
        "retirement_combined": _retirement_combined(conn) if trend else {},
        # index up to which the trend is a reconstruction (best guess). Everything
        # before the first REAL nightly snapshot is estimated; the recorded tail
        # grows one point per night. Whole line is estimated until snapshots accrue.
        "trend_estimated_until": est_until,
        "snapshot_trend": _snapshot_series(conn) if trend and history else [],
        "savings_trend": _cumulative_savings(conn) if trend and history else [],
    }


# The month change's parts, in the order they are printed, under the words
# the line uses for each asset class.
_CHANGE_PART_WORDS = (("Crypto", "crypto"), ("Retirement", "retirement"),
                      ("Taxable investments", "investments"),
                      ("Health savings", "health savings"),
                      ("Education savings", "education savings"),
                      ("Cash", "cash"), ("Card debt", "cards"),
                      ("Other", "other"))


def _signed_dollars(d: int) -> str:
    # a word joiner holds the sign to its figure: a browser may break a
    # line after "−", which left "investments −" at a line's end
    return f"{'+' if d > 0 else '−'}\u2060${abs(d):,}"


def _month_change_text(conn, by_class: dict, series: list,
                       figures: dict) -> str | None:
    """The headline's change this month by part — "crypto −$600 ·
    retirement −$300 · cash +$50" — in whole dollars adding up to it as
    printed, the parts that moved only. Each asset class is its balance
    now less what the last nightly recording of last month held for it;
    the property layer is its own tile's change. None when there is no
    change to explain, or when last month's recorded classes do not add
    up to the point the change is measured from (specs/counted-figures.md,
    "The month's change says what moved")."""
    total_ch = figures["month_change"]
    if total_ch is None or len(series) < 2:
        return None
    row = conn.execute(
        "SELECT by_class FROM networth_snapshot "
        "WHERE to_char(date,'YYYY-MM') = %s "
        "  AND jsonb_typeof(by_class) = 'object' "
        "ORDER BY date DESC LIMIT 1", (series[-2][0][:7],)).fetchone()
    if row is None:
        return None
    before = {}
    for k, v in as_dict(row["by_class"]).items():
        if k == SNAPSHOT_PROPERTY_KEY:
            continue
        v = _finite(v)
        if v is None:
            return None
        before[k] = v
    # the parts must be the parts of the two points the figure is measured
    # between, or the line is a second figure
    if (abs(sum(before.values()) - series[-2][1]) > 0.01
            or abs(sum(by_class.values()) - series[-1][1]) > 0.01):
        return None
    known = {k for k, _w in _CHANGE_PART_WORDS}
    if (set(before) | set(by_class)) - known:
        return None
    parts = [(word, by_class.get(k, 0.0) - before.get(k, 0.0))
             for k, word in _CHANGE_PART_WORDS]
    parts.append(("property", figures["property_month_change"] or 0.0))
    dollars = shown_dollars([v for _w, v in parts], total_ch)
    # a no-break space holds each part's word to its figure: a wrap
    # between them left "investments −" at a line's end
    said = [f"{word}\u00a0{_signed_dollars(d)}"
            for (word, _v), d in zip(parts, dollars) if d]
    return " · ".join(said) or None


# How long after it left a withdrawal may take to reach the bank, and how
# far ahead of it a bank may date the deposit.
_ARRIVAL_DAYS_AFTER = 7
_ARRIVAL_DAYS_BEFORE = 1


def _unarrived_withdrawals(conn, today: dt.date) -> str | None:
    """This month's withdrawals to the bank from a crypto account that no
    linked bank account has received, as the line under the month's
    change: "$250 withdrawn 03/02–03/04 has not shown up in a linked
    account". A withdrawal has arrived when a bank account holds a deposit
    of the same amount to the cent, dated from the day before it to seven
    days after; each deposit answers for one withdrawal. None when all
    arrived or nothing was withdrawn."""
    since = today.replace(day=1)
    out = []
    for r in conn.execute(
            """SELECT t.date, t.raw FROM transactions t
                 JOIN accounts a ON a.id = t.account_id
                WHERE a.subtype = 'crypto' AND t.removed = 0
                  AND t.date >= %s AND t.date <= %s
                ORDER BY t.date, t.id""", (since, today)).fetchall():
        d = as_dict(r["raw"])
        if d.get("type") != "fiat_withdrawal":
            continue
        usd = _native_usd(d)
        if usd is None:
            continue
        usd = abs(usd)
        if usd >= 0.005:
            out.append((as_date(r["date"]), int(round(usd * 100))))
    if not out:
        return None
    # deposits to any bank account the household has linked, each usable
    # once: (cents, day) -> how many
    arrived: dict[tuple, int] = {}
    for r in conn.execute(
            """SELECT t.date, t.amount FROM transactions t
                 JOIN accounts a ON a.id = t.account_id
                WHERE a.type = 'depository' AND t.removed = 0
                  AND t.amount < 0 AND t.date >= %s""",
            (since - dt.timedelta(days=_ARRIVAL_DAYS_BEFORE),)).fetchall():
        key = (int(round(-float(r["amount"]) * 100)), as_date(r["date"]))
        arrived[key] = arrived.get(key, 0) + 1
    missing = []
    for day, cents in out:
        for off in range(-_ARRIVAL_DAYS_BEFORE, _ARRIVAL_DAYS_AFTER + 1):
            key = (cents, day + dt.timedelta(days=off))
            if arrived.get(key):
                arrived[key] -= 1
                break
        else:
            missing.append((day, cents))
    if not missing:
        return None
    first, last = missing[0][0], missing[-1][0]
    when = (f"{first:%m/%d}" if first == last
            else f"{first:%m/%d}–{last:%m/%d}")
    return (f"${whole_dollars(sum(c for _d, c in missing) / 100):,} "
            f"withdrawn {when} has not shown up in a linked account")


def _account_category(typ: str | None) -> str:
    """The Accounts page's section for an account type."""
    return {"credit": "Credit Cards", "investment": "Investments",
            "loan": "Loans"}.get(typ or "", "Bank")


def _counted_text(total: float, counted: float, counted_d: int) -> str | None:
    """What of an Accounts section the headline counts, when it is not the
    section's total: a card's owed balance comes off it, a loan or an
    uncombined business account is not in it. Decided in cents (`total`,
    `counted`) before any rounding, so a fully counted section never reads
    "$1 short" because its share of the headline rounded the other way;
    `counted_d` is the whole-dollar part the page prints. None when they
    agree."""
    if abs(counted - total) < 0.005:
        return None
    if abs(counted) < 0.005:
        return "not in the total"
    return f"{'−' if counted_d < 0 else ''}${abs(counted_d):,} in the total"


def account_totals(conn) -> dict:
    """The Accounts page's figures (specs/counted-figures.md). The headline
    IS the Net Worth page's "Financial accounts" figure — compute_networth's
    `current_total`, read from it, never re-added: cash and investments
    less card debt, business accounts in exactly when the household
    combines them, linked backups and hidden accounts out. Loans are not
    in it (Net Worth carries them in the property layer); they are listed
    under it as owed. The section and institution subtotals are the page's
    lists' sums — each counted account's balance as the bank states it (a
    card's or a loan's is the amount owed) — split personal / business the
    way the page splits its panes. Each section also carries
    `counted_total`: the part of it inside the headline, signed as the
    headline signs it (card debt off, loans and accounts outside the total
    at 0), so the sections always sum to the headline whatever the page
    lists beside it."""
    from . import links
    nw = compute_networth(conn, trend=False)
    counted = _live_accounts(conn)
    loans = [r for r in counted if r["type"] == "loan"]
    fin = [r for r in counted if r["type"] != "loan"]
    shadows = links.shadow_ids(conn)
    sections: dict = {"personal": {}, "business": {}}
    counted_ids = {r["id"] for r in fin}
    # a combined household's business accounts are inside the total; its
    # business LOANS never are (no loan is), so they do not decide it
    business_counted = False
    for r in conn.execute(
            """SELECT a.id, a.item_id, a.type, a.balance_current AS bal,
                      a.entity_id
                 FROM accounts a
                WHERE a.balance_current IS NOT NULL
                  AND a.user_removed_at IS NULL
                  AND NOT (a.id = ANY(%s))""", (shadows,)).fetchall():
        pane = sections["business" if r["entity_id"] else "personal"]
        if r["entity_id"] and r["id"] in counted_ids:
            business_counted = True
        cat = pane.setdefault(_account_category(r["type"]),
                              {"total": 0.0, "counted_total": 0.0,
                               "by_item": {}, "_accounts": {}})
        cat["_accounts"].setdefault(r["item_id"], {})[r["id"]] = \
            float(r["bal"] or 0)
        if r["id"] in counted_ids:
            cat["counted_total"] += (-float(r["bal"] or 0)
                                     if r["type"] == "credit"
                                     else float(r["bal"] or 0))
        cat["total"] += float(r["bal"] or 0)
        cat["by_item"][r["item_id"]] = (cat["by_item"].get(r["item_id"], 0.0)
                                        + float(r["bal"] or 0))
    # what the page shows adds up: the sections' counted parts to the
    # headline, and each section's institution subtotals to its total
    cats = [cat for pane in sections.values() for cat in pane.values()]
    for cat, part in zip(cats, shown_parts(
            [c["counted_total"] for c in cats], nw["current_total"])):
        cat["counted_total"] = part
    for cat in cats:
        cat["total"] = round(cat["total"], 2)
        items = list(cat["by_item"])
        cat["by_item"] = dict(zip(items, shown_parts(
            [cat["by_item"][k] for k in items], cat["total"])))
    # and the same lists in the whole dollars the page prints
    for cat, part in zip(cats, shown_dollars(
            [c["counted_total"] for c in cats], nw["current_total"])):
        counted_c = cat["counted_total"]
        items = list(cat["by_item"])
        item_dollars = dict(zip(items, shown_dollars(
            [cat["by_item"][k] for k in items], cat["total"])))
        by_account = {}
        for item, accs in cat.pop("_accounts").items():
            ids = list(accs)
            by_account.update(zip(ids, shown_dollars(
                shown_parts([accs[a] for a in ids], cat["by_item"][item]),
                cat["by_item"][item], dollars=item_dollars[item])))
        total_d = whole_dollars(cat["total"])
        cat["shown"] = {"total": total_d,
                        "counted_total": part,
                        "counted_text": _counted_text(cat["total"], counted_c,
                                                       part),
                        "by_item": item_dollars,
                        "by_account": by_account}
    return {
        "label": "Financial accounts",
        "financial_total": nw["current_total"],
        "account_count": len(fin),
        "institution_count": len({(r["inst"], bool(r["inst_unnamed"]))
                                  for r in fin}),
        # loans, listed under the total as owed — not taken off it
        "loans_owed": round(sum(float(r["bal"] or 0) for r in loans), 2),
        "loan_count": len(loans),
        # true when the household combines business money with its own, so
        # the business accounts below are inside the total
        "business_counted": business_counted,
        "sections": sections,
        # logins whose accounts are all linked backups, and their words
        "backups": _backup_logins(conn, sections, shadows),
        # a backup whose balance is its serving account's, in the dollars
        # that account is shown in
        "backup_shown": _backup_dollars(conn, sections),
    }


# how a connection is named when a backup says what it stands behind
_PROVIDER_WORDS = {"plaid": "Plaid", "mx": "MX", "simplefin": "SimpleFIN",
                   "simplefin-org": "SimpleFIN"}


def _backup_logins(conn, sections: dict, shadows: list) -> dict:
    """{login: words} for a login none of whose accounts counts — every
    one a linked backup, its balances in the total through the serving
    copy. Such a login has no subtotal: the page prints "backup of Plaid —
    not counted" over its dimmed rows, never a $0 heading over full
    balances. The words name what serves: the serving account's
    connection (its aggregator, else its institution)."""
    if not shadows:
        return {}
    counted_items = {item for pane in sections.values()
                     for cat in pane.values() for item in cat["by_item"]}
    serving = _serving_accounts(conn)
    names = {r["id"]: (_PROVIDER_WORDS.get(r["aggregator"] or "")
                       or r["institution_name"] or "the serving account")
             for r in conn.execute(
                 """SELECT a.id, i.aggregator, i.institution_name
                      FROM accounts a JOIN items i ON i.id = a.item_id
                     WHERE a.id = ANY(%s)""",
                 (list(set(serving.values())),)).fetchall()}
    out: dict = {}
    for r in conn.execute(
            """SELECT a.id, a.item_id FROM accounts a
                WHERE a.user_removed_at IS NULL AND a.id = ANY(%s)
                ORDER BY a.id""", (shadows,)).fetchall():
        if r["item_id"] in counted_items or r["item_id"] in out:
            continue
        who = names.get(serving.get(r["id"], ""), "another connection")
        out[r["item_id"]] = f"backup of {who} — not counted"
    return out


def _serving_accounts(conn) -> dict[str, str]:
    """{backup account: the account serving its link group}."""
    from . import links
    serving: dict[str, str] = {}
    for g in links.groups(conn):
        primary = next((m["account_id"] for m in g["members"]
                        if m["primary"]), None)
        for m in g["members"]:
            if primary and m["account_id"] != primary:
                serving[m["account_id"]] = primary
    return serving


def _backup_dollars(conn, sections: dict) -> dict[str, int]:
    """{backup account: whole dollars} for a linked backup whose balance
    agrees to the cent with its serving account's: the dollars the serving
    account is shown in (its section's `shown.by_account`). The two rows
    are one account seen through two feeds, and the largest remainder may
    have moved the serving row a dollar off its own rounding; rounded on
    its own, the backup would then print the same balance a dollar apart
    (specs/counted-figures.md, "Lists add up in the dollars they show").
    A backup a cent or more apart, with no balance, hidden, or whose
    serving account no section counts is left out and shows its own."""
    serving = _serving_accounts(conn)
    if not serving:
        return {}
    shown = {a: d for pane in sections.values() for cat in pane.values()
             for a, d in cat["shown"]["by_account"].items()}
    cents = {r["id"]: int(round(float(r["bal"]) * 100))
             for r in conn.execute(
                 """SELECT a.id, a.balance_current AS bal FROM accounts a
                     WHERE a.balance_current IS NOT NULL
                       AND a.user_removed_at IS NULL
                       AND a.id = ANY(%s)""",
                 (sorted(set(serving) | set(serving.values())),)).fetchall()}
    return {b: shown[p] for b, p in sorted(serving.items())
            if p in shown and b in cents and cents[b] == cents.get(p)}


def _recorded_month_ends(conn) -> dict:
    """Recorded financial month-ends, {YYYY-MM: total}: imported month-end
    reports first, then the last nightly snapshot of each month."""
    recorded = {r["month"]: r["total"] for r in conn.execute(
        "SELECT month, total FROM networth_recorded")}
    for r in conn.execute(
            "SELECT DISTINCT ON (to_char(date,'YYYY-MM')) "
            "       to_char(date,'YYYY-MM') AS m, total "
            "FROM networth_snapshot ORDER BY to_char(date,'YYYY-MM'), date DESC"):
        recorded.setdefault(r["m"], r["total"])
    return recorded


def _held_with_cash(conn, months: list, prop: list) -> list:
    """The property layer, held wherever the financial trend holds cash.

    `_reconstruct_trend` rebuilds every month after the last recorded
    month-end from that record's cash (from today's when nothing is
    recorded): a mortgage payment's cash never leaves those points. The
    loan walked back through its rows would still carry the payment's
    principal, so the two layers added together read the principal as a
    gain with no outflow beside it — a payment month that lost money
    shows a rise. Holding the loan at the record (or at today) keeps both
    layers blind to the same payment; the months up to the last record
    keep the walk, because there the cash that paid it is in the figure.
    The current month is today's figure in both layers and is untouched."""
    if len(prop) < 2:
        return prop
    recorded = _recorded_month_ends(conn)
    anchored = [i for i, m in enumerate(months[:-1]) if m in recorded]
    if not anchored:
        return [prop[-1]] * len(prop)
    last = anchored[-1]
    return (prop[:last + 1] + [prop[last]] * (len(prop) - 2 - last)
            + [prop[-1]])


def _trend_estimated_until(conn, months: list) -> int:
    """Index of the boundary between reconstructed (estimated) and recorded
    months. Recorded = the earlier of the first `networth_recorded` month
    (balance report) and the first nightly `networth_snapshot`."""
    firsts = []
    row = conn.execute("SELECT MIN(date) AS d FROM networth_snapshot").fetchone()
    if row and row["d"]:
        firsts.append(_iso(row["d"])[:7])
    # exclude rows an import marked 'historical…': reconstructed from old
    # statements or a register (honestly "estimated"), not the live recorded
    # era, so the solid/recorded boundary stays at the snapshot region.
    row = conn.execute("SELECT MIN(month) AS m FROM networth_recorded "
                       "WHERE COALESCE(source,'') NOT LIKE 'historical%'").fetchone()
    if row and row["m"]:
        firsts.append(row["m"])
    if not firsts or not months:
        return len(months) - 1
    fm = min(firsts)
    for i, m in enumerate(months):
        if m >= fm:
            return i
    return len(months) - 1


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _security(raw):
    """Normalize an investment transaction's raw JSON to (ticker, share_delta,
    price) across all sources (QFX/Plaid brokerages, workplace-plan feeds,
    robo-advisors, Coinbase). Returns None for non-investment rows."""
    d = as_dict(raw)
    if "native_amount" in d and isinstance(d.get("amount"), dict):        # coinbase
        qty = _f(d["amount"].get("amount")); nat = _native_usd(d)
        return (d["amount"]["currency"], qty or 0.0,
                (abs(nat / qty) if qty and nat is not None else None))
    if "shares" in d:                                                     # plan feeds, robo-advisors
        return (d.get("symbol") or d.get("fund"), _f(d["shares"]) or 0.0, _f(d.get("price")))
    if "units" in d:                                                      # QFX brokerages
        return (d.get("ticker") or d.get("cusip"), _f(d["units"]) or 0.0, _f(d.get("unitprice")))
    return None


# Large moves of money BETWEEN the user's own accounts (checking↔brokerage,
# brokerage→brokerage) are NOT net-worth changes — they're transfers. We net
# them out so a round-trip doesn't read as a bump.
_TRANSFER_MIN = 10_000


def _is_transfer(raw) -> bool:
    d = as_dict(raw)
    if "native_amount" in d:
        return str(d.get("type")) in ("pro_withdrawal", "pro_deposit", "exchange_deposit",
                                      "exchange_withdrawal", "fiat_deposit", "fiat_withdrawal")
    t = str(d.get("type") or "").lower()
    return any(k in t for k in ("invbanktran", "acat", "withdraw", "deposit from",
                                "transfer", "in-kind"))


def _transfer_dir(raw, amount: float) -> float:
    """Signed cash flow INTO the investment world (+ in, − out). Direction is read
    from the transaction TYPE, not the stored amount sign — importers disagree on
    sign (one robo-advisor stores deposits opposite to a QFX brokerage), so the type name is
    the only reliable cue."""
    t = str(as_dict(raw).get("type") or "").lower()
    mag = abs(amount)
    if any(k in t for k in ("withdraw", "liquidation", "redemption", "send")):
        return -mag
    if any(k in t for k in ("deposit", "contribution", "purchase")):
        return +mag
    return -amount            # invbanktran/transfer: negative amount = money in


def _slim_raw(r: dict) -> dict:
    """The raw document as _security / _is_transfer / _transfer_dir read
    it, rebuilt from the columns the trend query projects."""
    if r["is_cb"]:                                                   # coinbase
        return {"native_amount": {"amount": r["cb_native"]},
                "amount": {"amount": r["cb_qty"], "currency": r["cb_cur"]},
                "type": r["r_type"]}
    d: dict = {"type": r["r_type"]}
    if r["has_shares"]:                            # plan feeds, robo-advisors
        d.update(shares=r["shares"], symbol=r["symbol"], price=r["price"])
    if r["has_units"]:                                        # QFX brokerages
        d.update(units=r["units"], ticker=r["ticker"], unitprice=r["unitprice"])
    return d


def _reconstruct_trend(conn, today_total, today: dt.date | None = None):
    """Net worth over time, reconstructed from HOLDINGS, not cash flow.

    Anchors every account at its exact current value and walks backward:
      • Investments — each position's share count is walked back from today's
        holdings through the share-level transaction history (buys/sells/reinvests),
        then valued at that date's price (interpolated from every price the ledger
        observed for that security, plus today's live price). This captures market
        growth AND drawdowns. Clamped to each account's inception (it held
        nothing before it existed).
      • Cash & cards — a constant baseline at today's exact signed balance
        (pre-history unrecoverable; investment transfers handled by the offset).
      • The part of an investment balance its holdings do not explain is
        carried as a constant from the account's inception, like cash.

    Transfers between the user's own accounts (large deposits/withdrawals/ACATs)
    are netted out via `_transfer_dir` so a round-trip doesn't read as a
    net-worth bump — only market moves and genuine external flows change the
    line. Residual month spikes from transfer timing are smoothed.

    Monthly, exact today. It is a reconstruction, not a recorded balance
    history — months with short investment history are underdetermined;
    recorded month-ends and the nightly snapshot pin what they cover."""
    today_d = today or dt.date.today()
    from . import links
    shadows = set(links.shadow_ids(conn))   # exclude dual-source duplicates
    # the SAME entity filter _live_accounts applies to the current
    # total. Without it the historical cash baseline carries business
    # balances that today's anchor excludes, so the line sits high for every
    # past month and then steps to the real number at today — a fabricated
    # move the exact size of the entity's balances.
    accts = list(conn.execute(
        "SELECT id, type, subtype, balance_current AS bal FROM accounts"
        " WHERE (" + budget.COMBINE_OR + "entity_id IS NULL)"))
    # exclude linked shadow investment accounts (same set _live_accounts drops)
    # — a Plaid+MX brokerage link would otherwise double-count holdings + the
    # share-delta walk across the whole reconstructed trend
    inv_ids = {a["id"] for a in accts
               if (a["type"] == "investment" or a["subtype"] == "crypto")
               and a["id"] not in shadows}
    if not inv_ids:
        return [[today_d.strftime("%Y-%m"), today_total]]

    # current holdings anchor: aid -> {ticker: [shares_now, price_now]}
    hold_now: dict = {}
    for h in conn.execute("SELECT account_id, symbol, quantity, price FROM holdings"):
        hold_now.setdefault(h["account_id"], {})[h["symbol"]] = [h["quantity"] or 0.0, h["price"] or 0.0]
    for h in conn.execute("SELECT account_id, currency, quantity, native_usd FROM crypto_holdings"):
        q = h["quantity"] or 0.0
        # a position with no USD value reads as $0, as the price above does
        hold_now.setdefault(h["account_id"], {})[h["currency"]] = [q, ((h["native_usd"] or 0.0) / q if q else 0.0)]

    # per-ticker price curve from every observation in the ledger + today's price.
    # One fetch of the investment rows feeds everything below that needs them
    # — share deltas, price observations, transfer events and each account's
    # inception — so the ledger is read (and its raw JSON decoded) once, not
    # once per purpose.
    today = today_d.isoformat()
    obs: dict = {}
    inv_deltas: dict = {}     # (aid, ticker) -> [(date, share_delta)]
    events = []               # large own-money transfers: (date, signed flow)
    inception: dict = {}      # aid -> first ledger date
    # the raw document is read for a dozen keys (_security, _is_transfer,
    # _transfer_dir); a brokerage row's full JSON is many times that, and
    # decoding megabytes of it in Python was most of this walk's time.
    # The keys come out as columns and are folded back into the small
    # dict the readers expect — a present-but-null key stays present, as
    # their `in` tests require.
    # text columns only — a jsonb column is decoded per row in Python,
    # and the coinbase amounts came back that way for every brokerage row
    # too. NULLIF folds the empty strings a plan feed writes so the
    # fallbacks (symbol → fund, ticker → cusip) resolve in SQL.
    for r in conn.execute(
            """SELECT account_id, date, amount,
                      raw->>'type' AS r_type,
                      raw ? 'native_amount' AS is_cb,
                      raw->'amount'->>'amount' AS cb_qty,
                      raw->'amount'->>'currency' AS cb_cur,
                      raw->'native_amount'->>'amount' AS cb_native,
                      raw ? 'shares' AS has_shares, raw->>'shares' AS shares,
                      COALESCE(NULLIF(raw->>'symbol',''), NULLIF(raw->>'fund',''))
                          AS symbol,
                      raw->>'price' AS price,
                      raw ? 'units' AS has_units, raw->>'units' AS units,
                      COALESCE(NULLIF(raw->>'ticker',''), NULLIF(raw->>'cusip',''))
                          AS ticker,
                      raw->>'unitprice' AS unitprice
                 FROM transactions
                WHERE removed=0 AND account_id = ANY(%s)""",
            (list(inv_ids),)):
        r = dict(r)
        r["raw"] = _slim_raw(r)
        aid = r["account_id"]
        d_iso = _iso(r["date"])
        if d_iso < inception.get(aid, "9999"):
            inception[aid] = d_iso
        amt = r["amount"] or 0.0
        if abs(amt) >= _TRANSFER_MIN and _is_transfer(r["raw"]):
            events.append((d_iso, _transfer_dir(r["raw"], amt)))
        s = _security(r["raw"])
        if not s or not s[0]:
            continue
        inv_deltas.setdefault((aid, s[0]), []).append((d_iso, s[1]))
        if s[2] and s[2] > 0:
            obs.setdefault(s[0], []).append((d_iso, s[2]))
    for posn in hold_now.values():
        for tk, (sh, pr) in posn.items():
            if pr and pr > 0:
                obs.setdefault(tk, []).append((today, pr))
    curve = {tk: sorted(set(o)) for tk, o in obs.items()}
    curve_dates = {tk: [d for d, _ in c] for tk, c in curve.items()}
    # shares held after a date = today's shares minus every delta dated after
    # it. Sorted deltas with a suffix-sum table answer that with one bisect
    # instead of re-summing the account's whole history for each of the
    # ~300 months walked.
    delta_dates: dict = {}
    delta_after: dict = {}    # (aid, tk) -> suffix sums aligned to delta_dates
    for key, dl in inv_deltas.items():
        dl.sort(key=lambda x: x[0])
        delta_dates[key] = [d for d, _ in dl]
        acc = [0.0] * (len(dl) + 1)
        for i in range(len(dl) - 1, -1, -1):
            acc[i] = acc[i + 1] + dl[i][1]
        delta_after[key] = acc

    def price(tk, date):
        c = curve.get(tk)
        if not c:
            return None
        i = bisect.bisect_right(curve_dates[tk], date)
        if i == 0:
            return c[0][1]
        if i >= len(c):
            return c[-1][1]
        (d0, p0), (d1, p1) = c[i - 1], c[i]
        span = (dt.date.fromisoformat(d1) - dt.date.fromisoformat(d0)).days or 1
        frac = (dt.date.fromisoformat(date) - dt.date.fromisoformat(d0)).days / span
        return p0 + (p1 - p0) * frac

    # cash/cards: a small constant baseline (today's exact signed balance). Its
    # early history is unrecoverable and swamped by the investment layer, so we
    # don't try to walk it — investment transfers are handled by the offset below.
    signed_now = {a["id"]: ((-(a["bal"] or 0.0)) if a["type"] == "credit" else (a["bal"] or 0.0))
                  for a in accts}
    # loans (mortgage/auto) live in the PROPERTY layer, not the financial
    # total — compute_networth skips them, so the trend must too, or a
    # linked mortgage would inflate the whole line by its balance
    loan_ids = {a["id"] for a in accts if a["type"] == "loan"}
    # Linked shadow accounts are excluded like every other
    # money aggregate — a dual-sourced checking/card pair would double the
    # baseline, and a shadow INVESTMENT account (excluded from inv_ids
    # above) would otherwise fall through into the cash sum
    cash_baseline = sum(v for aid, v in signed_now.items()
                        if aid not in inv_ids and aid not in loan_ids
                        and aid not in shadows)

    # transfer offset: cumulative external cash that flowed into the investment
    # world AFTER date t. Adding it makes contributions/withdrawals net-worth-
    # neutral (the money was in cash before it was invested).
    events.sort()
    ev_dates = [d for d, _ in events]
    ev_flows = [f for _, f in events]

    def offset(date):
        i = bisect.bisect_right(ev_dates, date)
        return sum(ev_flows[i:])

    acct_tickers: dict = {}
    for (aid, tk) in inv_deltas:
        acct_tickers.setdefault(aid, set()).add(tk)
    for aid, posn in hold_now.items():
        for tk in posn:
            acct_tickers.setdefault(aid, set()).add(tk)

    # The part of each investment balance the listed holdings do not
    # explain (a cash sweep, an unpriced fund, a plan reporting only a
    # total). It belongs to every month the account existed; carried as a
    # constant like cash, or the line would sit at cash + holdings for the
    # whole history and then jump by the residual in the pinned last month.
    residual = {}
    for a in accts:
        if a["id"] in inv_ids and a["bal"] is not None:
            held = sum(sh * pr for sh, pr in hold_now.get(a["id"], {}).values())
            residual[a["id"]] = signed_now[a["id"]] - held

    def nw(date):
        total = cash_baseline + offset(date)
        for aid in inv_ids:
            if inception.get(aid, "9999") > date:       # account didn't exist yet
                continue
            total += residual.get(aid, 0.0)
            posn = hold_now.get(aid, {})
            for tk in acct_tickers.get(aid, ()):
                dd = delta_dates.get((aid, tk))
                after = (delta_after[(aid, tk)][bisect.bisect_right(dd, date)]
                         if dd else 0.0)
                sh = posn.get(tk, [0.0, 0.0])[0] - after
                if abs(sh) < 1e-6:
                    continue
                p = price(tk, date)
                if p is not None:
                    total += sh * p
        return total

    # Investment accounts can exist (holdings) with zero ledger rows yet —
    # a brokerage linked before its transactions sync, or a holdings-only
    # fill. min() of an empty inception set would raise and 500
    # /api/reports/networth, so fall back to a single "today" point like
    # the no-inv case.
    starts = [inception[a] for a in inv_ids if a in inception]
    if not starts:
        return [[today_d.strftime("%Y-%m"), today_total]]
    start = min(starts)[:7]
    sy, sm = int(start[:4]), int(start[5:7])
    ey, em = today_d.year, today_d.month
    months = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        last = (dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1)).isoformat()
        months.append(min(last, today))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    vals = [nw(d) for d in months]
    # smooth residual month spikes from transfer timing (money briefly in-flight
    # between our own accounts). Iterated; replaces any point far from its
    # neighbours' midpoint. Real market moves are gradual and survive this.
    for _ in range(4):
        for i in range(1, len(vals) - 1):
            mid = (vals[i - 1] + vals[i + 1]) / 2
            if abs(mid) > 1 and abs(vals[i] - mid) > 0.30 * abs(mid):
                vals[i] = mid

    # ---- recorded overlay ------------------------------------------------
    # RECORDED month-end balances beat the reconstruction wherever they exist:
    # (a) `networth_recorded` — imported month-end balance reports (an
    #     import may also write reconstructed rows marked 'historical…');
    # (b) nightly `networth_snapshot`s, collapsed to month-end.
    # Recorded catches real cash moves the frozen-cash reconstruction cannot
    # see, so recorded wins.
    recorded = _recorded_month_ends(conn)
    ym = [d[:7] for d in months]
    # Recorded month-end balances pin the reconstruction. There can be MORE than
    # one recorded region (e.g. an older historical block reconstructed from
    # statements + register, PLUS a recent snapshot block). Correct the
    # reconstruction's DRIFT piecewise-linearly so its shape is preserved but it
    # passes through every recorded anchor: ramp toward the first anchor before
    # it, interpolate the delta between consecutive anchors, hold the last delta
    # after. With a single recent region this reduces to a single ramp.
    rec_idx = [i for i, m in enumerate(ym) if m in recorded]
    if rec_idx:
        deltas = {i: recorded[ym[i]] - vals[i] for i in rec_idx}
        first, last = rec_idx[0], rec_idx[-1]
        for i in range(first):                                  # before first anchor
            vals[i] += deltas[first] * (i / first if first else 0)
        for a, b in zip(rec_idx, rec_idx[1:]):                  # between anchors
            da, db = deltas[a], deltas[b]
            for i in range(a + 1, b):
                vals[i] += da + (db - da) * ((i - a) / (b - a))
        for i in range(last + 1, len(vals)):                    # after last anchor
            vals[i] += deltas[last]
        for i in rec_idx:                                       # anchors land exact
            vals[i] = recorded[ym[i]]
    if vals:
        vals[-1] = today_total                          # pin the last point exact
    return [[d[:7], round(v, 2)] for d, v in zip(months, vals)]


def _pl_scope(conn) -> tuple[str, list]:
    """The accounts an investment P/L panel may read, as an AND fragment on
    alias `a` with its params: the ones net worth counts (_live_accounts).
    A linked twin is one real account reached through two feeds, and a
    business account stays out of the personal figures unless the combined
    view is on; a panel that read every account would show a linked
    brokerage at twice the value the headline counts for it."""
    from . import links
    return ("\n                 AND (" + budget.COMBINE_OR + "a.entity_id IS NULL)"
            "\n                 AND NOT (a.id = ANY(%s))", [links.shadow_ids(conn)])


def _retirement_pl(conn) -> list[dict]:
    """Workplace-plan P/L: contributions (the clean cost basis, straight
    off the payroll 'Contribution' rows) vs current value, per plan + a combined
    Total. Retirement accounts funded by rollovers or in-kind transfers are
    excluded — the transaction feed never tags those as deposits, so no honest
    cost basis exists for them."""
    out = []
    tot_c = tot_v = 0.0
    scope, sp = _pl_scope(conn)
    for a in conn.execute(
            """SELECT a.id, COALESCE(a.display_name, a.name) nm, a.balance_current v,
                      COALESCE(i.institution_name, 'Workplace plan') inst
               FROM accounts a JOIN items i ON i.id=a.item_id
               WHERE i.aggregator='plan_csv' AND a.balance_current IS NOT NULL"""
            + scope + """
               ORDER BY a.balance_current DESC, a.id""", sp).fetchall():
        c = conn.execute(
            """SELECT COALESCE(SUM((raw ->> 'amount')::float8),0) c
               FROM transactions WHERE account_id=%s AND removed=0
                 AND raw ->> 'type' = 'Contribution'""",
            (a["id"],)).fetchone()["c"]
        val = a["v"] or 0.0
        out.append({"name": a["nm"], "institution": a["inst"],
                    "contributed": round(c, 2),
                    "value": round(val, 2), "pl": round(val - c, 2),
                    "pct": round(100 * (val - c) / c, 1) if c else None})
        tot_c += c; tot_v += val
    if len(out) > 1:
        out.append({"name": "Total", "contributed": round(tot_c, 2),
                    "value": round(tot_v, 2), "pl": round(tot_v - tot_c, 2),
                    "pct": round(100 * (tot_v - tot_c) / tot_c, 1) if tot_c else None})
    return out


# Feeds whose accounts carry a P/L of their own — Coinbase's fiat-anchored
# one, a workplace plan's contributions — are left out of the holdings P/L.
_OWN_PL_AGGREGATORS = ("coinbase", "plan_csv")


def _household_today(conn) -> dt.date:
    from .. import localtime
    return localtime.now_local(budget.load_config(conn)).date()


def _brokerage_pl(conn) -> list[dict]:
    """Per-account gain vs money-in for brokerage accounts that report
    holdings, with the money-in source chosen per account:

    - Where the aggregator's per-holding COST BASIS is trustworthy (lots
      bought with cash at real prices), basis = Σcost_basis.
    - Where it is not (lots transferred in-kind arrive with a near-zero
      basis), a manual anchor takes over: config `investment_basis_override`
      keyed by account mask — the dollars actually put in, as the
      brokerage's own dashboard states them. Value stays live, so the return
      tracks going forward; only the deposit anchor is manual.

    A holding with no cost_basis is cash (CUR:USD, gain 0) or an untracked lot;
    those contribute value to BOTH sides (0 gain). Each row names its
    institution so the combined view can group by it.

    The gain is taken at ONE moment: the holdings' value against their
    basis. Whatever the balance holds beyond the holdings — deposits since
    the snapshot, cash the feed does not list as a position — is money in,
    at no gain, like a holding with no basis. `pl_as_of` dates the gain
    when the snapshot is older than the balance (specs/honest-figures.md)."""
    # Masks are stored upper-case by the settings door and a feed may report
    # letters in either case, so the lookup ignores case on both sides.
    override = {str(k).upper(): v for k, v in
                (budget.load_config(conn).get("investment_basis_override")
                 or {}).items()}
    scope, sp = _pl_scope(conn)
    # One entry per ACCOUNT: two accounts can share an institution and a
    # display name (two spouses' "Roth IRA"), and merged they would read as
    # one account whose manual basis override replaced both bases.
    # "value now" is the account's balance — the figure every total
    # counts — not the sum of its holdings, which the feed refreshes on its
    # own clock and can lag the balance by months; the basis is still the
    # holdings' (specs/honest-figures.md)
    per: dict = {}
    for h in conn.execute(
            """SELECT a.id, COALESCE(a.display_name, a.name) nm, a.mask,
                      a.balance_current AS bal, h.value, h.raw, h.as_of,
                      COALESCE(i.institution_name, 'Brokerage') inst
               FROM holdings h JOIN accounts a ON a.id=h.account_id
               JOIN items i ON i.id=a.item_id
               WHERE COALESCE(i.aggregator, '') <> ALL(%s)""" + scope + """
               ORDER BY a.id""",
            [list(_OWN_PL_AGGREGATORS)] + sp):
        d = per.setdefault(h["id"], {"name": h["nm"], "inst": h["inst"],
                                     "mask": h["mask"], "v": 0.0, "c": 0.0,
                                     "bal": h["bal"], "as_of": None})
        val = h["value"] or 0.0
        d["v"] += val
        day = h["as_of"].date() if hasattr(h["as_of"], "date") else None
        if day is not None and (d["as_of"] is None or day < d["as_of"]):
            d["as_of"] = day
        try:
            cb = as_dict(h["raw"]).get("cost_basis")
        except (TypeError, ValueError):
            cb = None
        d["c"] += cb if cb is not None else val    # no basis → basis=value (0 gain)
    from ..web.data import HOLDINGS_FRESH_DAYS
    today = _household_today(conn)
    out = []
    tot_v = tot_c = 0.0
    for d in sorted(per.values(), key=lambda x: (-x["v"], x["name"] or "")):
        v, c, mask = d["v"], d["c"], d["mask"]
        as_of = None
        if d["bal"] is not None:
            v = float(d["bal"])
            c += v - d["v"]                         # beyond the holdings: money in
            if d["as_of"] and (today - d["as_of"]).days > HOLDINGS_FRESH_DAYS:
                as_of = d["as_of"].isoformat()
        if v < 1:                                  # skip drained/closed lineages
            continue
        if mask and str(mask).upper() in override:  # manual dashboard basis
            c = float(override[str(mask).upper()])
            as_of = None                           # the anchor is today's figure
        out.append({"name": d["name"], "institution": d["inst"],
                    "basis": round(c, 2),
                    "value": round(v, 2), "pl": round(v - c, 2),
                    "pct": round(100 * (v - c) / c, 1) if c else None,
                    "pl_as_of": as_of})
        tot_v += v; tot_c += c
    if len(out) > 1:
        out.append({"name": "Total", "basis": round(tot_c, 2), "value": round(tot_v, 2),
                    "pl": round(tot_v - tot_c, 2),
                    "pct": round(100 * (tot_v - tot_c) / tot_c, 1) if tot_c else None})
    return out


# The row types that move dollars. pro_/exchange_ transfers are the
# holder's own money moving between venues — counting them as flows buries
# the gains made there inside the cost basis.
_CRYPTO_FIAT_TYPES = {"buy", "sell", "fiat_deposit", "fiat_withdrawal"}


def _native_usd(raw: dict) -> float | None:
    """A Coinbase record's dollars, positive into the account, read the one
    way the push door reads them (coinbase_push.ledger_amount): None for a
    native amount that is not a plain, bounded decimal, so a stored raw
    that is not one is skipped rather than reaching float()."""
    from ..sync.coinbase_push import ledger_amount
    v = ledger_amount(raw)
    return None if v is None else -v


def _bank_cash_flows(raws) -> tuple[float, float]:
    """(cash in, cash out) of one crypto account: the dollars that crossed
    to or from the bank, each counted once.

    A buy or a sell settled through the account's cash wallet is written
    as two rows of one trade (the coin leg and the dollar leg, sharing the
    trade's id), with the bank side carried by a deposit or a withdrawal
    of its own. The two legs are one movement inside the account and count
    nothing, even when a fee taken between them leaves them unequal: the
    fee is already gone from the account's value. A trade written as one
    row went straight to or from the bank and counts whole.
    specs/honest-figures.md, "Crypto cash in and out"."""
    cash_in = cash_out = 0.0
    trades: dict[tuple, list[float]] = {}
    for raw in raws:
        d = as_dict(raw)
        kind = d.get("type")
        if kind not in _CRYPTO_FIAT_TYPES:
            continue
        usd = _native_usd(d)
        if usd is None:
            continue
        detail = d.get(kind)
        trade = detail.get("id") if isinstance(detail, dict) else None
        if trade:
            trades.setdefault((kind, trade), []).append(usd)
        elif usd > 0:
            cash_in += usd
        else:
            cash_out += -usd
    for legs in trades.values():
        # one row, or rows all on one side, is bank money row by row; two
        # opposite legs are the trade seen from both of its wallets
        if min(legs) < 0 < max(legs):
            continue
        for usd in legs:
            if usd > 0:
                cash_in += usd
            else:
                cash_out += -usd
    return cash_in, cash_out


def _coinbase_pl(conn) -> list[dict]:
    """Per-account Coinbase profit/loss, external-cash anchored.

    Cost basis = the bank-side flows only (_bank_cash_flows). External
    crypto received via send/tx (unknown basis) counts as gain."""
    out = []
    tot_in = tot_out = tot_val = 0.0
    scope, sp = _pl_scope(conn)
    for acct in conn.execute(
            """SELECT a.id, COALESCE(a.display_name, a.name) AS name,
                      a.balance_current AS bal FROM accounts a
               WHERE a.subtype='crypto' AND a.balance_current IS NOT NULL"""
            + scope + """
               ORDER BY a.balance_current DESC, a.id""", sp).fetchall():
        cash_in, cash_out = _bank_cash_flows(
            r["raw"] for r in conn.execute(
                "SELECT raw FROM transactions WHERE account_id=%s AND removed=0",
                (acct["id"],)))
        held = conn.execute(
            "SELECT COALESCE(SUM(native_usd),0) AS v FROM crypto_holdings WHERE account_id=%s",
            (acct["id"],)).fetchone()["v"] or 0.0
        if not cash_in and not cash_out and not held:
            # not a Coinbase-shaped account (no fiat flows, no
            # crypto_holdings) — this P/L method doesn't apply, and an
            # all-$0 row only reads as breakage
            continue
        # "value now" is the balance the totals count, not the holdings'
        # last snapshot (which can be months older than the balance)
        val = float(acct["bal"])
        pl = val + cash_out - cash_in
        out.append({"name": acct["name"], "cash_in": round(cash_in, 2),
                    "cash_out": round(cash_out, 2), "value": round(val, 2),
                    "pl": round(pl, 2),
                    "pct": round(100 * pl / cash_in, 1) if cash_in else None})
        tot_in += cash_in; tot_out += cash_out; tot_val += val
    if len(out) > 1:
        tpl = tot_val + tot_out - tot_in
        out.append({"name": "Combined", "cash_in": round(tot_in, 2),
                    "cash_out": round(tot_out, 2), "value": round(tot_val, 2),
                    "pl": round(tpl, 2),
                    "pct": round(100 * tpl / tot_in, 1) if tot_in else None})
    return out


def _retirement_combined(conn) -> dict:
    """Long-term investment accounts grouped by institution — brokerages
    (cost basis / dashboard-anchored) and workplace plans (contributions) —
    each account with money-in, value now, P/L and return; per-institution
    subtotals and a blended grand Total. 'Invested' unifies the two basis
    concepts (both are 'money put in' against current value)."""
    by_inst: dict[str, list[dict]] = {}
    dated: list[str] = []
    for c in _brokerage_pl(conn):
        if c["name"] != "Total":
            if c.get("pl_as_of"):
                dated.append(f"{c['name']}: gain counted to its holdings of "
                             f"{dt.date.fromisoformat(c['pl_as_of']):%m/%d/%y}")
            by_inst.setdefault(c["institution"], []).append(
                {"name": c["name"], "invested": c["basis"], "value": c["value"],
                 "pl": c["pl"], "pct": c["pct"]})
    for c in _retirement_pl(conn):
        if c["name"] != "Total":
            by_inst.setdefault(c["institution"], []).append(
                {"name": c["name"], "invested": c["contributed"],
                 "value": c["value"], "pl": c["pl"], "pct": c["pct"]})

    def _grp(name, rows):
        rows = sorted(rows, key=lambda r: -r["value"])
        inv = sum(r["invested"] for r in rows)
        val = sum(r["value"] for r in rows)
        return {"provider": name, "accounts": rows, "invested": round(inv, 2),
                "value": round(val, 2), "pl": round(val - inv, 2),
                "pct": round(100 * (val - inv) / inv, 1) if inv else None}

    groups = [_grp(n, r) for n, r in by_inst.items() if r]
    if not groups:
        return {}
    groups.sort(key=lambda g: -g["value"])
    ti = sum(g["invested"] for g in groups)
    tv = sum(g["value"] for g in groups)
    return {"groups": groups,
            "total": {"invested": round(ti, 2), "value": round(tv, 2),
                      "pl": round(tv - ti, 2),
                      "pct": round(100 * (tv - ti) / ti, 1) if ti else None},
            "pl_note": ("; ".join(dated) + "; what the balance moved since "
                        "counts as money in." if dated else None)}


def snapshot_networth(conn):
    """Record today's exact net worth + asset-class split. Called nightly by
    the snapshot job so a REAL net-worth trend accrues going forward."""
    from .. import localtime
    # The row's date is the HOUSEHOLD's day, like every other day the app
    # keys on: the nightly job runs on the instance clock (UTC hosted), so
    # a snapshot taken at 01:00 UTC would land on tomorrow's date for a US
    # household and the trend would show a point it has not lived.
    today = localtime.now_local(budget.load_config(conn)).date()
    rows = _live_accounts(conn)
    total = 0.0; by_class = {}
    for r in rows:
        # Match compute_networth: loans (mortgages) live in the property
        # layer, not as positive financial assets.
        if r["type"] == "loan":
            continue
        signed = (-r["bal"]) if r["type"] == "credit" else r["bal"]
        total += signed
        cls = asset_class(r["type"], r["subtype"])
        by_class[cls] = by_class.get(cls, 0) + signed
    # the property layer's net rides along under its own key (not an asset
    # class of the financial total, which `total` alone is), so the full
    # net-worth line can be measured month to month from recorded values
    try:
        by_class[SNAPSHOT_PROPERTY_KEY] = round(sum(
            p["value"] for p in _property_items(
                conn, [r for r in rows if r["type"] == "loan"])), 2)
    except Exception:                                      # noqa: BLE001
        log.exception("snapshot: property layer unavailable")
    with conn.transaction():
        conn.execute(
            """INSERT INTO networth_snapshot (date, total, by_class) VALUES (%s,%s,%s)
               ON CONFLICT (tenant_id, date) DO UPDATE
               SET total=EXCLUDED.total, by_class=EXCLUDED.by_class""",
            (today, round(total, 2), jsonb(by_class)))


# by_class key under which the nightly snapshot records the property net
SNAPSHOT_PROPERTY_KEY = "property_net"


def _finite(x):
    """A finite float, or None: a restored snapshot can carry a string,
    a boolean or a number too wide for a double where a figure belongs."""
    if isinstance(x, bool):
        return None
    try:
        f = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _counts_as_principal(row) -> bool:
    """A loan row the servicer itself labels as principal."""
    return any("PRINCIPAL" in (row[k] or "").upper()
               for k in ("category_override", "category_detailed", "name"))


def _charges_interest(row) -> bool:
    """A loan row that is the lender's interest charge."""
    return any("INTEREST" in (row[k] or "").upper()
               for k in ("category_override", "category_detailed", "name"))


# months with a charge on the loan before its ledger reads as the lender's
# whole statement even where no row is named as interest
_STATEMENT_CHARGE_MONTHS = 3


def _loan_movements(conn, loan) -> list | None:
    """(date, amount) rows that moved one loan's balance, signed as the
    ledger stores them (positive = more owed), or None when the ledger
    holds payments whose principal share is unknown.

    A loan whose ledger carries the lender's interest as well as payments
    is a full statement: its rows sum to the balance change. A charge that
    is not interest (a late fee) does not make one — it says nothing about
    how the payments split — so the ledger must show interest by name or a
    charge recurring month after month. A ledger of payments alone does
    not say how much of each payment was principal, and crediting the
    whole payment to the balance makes a mortgage month read as costing
    nothing. So there a payment counts when the servicer labels it
    principal; otherwise the liabilities record's principal paid this
    year, as a share of the payments posted this year, splits each
    payment — only when the ledger reaches back past the start of that
    year and the share is a share at all (more principal than payments
    means payments are missing). Other charges raise the balance as they
    stand. With no split known the answer is None: holding the loan flat
    would count the whole payment as lost, which is as much a guess."""
    rows = [(_iso(r["date"]), r["amount"] or 0.0, r) for r in conn.execute(
        "SELECT date, amount, name, category_detailed, category_override "
        "FROM transactions WHERE removed=0 AND account_id = %s",
        (loan["id"],))]
    charges = [(d, a, r) for d, a, r in rows if a > 0]
    if (any(_charges_interest(r) for _, _, r in charges)
            or len({d[:7] for d, _, _ in charges})
            >= _STATEMENT_CHARGE_MONTHS):
        return [(d, a) for d, a, _ in rows]
    fees = [(d, a) for d, a, _ in charges]
    payments = [(d, a, r) for d, a, r in rows if a <= 0]
    if not any(a for _, a, _ in payments):
        return fees
    labelled = [(d, a) for d, a, r in payments if _counts_as_principal(r)]
    if labelled:
        return labelled + fees
    share = None
    lia = conn.execute(
        "SELECT as_of, raw FROM liabilities WHERE account_id = %s",
        (loan["id"],)).fetchone()
    if lia:
        raw = as_dict(lia["raw"])
        ytd = _finite(raw.get("ytd_principal_paid"))
        as_of = _iso(lia["as_of"])[:10] if lia["as_of"] else None
        # the year's payments are all in the ledger only when it begins
        # before the year did
        whole_year = min(d for d, _, _ in rows) < f"{as_of[:4]}-01-01" \
            if as_of else False
        if ytd is not None and ytd >= 0 and as_of and whole_year:
            paid = -sum(a for d, a, _ in payments
                        if d[:4] == as_of[:4] and d <= as_of)
            if paid > 0 and ytd <= paid:
                share = ytd / paid
    if share is None:
        return None
    return [(d, a * share) for d, a, _ in payments] + fees


def _property_trend(conn, months: list, now_net: float,
                    live_loans) -> list:
    """The property layer's net at each month in `months` (YYYY-MM),
    aligned with them, ending at `now_net`.

    A month the nightly snapshot recorded a property net for uses the last
    recording in that month. Any other month walks the live loan balances
    back through what reduced or raised them after the month's end
    (_loan_movements: a principal payment lowers the balance, so before it
    the debt was larger); the manual entries have no history and hold
    their value. A month with a payment after it whose principal is
    unknown is None: any figure for it is a guess. The current month is
    today's figure exactly."""
    if not months:
        return []
    recorded = {}
    # the value is read as JSON text and checked here: a restored archive
    # can hold a string, a boolean or 1e999 under the key, and a cast in
    # SQL would fail the whole page on it
    for r in conn.execute(
            "SELECT DISTINCT ON (to_char(date,'YYYY-MM')) "
            "       to_char(date,'YYYY-MM') AS m, "
            "       by_class->>%s AS p "
            "FROM networth_snapshot "
            "WHERE jsonb_typeof(by_class) = 'object' "
            "  AND jsonb_typeof(by_class->%s) = 'number' "
            "ORDER BY to_char(date,'YYYY-MM'), date DESC",
            (SNAPSHOT_PROPERTY_KEY, SNAPSHOT_PROPERTY_KEY)):
        p = _finite(r["p"])
        if p is not None:
            recorded[r["m"]] = p
    posted = []
    unknown_until = ""          # newest payment whose principal is unknown
    for ln in live_loans:
        mv = _loan_movements(conn, ln)
        if mv is not None:
            posted.extend(mv)
            continue
        newest = conn.execute(
            "SELECT max(date) AS d FROM transactions"
            " WHERE removed=0 AND account_id = %s AND amount < 0",
            (ln["id"],)).fetchone()["d"]
        unknown_until = max(unknown_until, _iso(newest) if newest else "")
    posted.sort()
    dates = [d for d, _ in posted]
    after = [0.0] * (len(posted) + 1)
    for i in range(len(posted) - 1, -1, -1):
        after[i] = after[i + 1] + posted[i][1]
    out = []
    for m in months:
        if m in recorded:
            out.append(recorded[m])
            continue
        y, mo = int(m[:4]), int(m[5:7])
        end = dt.date(y, mo, calendar.monthrange(y, mo)[1]).isoformat()
        if unknown_until > end:
            out.append(None)
            continue
        # balance then = balance now − what posted since; the layer holds
        # the loan as −balance, so the net then is higher by that sum
        out.append(round(now_net + after[bisect.bisect_right(dates, end)], 2))
    out[-1] = round(now_net, 2)
    return out


def _manual_entry_months(conn, live_loans, cur: str) -> list[tuple]:
    """(month it enters the trend, value, the item) for each hand-entered
    property item the layer counts — the ONE reading of an item's date:
    its `acquired_on` month, else its `as_of`, else the month in progress.
    A manual mortgage a linked one supersedes is not counted and is not
    here."""
    try:
        manual = budget.load_config(conn).get("manual_assets", [])
    except Exception:          # noqa: BLE001
        manual = []
    live_mortgage = any((r["subtype"] or "") == "mortgage" for r in live_loans)
    out = []
    for m in manual if isinstance(manual, list) else []:
        if not isinstance(m, dict):
            continue
        if m.get("kind") == "mortgage" and live_mortgage:
            continue
        try:
            value = float(m.get("value") or 0)
        except (TypeError, ValueError):
            continue
        since = cur
        for key in ("acquired_on", "as_of"):
            day = str(m.get(key) or "")
            if re.match(r"^\d{4}-\d{2}", day):
                since = day[:7]
                break
        out.append((since, value, m))
    return out


_ADDED_NAME_MAX = 24


def _property_steps(months: list, manual: list, items: list,
                    exact: bool) -> list[tuple]:
    """(month, "added" | "changed", item name or None) for every month the
    hand-entered part of the property layer (`manual`, one figure per
    month, None where unknown) moves from the month before: something was
    typed in, taken out or re-valued — never money gained or lost. Read
    off the layer the line is drawn from (_property_layers), so the line
    and a window's note cannot disagree about when a value arrived: a
    home typed in this autumn with an older "owned since" steps where its
    value first appears. With a linked loan the layer's manual part is
    what is left after the loan's walked balance, so only a move past 2%
    counts (`exact` False); without one, any move past a dollar."""
    names = [(float(v), str(m.get("name") or "").strip())
             for _since, v, m in items if m.get("kind") != "mortgage"]
    out = []
    for i in range(1, len(months)):
        a, b = manual[i - 1], manual[i]
        if a is None or b is None:
            continue
        d = b - a
        tol = 1.0 if exact else max(1.0, 0.02 * max(abs(a), abs(b)))
        if abs(d) <= tol:
            continue
        # an arrival: the first property there is, or a rise the size of
        # one item; anything else is a value that changed
        match = [n for v, n in names if abs(v - d) <= tol]
        if d > 0 and (abs(a) <= tol or match):
            name = (match[0] if len(match) == 1
                    else names[0][1] if len(names) == 1 else "")
            out.append((months[i][:7], "added",
                        name if 0 < len(name) <= _ADDED_NAME_MAX else None))
        else:
            out.append((months[i][:7], "changed", None))
    return out


def _added_note(inside: list[tuple]) -> str:
    """What a window's note says moved in the property layer, and when:
    "Home added 09/26" for one arrival (the item's own name when it is
    known), "property added …" for several, "property value changed …"
    when any of the moves is not an arrival; months as MM/YY."""
    when = ", ".join(f"{m[5:7]}/{m[2:4]}"
                     for m in sorted({e[0] for e in inside}))
    if any(kind != "added" for _m, kind, _n in inside):
        what = "property value changed"
    else:
        what = ((inside[0][2] if len(inside) == 1 and inside[0][2]
                 else "property") + " added")
    return (f"{what} {when} — the change is measured on the "
            "financial accounts")


def _like_for_like(window: dict, points: list, financial: list,
                   steps: list) -> dict:
    """An Over-time window with its `note`, and — when the hand-entered
    property value moved inside it (`steps`, _property_steps: after the
    window's first point, on or before its last) — its gain and percent
    measured on the financial accounts alone (`financial`, the same
    months as `points`). A value typed in, taken out or re-valued is not
    money gained or lost (specs/counted-figures.md, "A property added
    inside the window"). The points drawn do not change."""
    out = {**window, "note": None}
    if window["gain"] is None or not steps:
        return out
    lo, hi = points[window["start"]][0][:7], points[window["end"]][0][:7]
    inside = [e for e in steps if lo < e[0] <= hi]
    if not inside:
        return out
    base = financial[window["start"]][1]
    gain = round(financial[window["end"]][1] - base, 2)
    out.update(gain=gain,
               pct=round(100 * gain / base, 2) if base > 0 else None,
               from_text=_from_words(base, financial[window["start"]][0]),
               note=_added_note(inside))
    return out


def _property_entered(conn, months: list, prop: list, live_loans) -> list:
    """The property layer with each item entering on its recorded date
    (_property_layers, which also says what part of it is hand-entered)."""
    return _property_layers(conn, months, prop, live_loans)[0]


def _property_layers(conn, months: list, prop: list,
                     live_loans) -> tuple[list, list]:
    """(the layer, its hand-entered part) month by month.

    The property layer with each item entering on its recorded date: a
    month before an item was recorded carries
    none of it, so earlier months show the financial accounts alone and a
    house first recorded this spring never reads as owned since 1999. A
    hand-entered item enters on its `acquired_on` month — the date it was
    first recorded, or the owner's "owned since"; a value edit restamps
    `as_of` and never moves it (an item stored without one falls back to
    `as_of`, one with neither to the month in progress); a linked loan on
    the month of its first ledger row. A month the nightly snapshot
    recorded a property net for keeps that recording — it is what the page
    showed that day. Leaving an item out takes off what the layer held for
    it that month: a manual item's value, or the loan's balance as `prop`
    carries it there — walked back through its rows and held where
    _held_with_cash holds the layer, so a month held at today's balance
    loses today's balance, not the one before the loan's first row."""
    if not months:
        return prop, []
    cur = months[-1]
    recorded = {r["m"] for r in conn.execute(
        "SELECT DISTINCT to_char(date,'YYYY-MM') AS m FROM networth_snapshot "
        "WHERE jsonb_typeof(by_class) = 'object' "
        "  AND jsonb_typeof(by_class->%s) = 'number'",
        (SNAPSHOT_PROPERTY_KEY,))}
    items = _manual_entry_months(conn, live_loans, cur)
    entries: list[tuple[str, float]] = [
        (since, [value] * len(months)) for since, value, _item in items]
    # a linked loan is a debt the ledger walks back through its own rows;
    # it leaves the earlier months only beside property that entered later
    # (a mortgage with its house), from the earlier of its first row and
    # the first item's month — never on its own, where its walked balance
    # is the debt the household owed then
    first_item = min((since for since, _v in entries), default=None)
    for ln in live_loans if first_item is not None else []:
        first = conn.execute(
            "SELECT min(date) AS d FROM transactions"
            " WHERE removed=0 AND account_id = %s", (ln["id"],)).fetchone()["d"]
        since = min(first.strftime("%Y-%m") if first else cur, first_item)
        # the loan's own share of the layer month by month, walked and held
        # exactly as _property_trend and _held_with_cash build `prop`: a
        # payment it has no principal for (None) moves nothing in the walk
        mv = _loan_movements(conn, ln) or []
        bal = float(ln["bal"] or 0)
        walked = []
        for m in months:
            y, mo = int(m[:4]), int(m[5:7])
            end = dt.date(y, mo, calendar.monthrange(y, mo)[1]).isoformat()
            walked.append(-(bal - sum(a for d, a in mv if d > end)))
        walked[-1] = -bal
        entries.append((since, _held_with_cash(conn, months, walked)))
    loans = entries[len(items):]
    out, manual = [], []
    for i, (m, p) in enumerate(zip(months, prop)):
        if p is None or m in recorded or m == cur:
            out.append(p)
            # what a recording (or today's figure) holds beyond the loans
            # is the hand-entered value as it stood then: nothing before
            # an item was typed in, whatever its "owned since" says
            manual.append(None if p is None else
                          round(p - sum(v[i] for _s, v in loans), 2))
            continue
        out.append(round(p - sum(v[i] for since, v in entries if since > m),
                         2))
        manual.append(round(sum(v for since, v, _it in items if since <= m),
                            2))
    return out, manual


def _snapshot_series(conn):
    return [[_iso(r["date"]), r["total"]] for r in conn.execute(
        "SELECT date, total FROM networth_snapshot ORDER BY date")]


def _cumulative_savings(conn):
    """Reliable historical wealth-building proxy: cumulative (income − spend) by
    year. NOT exact net worth (no early balance snapshots) — labelled as such
    in the UI."""
    excl, ex_p = excluded_accounts_sql(conn)
    spend = {r["p"]: r["amt"] for r in conn.execute(
        f"SELECT to_char(t.date,'YYYY-MM') p, SUM({NET_AMOUNT_JOINED}) amt "
        f"FROM transactions t {REIMB_PARTIAL_JOIN} WHERE {SPEND_WHERE} {excl} GROUP BY p", ex_p)}
    _income_y, income = _income_year_month(conn)
    cov = _income_coverage(conn)
    years = sorted({k[:4] for k in spend} | {k[:4] for k in income})
    run = 0.0; out = []
    for y in years:
        # by the month, as every Cash Flow figure (_income_known): a month
        # the bank feed does not measure has an unknowable net and adds
        # nothing, so the line holds flat through it
        for mo in range(1, 13):
            k = f"{y}-{mo:02d}"
            if _income_known(cov, k):
                run += (income.get(k, 0) or 0) - (spend.get(k, 0) or 0)
        out.append([y, round(run, 2)])
    return out


def _month_ends(start: dt.date, end: dt.date) -> list[str]:
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        last = (dt.date(y + (m == 12), (m % 12) + 1, 1) - dt.timedelta(days=1))
        out.append(min(last, end).isoformat())
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ---- spending -----------------------------------------------------------
def compute_spending(conn, today: dt.date | None = None) -> dict:
    # The trailing windows are measured from the HOUSEHOLD's day (the
    # router passes its timezone's today), bound into the same interval
    # arithmetic CURRENT_DATE drives. Off the DB clock, a household west of
    # UTC watches a day fall out of its 12-month figures every evening while
    # the Cash Flow windows (spending_window) keep it.
    today = today or dt.date.today()
    amt2 = _R2.format(f"SUM({NET_AMOUNT_JOINED})")
    excl, ex_p = excluded_accounts_sql(conn)
    # One pass over all history serves the year totals, the year × category
    # matrix and the all-time category axis: GROUPING SETS aggregates each
    # grouping from the same scan, and each group's SUM is still rounded in
    # SQL exactly as the three separate queries rounded theirs. GROUPING()
    # tells the rows apart — 0 = (yr, cat), 1 = year only, 2 = category only.
    # by category, so the parts of a hand-split row are joined and each
    # lands under its own category for its share (the year totals still
    # add up: the parts sum to the row)
    pamt2 = _R2.format(f"SUM({PART_NET_JOINED})")
    # n counts TRANSACTIONS: the split join yields one row per part, so a
    # plain COUNT(*) would call a three-way split three purchases — a split
    # whose parts land in two categories is one purchase in each, and one
    # in its year. Not COUNT(DISTINCT t.id): a DISTINCT aggregate cannot be
    # hashed, so the whole history would be sorted once per grouping set to
    # correct the few rows that are split. Instead each joined row says
    # whether it is its charge's first part, and its first part in its
    # category (an unsplit row is both); the year set sums the one, the
    # category sets the other.
    first_part = ("(SELECT min(s2.line) FROM transaction_splits s2 "
                  "WHERE s2.tenant_id = sp.tenant_id AND s2.txn_id = sp.txn_id{})")
    txn_first = (f"CASE WHEN sp.line IS NULL THEN 1 "
                 f"WHEN sp.line = {first_part.format('')} THEN 1 ELSE 0 END")
    cat_first = ("CASE WHEN sp.line IS NULL THEN 1 WHEN sp.line = "
                 + first_part.format(
                     f" AND {PART_CAT.replace('sp.', 's2.')} = {PART_CAT}")
                 + " THEN 1 ELSE 0 END")
    all_time = conn.execute(
        f"SELECT to_char(t.date,'YYYY') yr, {PART_CAT} cat, {pamt2} amt, "
        f"CASE WHEN GROUPING({PART_CAT}) = 1 THEN SUM({txn_first}) "
        f"ELSE SUM({cat_first}) END::int n, "
        f"GROUPING(to_char(t.date,'YYYY'), {PART_CAT}) g "
        f"FROM transactions t {REIMB_PARTIAL_JOIN} {SPLIT_JOIN} WHERE {SPEND_WHERE} {excl} "
        f"GROUP BY GROUPING SETS ((yr, cat), (yr), (cat))",
        ex_p or None).fetchall()
    by_year = sorted((r for r in all_time if r["g"] == 1), key=lambda r: r["yr"])
    by_year_cat = [r for r in all_time if r["g"] == 0]
    by_cat = [r for r in all_time if r["g"] == 2]
    by_month = conn.execute(
        f"SELECT to_char(t.date,'YYYY-MM') ym, {amt2} amt "
        f"FROM transactions t {REIMB_PARTIAL_JOIN} WHERE {SPEND_WHERE} "
        f"AND t.date >= %s::date - INTERVAL '36 months' {excl} "
        f"GROUP BY ym ORDER BY ym", (today, *ex_p)).fetchall()
    # Trailing 12 months, not all time. An all-time toplist is
    # dominated by whatever you spent most on since the ledger began — a
    # mortgage paid off years ago outranks this year's groceries forever — and
    # a spending report is about behaviour you can still change. by_year
    # below keeps the long view; that is its job. Twelve months means twelve:
    # the lower bound is strict, or an annual charge dated on today's
    # anniversary counts twice (last year's and this year's), and a row
    # dated past today (a scheduled payment) is not yet spending.
    top_merch = conn.execute(
        f"SELECT {DISPLAY_MERCHANT} payee, {amt2} amt, COUNT(*) n, "
        f"       max({MERCHANT_LOGO}) logo "
        f"FROM transactions t {MC_JOIN} {REIMB_PARTIAL_JOIN} WHERE {SPEND_WHERE} "
        f"AND t.date > %s::date - INTERVAL '12 months' AND t.date <= %s {excl} "
        f"GROUP BY payee ORDER BY amt DESC LIMIT 40", (today, today, *ex_p)).fetchall()
    # Amazon's own sub-categories, all time — the category grouping above,
    # narrowed to the 'Amazon…' prefix (LIKE 'Amazon%' is a case-sensitive
    # prefix test, which startswith reproduces).
    amazon = sorted((r for r in by_cat if (r["cat"] or "").startswith("Amazon")),
                    key=lambda r: -r["amt"])
    # Two lists, on purpose. The DISPLAYED toplist is trailing-12-month for
    # the reason above; the matrix's category AXIS stays all-time, because
    # narrowing it would quietly drop a category that was large for years
    # from the year-over-year table — which is the one place you would go to
    # see exactly that.
    cat_tot = conn.execute(
        f"SELECT {PART_CAT} cat, {pamt2} amt FROM transactions t {REIMB_PARTIAL_JOIN} "
        f"{SPLIT_JOIN} WHERE {SPEND_WHERE} "
        f"AND t.date > %s::date - INTERVAL '12 months' AND t.date <= %s {excl} "
        f"GROUP BY cat ORDER BY amt DESC LIMIT 15", (today, today, *ex_p)).fetchall()
    cat_all = sorted(by_cat, key=lambda r: -r["amt"])[:15]
    years = sorted({r["yr"] for r in by_year_cat})
    cats = [r["cat"] for r in cat_all]
    matrix = {(r["yr"], r["cat"]): r["amt"] for r in by_year_cat}
    return {
        "by_year": [[r["yr"], r["amt"], r["n"]] for r in by_year],
        "by_month": [[r["ym"], r["amt"]] for r in by_month],
        # [payee, amount, count, logo_url|null] — clients that read only
        # the first three still work
        "top_merchants": [[r["payee"], r["amt"], r["n"], r["logo"]] for r in top_merch],
        "amazon": [[r["cat"], r["amt"], r["n"]] for r in amazon],
        "category_totals": [[r["cat"], r["amt"]] for r in cat_tot],
        "yoy_years": years, "yoy_cats": cats,
        "yoy_matrix": [[c] + [matrix.get((y, c), 0) for y in years] for c in cats],
    }


# ---- cash flow & savings ------------------------------------------------
# investment-provider tokens as they appear in bank-transaction descriptors
# and institution names. Income no longer reads them to decide which
# ACCOUNTS are banks (see _bank_account_ids); they still name the provider
# on a deposit's own line, and the investment-funding and cash-cost figures
# use them to tell a provider's accounts from a bank's.
_INV_PROVIDERS = ("acorns", "betterment", "coinbase", "fidelity", "robinhood",
                  "schwab", "sofi invest", "vanguard", "wealthfront")
_INV_PATTERNS = ["%" + p + "%" for p in _INV_PROVIDERS]
# own-money inflows a bank stamps INCOME: transfers back from an investment
# provider (stock sells moved to checking — the budget engine treats
# those as shortfall funding, never income) and "Ext Trans" (transfers
# between own accounts at different banks). A deposit the aggregator
# tagged as wages is a paycheck whoever pays it — an employer can share a
# provider's name — so the descriptor test leaves it alone. Literal SQL so
# _INCOME_WHERE keeps its one parameter; the doubled percents survive the
# param step.
# A deposit whose description names payroll is pay too, for the same
# reason (specs/paychecks.md).
_PAID_AS_PAY_SQL = (
    "(COALESCE(t.category_detailed, '') = 'INCOME_WAGES'"
    " OR COALESCE(t.name, '') ~* '\\m(payroll|payrl|pyrl|salary|wages?|paychecks?"
    "|direct ?dep(osit)?|dir ?dep|paychex|gusto|adp)\\M')")
# Brokers' names and words a deposit of money back from a brokerage carries
# beyond the providers above — read for income only, so the investment
# funding figures keep the provider list they always had.
_BROKER_WORDS = ("brokerage", "moneylink", "etrade", "e*trade", "e-trade",
                 "ameritrade", "merrill edge", "merrill lynch", "webull",
                 "interactive brokers", "m1 finance", "ally invest",
                 "firstrade", "tastytrade")
_NOT_OWN_MONEY_SQL = (
    f"AND ({_PAID_AS_PAY_SQL} OR ("
    + " AND ".join(f"LOWER(t.name) NOT LIKE '%%{tok}%%'"
                   for tok in (*_INV_PROVIDERS, *_BROKER_WORDS, "ext trans"))
    + "))")
# A card payment coming back (a returned card autopay) is the household's
# own money, not money earned — and the payment it undid never counted as
# spending, so it must not count as income either. Every other payment
# that bounces (a utility ACH return, a mortgage payment reversed) undid a
# payment that DID count as spending; that deposit stays income so the
# month's net is unchanged, exactly as if neither row had posted
# (specs/paychecks.md → "Money back is not income").
_NOT_MONEY_BACK_SQL = (
    f"AND ({_PAID_AS_PAY_SQL} OR ("
    "COALESCE(t.category_detailed, '') <> 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'))")

# A deposit into a brokerage's cash account — a depository account held in
# the same login as an investment account — counts as income only when it
# is stamped INCOME. That account takes the household's paycheck and earns
# interest like any bank account, but it also receives the cash leg of every
# sale and sweep out of the portfolio beside it, which is the household's
# own money moving, not money earned. The test is the login's account
# TYPES, never the institution's name: a bank whose name reads like a
# brokerage is a bank. Hand-made and file-imported accounts all share the
# item 'manual', which is no login at all — a hand-made IRA beside an
# imported checking account must not turn that checking into brokerage
# cash and drop its paychecks.
_BROKERAGE_CASH_ONLY_INCOME_SQL = """AND (COALESCE(t.category_override,
                                          t.category_primary, '') = 'INCOME'
              OR NOT EXISTS (SELECT 1 FROM accounts ba
                             JOIN accounts ia ON ia.item_id = ba.item_id
                                             AND ia.type = 'investment'
                             WHERE ba.id = t.account_id
                               AND ba.item_id <> 'manual'))"""


def _bank_account_ids(conn, *, brokerage_cash: bool = True) -> list[str]:
    """The household's bank accounts for income: every depository account,
    by its TYPE. The institution's name decides nothing — a paycheck banked
    at a bank whose name reads like a brokerage is income on every surface,
    exactly as the budget's month plan already counted it. A brokerage's
    cash account (depository, in a login that also holds an investment
    account) is included, and _INCOME_WHERE counts only its INCOME-stamped
    deposits; `brokerage_cash=False` leaves those accounts out, for the
    coverage question (from when is income measured), where a cash account
    that only ever received sweeps would call years known that no paycheck
    was ever seen in.

    Reaching these rows by joining accounts inside a full scan of
    transactions costs the whole ledger; resolving the handful of account
    ids first lets the same predicate run as an index probe over the bank
    rows alone."""
    cash = "" if brokerage_cash else """
             AND NOT EXISTS (SELECT 1 FROM accounts ia
                             WHERE ia.item_id = a.item_id
                               AND ia.item_id <> 'manual'
                               AND ia.type = 'investment')"""
    return [r["id"] for r in conn.execute(
        f"""SELECT a.id FROM accounts a
           WHERE a.type='depository'{cash}""")]


# A deposit stamped TRANSFER_IN whose partial links do not use it up. A
# partial link repays one charge out of the deposit and leaves the rest as
# income, so such a deposit is income net of its links (INCOME_NET)
# whatever its override says: the stamp may predate the rule that a
# partial link leaves the deposit's category alone, or come back that way
# from an archive, and reading it as a whole transfer would drop the
# unclaimed part from income. A partial-linked deposit is a repayment by
# construction, so a transfer set by hand on one reads the same way. Not a
# deposit with a full pair (a full link spends all of it), and not one the
# aggregator itself called a transfer — that was never income.
#
# Links to a charge that has left the ledger claim nothing: a deposit
# whose every charge was withdrawn is income again in full until the pair
# is released. It still has to carry some pair, so a transfer set by hand
# on a deposit nobody linked stays a transfer.
PARTLY_CLAIMED_DEPOSIT = """(t.category_override = 'TRANSFER_IN'
    AND COALESCE(t.category_primary, '') NOT IN ('TRANSFER_IN','TRANSFER_OUT')
    AND EXISTS (SELECT 1 FROM reimbursements ar WHERE ar.reimburse_id = t.id)
    AND NOT EXISTS (SELECT 1 FROM reimbursements fr
                    JOIN transactions fe ON fe.id = fr.expense_id
                                        AND fe.removed = 0
                    WHERE fr.reimburse_id = t.id AND fr.partial = 0)
    AND COALESCE((SELECT SUM(pr.amount) FROM reimbursements pr
                  JOIN transactions pe ON pe.id = pr.expense_id
                                      AND pe.removed = 0
                  WHERE pr.reimburse_id = t.id AND pr.partial = 1), 0)
        < -t.amount - 0.005)"""

# The income definition, as SQL over bank rows. Own-money inflows a bank
# stamps INCOME are excluded (_NOT_OWN_MONEY_SQL), and so is a brokerage
# cash account's deposit that is not stamped INCOME
# (_BROKERAGE_CASH_ONLY_INCOME_SQL). Callers supply the bank account ids
# as the one parameter.
_INCOME_WHERE = f"""t.removed=0 AND t.amount < 0 AND t.account_id = ANY(%s)
              {_NOT_OWN_MONEY_SQL}
              {_NOT_MONEY_BACK_SQL}
              {_BROKERAGE_CASH_ONLY_INCOME_SQL}
              AND (COALESCE(t.category_override, t.category_primary, '')
                   NOT IN ('TRANSFER_IN','TRANSFER_OUT')
                   OR {PARTLY_CLAIMED_DEPOSIT})
              {NOT_RECLASSIFIED_TRANSFER_IN}
              {_NOT_SHADOW.format(col="t.account_id")}
              {budget.PERSONAL_ONLY_SQL}"""


def _income_by(conn, grain: str):
    # Bank accounts only — see _bank_account_ids for why the ids are resolved
    # first and _INCOME_WHERE for what counts as income.
    col = "to_char(t.date,'%s')" % ("YYYY-MM" if grain == "month" else "YYYY")
    excl, ex_p = excluded_accounts_sql(conn)
    return conn.execute(
        f"""SELECT {col} p, {_R2.format(f"SUM({INCOME_NET})")} amt FROM transactions t
            WHERE {_INCOME_WHERE} {excl}
            GROUP BY p ORDER BY p""",
        (_bank_account_ids(conn), *ex_p)).fetchall()


def _income_year_month(conn, since: dt.date | None = None,
                       until: dt.date | None = None) -> tuple[dict, dict]:
    """Both grains of _income_by from one pass: ({year: amt}, {month: amt}).
    GROUPING SETS rounds each grouping's SUM in SQL exactly as the two
    single-grain queries do, so the figures are the same — only the scan is
    shared. For callers that need both (cash flow, the year lens).
    `since`/`until` bound the read to whole years for a caller that shows
    only those (the Year page reads two years, not the household's whole
    history); a bounded year's figure is the same as the unbounded one."""
    years: dict = {}
    months: dict = {}
    excl, ex_p = excluded_accounts_sql(conn)
    bound, b_p = "", []
    if since is not None:
        bound += " AND t.date >= %s"
        b_p.append(since)
    if until is not None:
        bound += " AND t.date < %s"
        b_p.append(until)
    for r in conn.execute(
            f"""SELECT to_char(t.date,'YYYY') yr, to_char(t.date,'YYYY-MM') p,
                       {_R2.format(f"SUM({INCOME_NET})")} amt
                FROM transactions t
                WHERE {_INCOME_WHERE}{bound} {excl}
                GROUP BY GROUPING SETS ((yr), (p))""",
            (_bank_account_ids(conn), *b_p, *ex_p)):
        if r["p"] is None:
            years[r["yr"]] = r["amt"]
        else:
            months[r["p"]] = r["amt"]
    return years, months


def _flow_parts(conn, *, start: dt.date | None, end: dt.date | None,
                ids: list[str] | None, category: str | None,
                with_income: bool,
                where: tuple[str, tuple] | None = None
                ) -> tuple[tuple, tuple | None]:
    """The two halves `flows` sums, as (amount, FROM…WHERE, params) for
    money out and (when asked) money in — one definition read by `flows`
    and `flows_by_month`, so a month's figures always add up to the
    whole. `where` is a predicate over `transactions t` (SQL, params) —
    a search's own match, so a large result is filtered in the query
    rather than shipped back in as an id list."""
    excl, ex_p = excluded_accounts_sql(conn)
    bound, b_p = "", []
    if where is not None:
        bound += f" AND ({where[0]})"
        b_p.extend(where[1])
    if start is not None:
        bound += " AND t.date >= %s"
        b_p.append(start)
    if end is not None:
        bound += " AND t.date < %s"
        b_p.append(end)
    if ids is not None:
        bound += " AND t.id = ANY(%s)"
        b_p.append(list(ids))
    if category is None:
        out = (NET_AMOUNT_JOINED,
               f"""FROM transactions t {REIMB_PARTIAL_JOIN}
                WHERE {SPEND_WHERE}{bound} {excl}""",
               (*b_p, *ex_p))
    else:
        out = (PART_NET_JOINED,
               f"""FROM transactions t {REIMB_PARTIAL_JOIN} {SPLIT_JOIN}
                WHERE {SPEND_WHERE}{bound}
                  AND COALESCE(sp.category, t.category_override,
                               t.category_primary, '') = %s {excl}""",
               (*b_p, category, *ex_p))
    if not with_income:
        return out, None
    inc = (INCOME_NET,
           f"FROM transactions t WHERE {_INCOME_WHERE}{bound} {excl}",
           (_bank_account_ids(conn), *b_p, *ex_p))
    return out, inc


def flows(conn, *, start: dt.date | None = None, end: dt.date | None = None,
          ids: list[str] | None = None, category: str | None = None,
          with_income: bool = True,
          where: tuple[str, tuple] | None = None) -> dict:
    """Money in / out / net over a window, a set of rows, or both — THE
    definition every in/out figure the ledger prints is read from
    (specs/ledger-figures.md). Out is spending by SPEND_WHERE (card
    payments, transfers, loan accounts, shadows and business money left
    out; partial reimbursements netted), in is income by _INCOME_WHERE
    over the bank accounts; both drop the excluded accounts. So a window's
    figures are Cash Flow's for the same days (spending_window /
    income_window total these predicates), and a search's are the same
    rule over the rows it matched.

    `start`/`end` (end EXCLUSIVE) bound the dates; `ids` restricts to
    those rows. `category` counts only the parts of a split row filed
    under it (the search's category filter), as the category rollups do.
    `with_income=False` skips the income sum for a caller that states it
    by its own rule (month_flows: unknown before the bank feed starts).
    `where` restricts to a predicate instead (see _flow_parts)."""
    (o_amt, o_from, o_p), inc_part = _flow_parts(
        conn, start=start, end=end, ids=ids, category=category,
        with_income=with_income, where=where)
    out = conn.execute(
        f"SELECT {_R2.format(f'COALESCE(SUM({o_amt}), 0)')} amt {o_from}",
        o_p).fetchone()["amt"]
    if inc_part is None:
        return {"out": float(out or 0), "in": None, "net": None}
    i_amt, i_from, i_p = inc_part
    inc = conn.execute(
        f"SELECT {_R2.format(f'COALESCE(SUM({i_amt}), 0)')} amt {i_from}",
        i_p).fetchone()["amt"]
    out, inc = float(out or 0), float(inc or 0)
    return {"out": out, "in": inc, "net": round(inc - out, 2)}


def flows_by_month(conn, *, ids: list[str] | None = None,
                   category: str | None = None,
                   start: dt.date | None = None,
                   end: dt.date | None = None,
                   where: tuple[str, tuple] | None = None) -> list[dict]:
    """`flows`, one entry per calendar month that has money in or out,
    oldest first: [{month: "YYYY-MM", out, in, net}]. The same halves
    `flows` sums, grouped, so the months add up to its figures (each
    month rounded to the cent on its own)."""
    out_part, inc_part = _flow_parts(conn, start=start, end=end, ids=ids,
                                     category=category, with_income=True,
                                     where=where)
    months: dict[str, dict] = {}
    for key, (amt, frm, params) in (("out", out_part), ("in", inc_part)):
        for r in conn.execute(
                f"SELECT to_char(t.date, 'YYYY-MM') ym, "
                f"{_R2.format(f'COALESCE(SUM({amt}), 0)')} amt {frm} "
                f"GROUP BY 1", params):
            m = months.setdefault(r["ym"], {"month": r["ym"], "out": 0.0,
                                            "in": 0.0})
            m[key] = float(r["amt"] or 0)
    for m in months.values():
        m["net"] = round(m["in"] - m["out"], 2)
    return [months[k] for k in sorted(months)
            if months[k]["out"] or months[k]["in"]]


# the category a row (or a split row's part) is filed under, as the
# search's category filter and flows(category=) compare it; '' is none
_FILED_UNDER = "COALESCE(sp.category, t.category_override, t.category_primary, '')"


def match_groups(conn, where: str, params: tuple, *,
                 category: str | None = None, top: int | None = 10) -> dict:
    """A search's matched rows — the predicate `where` over
    `transactions t` with its `params` — grouped the reports' way: money
    in / out / net by month (`flows_by_month`), spending by category (a
    hand-split charge under each part, at its share) and the merchants by
    spending (`top` of them, None for all), plus how many matched rows
    the money figures leave out (`left_out`: business money, and rows on
    accounts the household excludes, hides or holds as a linked copy).

    Each group is keyed by exactly what the search filters on — the
    stored category ("?" for none) and the name a row is shown under —
    so a key passed back as the filter finds exactly the rows and the
    money it was given for. Ties are ordered by the key.

    `category` is the search's category filter. Under it only the
    matching part of a split charge counts — in the months, the
    categories and the merchants alike — which is what the search's own
    money-out total counts, so each grouping adds up to it. ("?", no
    category, has no parts: its rows count whole.)"""
    part = category if category and category != "?" else None
    by_month = flows_by_month(conn, category=part, where=(where, params))
    excl, ex_p = excluded_accounts_sql(conn)
    base = f"{SPEND_WHERE} AND ({where})"
    if part is None:
        cat_cond, cat_p = "", ()
    else:
        cat_cond = f" AND {_FILED_UNDER} = %s"
        cat_p = (part,)
    by_cat = []
    for r in conn.execute(
            f"SELECT COALESCE(NULLIF({_FILED_UNDER}, ''), '?') k,"
            f" {_R2.format(f'SUM({PART_NET_JOINED})')} amt FROM transactions t "
            f"{REIMB_PARTIAL_JOIN} {SPLIT_JOIN} WHERE {base}{cat_cond} {excl} "
            f"GROUP BY k ORDER BY amt DESC, k", (*params, *cat_p, *ex_p)):
        amt = round(float(r["amt"] or 0), 2)
        if amt:
            by_cat.append({"category": r["k"],
                           "label": _categories.display_label(r["k"]),
                           "amount": amt})
    # the merchants: the whole row, or under a category its matching part
    amt_expr = NET_AMOUNT_JOINED if part is None else PART_NET_JOINED
    split = "" if part is None else SPLIT_JOIN
    merchants = []
    for r in conn.execute(
            f"SELECT {DISPLAY_MERCHANT} payee, "
            f"{_R2.format(f'SUM({amt_expr})')} amt, COUNT(DISTINCT t.id) n "
            f"FROM transactions t {MC_JOIN} {REIMB_PARTIAL_JOIN} {split} "
            f"WHERE {base}{cat_cond} {excl} GROUP BY payee "
            f"ORDER BY amt DESC, payee"
            + ("" if top is None else f" LIMIT {int(top)}"),
            (*params, *cat_p, *ex_p)):
        if round(float(r["amt"] or 0), 2) > 0:
            merchants.append({"merchant": r["payee"],
                              "amount": round(float(r["amt"]), 2),
                              "count": int(r["n"])})
    # what the figures above cannot count however the rows are found: a
    # business's money, and the accounts the money math sets aside
    from . import links
    aside = (budget.excluded_account_ids(budget.load_config(conn))
             + list(links.shadow_ids(conn)))
    left = conn.execute(
        f"SELECT COUNT(*) FILTER (WHERE NOT (TRUE {budget.PERSONAL_ONLY_SQL}))"
        f" AS business, COUNT(*) FILTER (WHERE (TRUE {budget.PERSONAL_ONLY_SQL})"
        f" AND t.account_id = ANY(%s::text[])) AS set_aside "
        f"FROM transactions t WHERE ({where})", (aside, *params)).fetchone()
    return {"by_month": by_month, "by_category": by_cat,
            "top_merchants": merchants,
            "left_out": {"business": int(left["business"]),
                         "set_aside": int(left["set_aside"])}}


def month_flows(conn, year: int, month: int) -> dict:
    """One month's household money in / out / net, by the SAME definitions
    the Cash Flow page uses (SPEND_WHERE / _INCOME_WHERE), so the ledger's
    headline and Cash Flow cannot disagree. This is what the Transactions
    month header states: a raw sum of the listed rows counts card spend
    twice (once at the swipe, once at the payment) and calls a transfer
    between own accounts money out, which makes the header useless for the
    question it exists to answer — did the month cost more than it paid.

    `in` and `net` are None for a month before the bank feed starts (the
    Month lens's rule): its income is unknown, and $0 in would show the
    month's whole spend as a loss."""
    start = dt.date(year, month, 1)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    excl, ex_p = excluded_accounts_sql(conn)
    out = flows(conn, start=start, end=end, with_income=False)["out"]
    # the exclusion just read, handed on so the income half does not read
    # the config a second time
    inc = month_income(conn, year, month, memo={
        budget._memo_key("reporting.excluded_accounts_sql"): (excl, ex_p)})
    if inc is None:
        return {"out": out, "in": None, "net": None}
    return {"out": out, "in": inc, "net": round(inc - out, 2)}


def _month_income_scope(conn, year: int, month: int, *,
                        memo: dict | None, until: dt.date | None,
                        excluded: list | None) -> tuple[str, tuple] | None:
    """(WHERE, params) over `transactions t` for the month's income rows,
    or None for a month before the bank feed starts — the shared half of
    month_income and month_income_rows.

    `until` (exclusive) cuts the month short: a day viewed inside the month
    counts what had landed by then, not deposits that came after it.
    `excluded` is the excluded-account list of the config the month is
    judged under (a closed month's snapshot); without it the live config's
    list applies. An explicit list never reads or fills the memo's live
    exclusion, so the two can never be served for each other."""
    start = dt.date(year, month, 1)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    if until is not None:
        end = min(end, until)

    def _once(name, fn):
        if memo is None:
            return fn()
        key = budget._memo_key(name)
        if key not in memo:
            memo[key] = fn()
        return memo[key]

    mw = _once("coverage.measured_window", lambda: coverage.window(conn))
    if not mw.month_known(f"{year:04d}-{month:02d}"):
        return None
    if excluded is None:
        excl, ex_p = _once("reporting.excluded_accounts_sql",
                           lambda: excluded_accounts_sql(conn, memo=memo))
    else:
        excl, ex_p = (("AND NOT (t.account_id = ANY(%s))", (list(excluded),))
                      if excluded else ("", ()))
    bank = _once("reporting.bank_account_ids",
                 lambda: _bank_account_ids(conn))
    return (f"{_INCOME_WHERE} AND t.date >= %s AND t.date < %s {excl}",
            (bank, start, end, *ex_p))


def month_income(conn, year: int, month: int, *,
                 memo: dict | None = None, until: dt.date | None = None,
                 excluded: list | None = None) -> float | None:
    """month_flows' `in` alone — the month's income by the Cash Flow
    definition (_INCOME_WHERE), None for a month before the bank feed
    starts. The budget's month plan reads only this half, once per month
    of a window (the fixed-spend totals of an all-history Cash Flow run it
    for every month of the ledger), so it skips the spend sum, and with
    the request's `memo` the per-household inputs — the bank accounts,
    the exclusion, the months the feed covers — are read once per
    request instead of once per month. `until` and `excluded`: see
    _month_income_scope."""
    scope = _month_income_scope(conn, year, month, memo=memo, until=until,
                                excluded=excluded)
    if scope is None:
        return None
    where, params = scope
    return conn.execute(
        f"""SELECT {_R2.format(f"COALESCE(SUM({INCOME_NET}), 0)")} amt
            FROM transactions t WHERE {where}""", params).fetchone()["amt"]


def month_deposit_rows(conn, year: int, month: int, since: dt.date,
                       until: dt.date, *, excluded: list,
                       memo: dict | None = None,
                       today: dt.date | None = None
                       ) -> tuple[list[dict], bool]:
    """Every deposit dated [since, until) the budget's month plan reads,
    from ONE statement: the rows a paycheck occurrence may be matched
    against (`matchable`: depository, not a transfer — the _inflow_rows
    rule, pending included) and the rows the ledger counts as the month's
    income (`counted`: _INCOME_WHERE, with `net` its contribution). Both
    flags come from the same snapshot, so a pending paycheck that posts
    between two reads cannot be counted once as each.

    `excluded` (with the linked-account shadows) drops accounts entirely.
    Returns (rows, known): `known` is False for a month before the bank
    feed starts, whose ledger income is unknown — `counted` is then False
    on every row."""
    from . import links, merchant_sql
    key = budget._memo_key("reporting.month_deposit_rows", year, month,
                           since, until, sorted(excluded))
    if memo is not None and key in memo:
        return memo[key]

    def _once(name, fn):
        if memo is None:
            return fn()
        k = budget._memo_key(name)
        if k not in memo:
            memo[k] = fn()
        return memo[k]

    # `today`: the day the caller reads for (an as-of forecast), so the
    # ledger's "present" is that day and not the wall clock's. A build
    # that reads many months as of one day names that day in its memo
    # ("forecast.as_of"), and they share one window
    as_of = (memo or {}).get("forecast.as_of") or today
    mw = (_once("coverage.measured_window", lambda: coverage.window(conn))
          if as_of is None else
          _once(f"coverage.measured_window.{as_of.isoformat()}",
                lambda: coverage.window(conn, today=as_of)))
    known = mw.month_known(f"{year:04d}-{month:02d}")
    bank = _once("reporting.bank_account_ids",
                 lambda: _bank_account_ids(conn)) if known else []
    drop = list(excluded) + links.shadow_ids(conn)
    rows = conn.execute(
        f"""SELECT t.id AS txn_id, t.date, -t.amount AS amount,
                   {merchant_sql.RAW_KEY} AS payee, t.name,
                   t.merchant_id::text AS merchant_id, t.pending,
                   t.account_id,
                   ({_INCOME_WHERE}) AS counted,
                   ({INCOME_NET})::float8 AS net,
                   (a.type = 'depository'
                    AND COALESCE(t.category_override, t.category_primary, '')
                        NOT IN ('TRANSFER_OUT','TRANSFER_IN')) AS matchable
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE t.removed = 0 AND t.amount < 0
              AND t.date >= %s AND t.date < %s
              AND NOT (t.account_id = ANY(%s))
              {budget.PERSONAL_ONLY_SQL}
            ORDER BY t.amount ASC, t.id""",
        (bank, since, until, drop)).fetchall()
    out = [dict(r, date=as_date(r["date"]),
                counted=bool(known and r["counted"])) for r in rows]
    out = [r for r in out if r["matchable"] or r["counted"]]
    if memo is not None:
        memo[key] = (out, known)
    return out, known


_MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
        "Oct", "Nov", "Dec"]


# the one timeframe vocabulary every over-time toggle on the site speaks
# (Net Worth set it; Cash Flow and the rest follow): months back, None = all.
# Cash Flow adds two short windows: "cur" is the household's month so far
# (one month, open to today), "1m" is the last COMPLETE month — a window
# that closes before today, which no other key does (see _range_window).
FLOW_RANGES = {"cur": 1, "1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36,
               "5y": 60, "all": None}

# (tenant, inputs fingerprint, month-start iso) -> (fixed actual,
# expires-at). Closed months only — see _fixed_for. The expiry (a day,
# below) bounds a cache miss nobody notices; the fingerprint bounds the one
# they would. Keyed on time alone, a bill edit or a combined-entities
# toggle would leave the Cash Flow page showing the pre-edit fixed split
# until the entry expired, process-wide — the page disagreeing with the
# Budget page it promises to agree with. The stamp is the bills the match runs against,
# the config it runs under, AND the ledger the match reads: a closed
# month's fixed total is the sum of the charges the bills matched, so a
# charge re-filed, reimbursed, split or removed, or an account hidden,
# moves it with no bill or setting changing. Any change to any of them
# misses instead.
# The dated rows (_FIXED_DATED_TABLES) are not in the tenant-wide stamp:
# each entry carries the digest of the month stamps its walk reads and is
# served only while that digest still matches, so a write misses the
# months it can reach and no others.
# A closed run's (income by kind, spend) sits here too, under a key of its
# own (see flow_breakdown).
_FIXED_MONTH_CACHE: dict[tuple[str, str, str],
                         tuple[object, float, str]] = {}
# The stamp each household's cache entries were last written under. A new
# stamp (any ledger, bill or config write) makes every entry under the old
# one unreachable, so they are dropped at once instead of sitting until the
# dict passes its size cap — hourly feed pulls would otherwise leave a copy
# of every closed run per stamp in every worker.
_FIXED_CACHE_STAMP: dict[str, str] = {}
# Requests run on the worker's thread pool and all share the dict, so the
# turn — the one place that walks it — holds a lock, and takes a snapshot
# of the keys so a write from a request that slipped in before the lock
# cannot change the size of what it iterates.
_FIXED_CACHE_LOCK = threading.Lock()


def _fixed_cache_turn(tid: str, stamp: str) -> None:
    """Forget a household's entries written under an earlier stamp."""
    if _FIXED_CACHE_STAMP.get(tid) == stamp:
        return
    with _FIXED_CACHE_LOCK:
        if _FIXED_CACHE_STAMP.get(tid) == stamp:
            return
        for k in list(_FIXED_MONTH_CACHE.keys()):
            if k[0] == tid and k[1] != stamp:
                _FIXED_MONTH_CACHE.pop(k, None)
        _FIXED_CACHE_STAMP[tid] = stamp
        if len(_FIXED_MONTH_CACHE) > 100_000:
            _FIXED_MONTH_CACHE.clear()
# the stamp in the key already misses on any change to an input; the
# expiry is a backstop for an input the stamp does not name, not the
# freshness rule. Ten minutes made every visit after a quiet spell walk
# the whole ledger's closed months again (half a second for a year, five
# for all time) — a day keeps the backstop and drops the re-walks.
_FIXED_CACHE_TTL = 86_400.0

# tenant -> (stamp, expiry, paychecks.PayerVerdicts): the flow picture's
# paycheck verdict under the inputs it was read from (_cached_verdicts)
_VERDICT_CACHE: dict[str, tuple[str, float, object]] = {}


def _cached_verdicts(conn, tid: str, stamp: str, month_stamps: dict,
                     memo: dict):
    """paychecks.payer_verdicts, kept per household while the bills, the
    config (the household's words ride in it), the undated ledger tables
    and every month's dated rows are unchanged — the stamps the closed
    runs are filed under — and for no longer than the runs are."""
    key = hashlib.md5(
        (stamp + "|" + "|".join(f"{m}:{v}" for m, v in
                                sorted(month_stamps.items()))).encode(),
        usedforsecurity=False).hexdigest()
    now = dt.datetime.now().timestamp()
    hit = _VERDICT_CACHE.get(tid)
    if hit is not None and hit[0] == key and hit[1] > now:
        memo["paycheck_verdicts"] = hit[2]
        return hit[2]
    v = _paychecks.payer_verdicts(conn, memo=memo)
    if len(_VERDICT_CACHE) > 10_000:
        _VERDICT_CACHE.clear()
    _VERDICT_CACHE[tid] = (key, now + _FIXED_CACHE_TTL, v)
    return v
# what a month's fixed actual reads besides bills and config: every table
# the budget engine's ledger query depends on (the charges, what they are
# filed and split under, what reimbursed them, the accounts and connections
# they sit in, which copy of a linked account counts, the merchants the
# match keys on), plus the month's frozen budget. Derived from the budget
# engine's own list rather than restated, so an input added there cannot
# escape this cache for a day.
_FIXED_LEDGER_TABLES = tuple(dict.fromkeys(
    (*budget._LEDGER_TABLES, "budget_snapshots")))
# The ledger tables whose rows belong to a DATE — the charge itself, or
# something hung off one charge (its split parts, its category override,
# the reimbursement on either side of it) — named with the column that
# finds that charge. They are stamped per calendar month instead of
# tenant-wide (_fixed_month_stamps): a row dated in one month can reach
# only the months whose walk reads it, so a charge synced today, or an
# override on last week's coffee, leaves every month before it cached.
# Every other table stays in the tenant-wide stamp — a table added to the
# budget engine's list lands there, the safe side.
_FIXED_DATED_TABLES: dict[str, tuple[str, ...]] = {
    "transactions": (),
    "transaction_splits": ("txn_id",),
    "manual_categories": ("transaction_id",),
    "reimbursements": ("expense_id", "reimburse_id"),
}
# Two of those tables are rewritten by every bank pull whether or not
# anything moved: the account's balance and pulled-at time, the connection's
# cursor. A row version over them would make each pull throw the whole
# closed-month cache away, so they are fingerprinted by VALUE instead, every
# column except the ones named here. Naming what is left out rather than
# what is kept means a column a migration adds is an input until someone
# says otherwise: the failure mode of this list is a slow page, and the
# failure mode of the opposite list is a wrong closed-month total.
#
# Nothing the total reads may be named here unless the stamp answers for it
# another way: accounts.updated_at, and items' error status beside it, reach
# the total only through which copy of a linked account counts, stamped as
# the effective shadow set; accounts.balance_current only through whether a
# card-payment bill is dropped for live card debt, stamped as that answer.
# items.access_token is not read and has no business in a hash.
_FIXED_NOT_INPUTS = {
    "accounts": ("updated_at", "raw", "balance_current", "balance_available",
                 "balance_limit"),
    "items": ("access_token", "tx_cursor", "tx_update_status", "raw",
              "bank_updated_at", "bank_failed_at", "webhook_status",
              "webhook_status_at"),
}


def _fixed_row_stamp(table: str) -> str:
    """SQL scalar: a table's row count and row versions, or for a table in
    _FIXED_NOT_INPUTS, a hash of its rows without the named columns."""
    skipped = _FIXED_NOT_INPUTS.get(table)
    if skipped is None:
        return (f"(SELECT count(*) || ':' || "
                f"COALESCE(sum(xmin::text::bigint), 0) FROM {table})")
    names = ", ".join(f"'{c}'" for c in skipped)
    return (f"(SELECT COALESCE(md5(string_agg((to_jsonb(r) - "
            f"ARRAY[{names}]::text[])::text, '|' ORDER BY r.id)), '') "
            f"FROM {table} r)")


def _fixed_inputs_stamp(conn, cfg: dict) -> str:
    """Fingerprint of everything a closed month's fixed total is computed
    from: the bill rows themselves, the budget config, and the ledger
    tables the match reads. The ledger part is a row count and a sum of
    row versions per table — any insert, update or delete changes it —
    so one scan per call stands in for hundreds of month walks. The
    accounts and connections are hashed by value (_FIXED_NOT_INPUTS)."""
    return _inputs_stamp(conn, cfg, settings_row=True)


def _inputs_stamp(conn, cfg: dict, *, settings_row: bool) -> str:
    """_fixed_inputs_stamp, with the settings row's own version left out
    when not `settings_row`: the config then reaches the stamp only as
    `cfg`'s value, so a caller that hashes a view of the config (the
    forecast's learned facts, which no send stamp feeds) is not moved by a
    write to a key outside it."""
    row = conn.execute(
        """SELECT COALESCE(md5(string_agg(
                 id || ':' || COALESCE(type, '') || ':'
                    || COALESCE(amount::text, '') || ':'
                    || COALESCE(monthly_amount::text, '') || ':'
                    || COALESCE(payee, '') || ':' || COALESCE(merchant, '')
                    || ':' || COALESCE(frequency, '') || ':'
                    || COALESCE(category, '') || ':'
                    || COALESCE(account_id, '') || ':'
                    || COALESCE(due_on::text, '') || ':'
                    || COALESCE(active::text, '') || ':'
                    -- raw carries what the match keys on beyond the
                    -- columns: the merchant list, an envelope's match
                    -- category, bill type and months
                    || COALESCE(raw::text, ''), '|' ORDER BY id)), '') AS h
             FROM bills""").fetchone()
    ledger = conn.execute(
        "SELECT " + " || '|' || ".join(
            _fixed_row_stamp(t) for t in _FIXED_LEDGER_TABLES
            if t not in _FIXED_DATED_TABLES
            and (settings_row or t != "tenant_settings")) + " AS v"
    ).fetchone()["v"]
    # The effective shadow set, not just the link table: a linked primary
    # whose feed goes stale fails over by the clock alone, with no row
    # written, and the backup's charges start counting.
    from . import links
    shadows = ",".join(sorted(links.shadow_ids(conn)))
    # The one way a balance reaches the total: a card-payment bill leaves
    # the schedule while live card debt exists, and the charge it claimed
    # turns variable. Asked of the budget engine's own filter under the
    # config the walk runs under, so a balance moving does not miss and
    # crossing that line does. No card-payment bill: no query.
    payees = [{"payee": r["payee"] or ""} for r in conn.execute(
        "SELECT DISTINCT payee FROM bills").fetchall()]
    kept = len(budget.drop_card_pay_bills(conn, payees, cfg=cfg))
    card_bills_dropped = kept != len(payees)
    try:
        cfg_s = json.dumps(cfg, sort_keys=True, default=str)
    except Exception:                              # noqa: BLE001
        cfg_s = repr(sorted(cfg))
    return hashlib.md5(
        f"{row['h']}|{ledger}|{shadows}|{card_bills_dropped}|{cfg_s}".encode(),
        usedforsecurity=False).hexdigest()


# tenant -> (ledger probe, the month stamps computed under it). The month
# stamps are a grouped join over every dated row; the probe is the set of
# transactions that wrote any dated row of the household, kept by a
# trigger on each dated table (db/migrations/218), one indexed read. Every
# committed write adds an id no earlier state of the set held, so while the
# probe is unchanged no dated row was inserted, updated or deleted, and the
# stamps computed last time are the ones a fresh pass would compute. A
# write and its undo are two moves, never a return: a row count and a sum
# of row versions, the probe before this one, came back to the values they
# had. One entry per household: a write replaces it. The probe reads under
# the household's row security, like the stamps.
_FIXED_STAMPS_MEMO: dict[str, tuple[str, dict[dt.date, str]]] = {}


def _fixed_month_probe(conn) -> str:
    """The transactions that last wrote the household's dated rows (the
    tables of _FIXED_DATED_TABLES, each carrying the trigger that records
    them). Moves on every committed write; reading writes nothing."""
    return conn.execute(
        "SELECT COALESCE(string_agg(xact::text, ',' ORDER BY xact), '') AS v"
        " FROM ledger_writes").fetchone()["v"]


def _fixed_month_stamps(conn) -> dict[dt.date, str]:
    """Month start -> a row count and sum of row versions over every row of
    _FIXED_DATED_TABLES dated in that month (a dependent row by its
    charge's date; a reimbursement in the months of both its charges), in
    one pass. Any insert, update or delete of such a row changes its
    month's stamp — a charge moved to another date changes both months.
    Served from _FIXED_STAMPS_MEMO while the ledger probe says no dated
    row was written."""
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    probe = _fixed_month_probe(conn)
    hit = _FIXED_STAMPS_MEMO.get(tid)
    if hit is not None and hit[0] == probe:
        return hit[1]
    stamps = _fixed_month_stamps_pass(conn)
    # the probe and the pass are two statements (two snapshots on an
    # autocommit connection): a write committed between them would file
    # stamps that include it under a probe that does not. Memo only when
    # the probe did not move across the pass; an undo of that write moves
    # it again rather than back.
    if _fixed_month_probe(conn) == probe:
        if len(_FIXED_STAMPS_MEMO) > 10_000:
            _FIXED_STAMPS_MEMO.clear()
        _FIXED_STAMPS_MEMO[tid] = (probe, stamps)
    return stamps


def _fixed_month_stamps_pass(conn) -> dict[dt.date, str]:
    """The month stamps of _fixed_month_stamps, computed."""
    arms = []
    for i, (table, cols) in enumerate(_FIXED_DATED_TABLES.items()):
        for j, col in enumerate(cols or (None,)):
            tag = f"{i}.{j}"
            if col is None:
                arms.append(
                    f"SELECT date_trunc('month', t.date)::date AS m, "
                    f"'{tag}:' || count(*) || ':' || "
                    f"sum(t.xmin::text::bigint) AS p FROM {table} t "
                    f"WHERE t.date IS NOT NULL GROUP BY 1")
            else:
                arms.append(
                    f"SELECT date_trunc('month', t.date)::date AS m, "
                    f"'{tag}:' || count(*) || ':' || "
                    f"sum(d.xmin::text::bigint) AS p FROM {table} d "
                    f"JOIN transactions t ON t.tenant_id = d.tenant_id "
                    f"AND t.id = d.{col} "
                    f"WHERE t.date IS NOT NULL GROUP BY 1")
    # A reimbursement's netting reads its OTHER leg's live state (removed,
    # amount): each leg's month also carries the other leg's row version,
    # so a deposit withdrawn in February re-walks January's charge too.
    for j, (col, other) in enumerate((("expense_id", "reimburse_id"),
                                      ("reimburse_id", "expense_id"))):
        arms.append(
            f"SELECT date_trunc('month', t.date)::date AS m, "
            f"'rx.{j}:' || count(*) || ':' || "
            f"sum(o.xmin::text::bigint) AS p FROM reimbursements d "
            f"JOIN transactions t ON t.tenant_id = d.tenant_id "
            f"AND t.id = d.{col} "
            f"JOIN transactions o ON o.tenant_id = d.tenant_id "
            f"AND o.id = d.{other} "
            f"WHERE t.date IS NOT NULL GROUP BY 1")
    rows = conn.execute(
        "SELECT m, md5(string_agg(p, '|' ORDER BY p)) AS h FROM ("
        + " UNION ALL ".join(arms) + ") x GROUP BY m").fetchall()
    return {r["m"]: r["h"] for r in rows}


def _fixed_month_digest(stamps: dict[dt.date, str], first: dt.date,
                        stop: dt.date) -> str:
    """The month stamps of every month from `first` to `stop` inclusive."""
    parts, cur = [], first.replace(day=1)
    while cur <= stop:
        parts.append(stamps.get(cur, ""))
        cur = (cur + dt.timedelta(days=32)).replace(day=1)
    return hashlib.md5("|".join(parts).encode(),
                       usedforsecurity=False).hexdigest()


def _fixed_month_reach(month: dt.date) -> dt.date:
    """The first month whose rows a month's fixed walk reads: the prepay
    match looks PREPAY_MATCH_DAYS before the month opens, and an annual
    envelope's pool reads the calendar year to date."""
    return min(month.replace(month=1),
               (month - dt.timedelta(days=budget.PREPAY_MATCH_DAYS))
               .replace(day=1))


def _fixed_month_stop(month: dt.date, has_annual: bool) -> dt.date:
    """The last month whose rows a month's fixed walk reads: a closed month
    is matched through the month after it plus the prepay window, and the annual
    envelopes' prior use is settled over the WHOLE calendar year, with
    occurrences reaching the January after (_envelope_year_claims): a row
    dated after the month can move its envelope figure."""
    # a closed month is matched through the month after plus the prepay
    # window (month_status closed=True), so those rows are its inputs too
    after = (month + dt.timedelta(days=32)).replace(day=1)
    horizon = ((after + dt.timedelta(days=32)).replace(day=1)
               + dt.timedelta(days=budget.PREPAY_MATCH_DAYS)).replace(day=1)
    if not has_annual:
        return horizon
    return max(horizon, (dt.date(month.year + 1, 1, 1)
                         + dt.timedelta(days=budget.PREPAY_MATCH_DAYS))
               .replace(day=1))


def _months_back(month_start: dt.date, n: int) -> dt.date:
    for _ in range(n):
        month_start = (month_start - dt.timedelta(days=1)).replace(day=1)
    return month_start


def _span_months(start: dt.date, last: dt.date) -> int:
    """Calendar months a window covers, first-of-month to first-of-month
    inclusive — the divisor behind every "/mo" pace the windowed tabs
    show. Counted from the CALENDAR, never from which months had a row:
    $300 a quarter over a year is $100/mo, and a divisor that shrinks to
    the four months with a deposit puts $300/mo on the Income headline.
    Every month the range label names counts whole ("Sep 2025–Aug 2026"
    is twelve); a range of months holds closed months only, and the one
    window that runs to today ("This month") is one month, not a fraction.

    `last` is the window's OWN last day, not the wall clock. "1m" closes
    before today (see _range_window); measured to today it would count the
    in-progress month too and halve the pace of the complete month it
    names."""
    return (last.year - start.year) * 12 + last.month - start.month + 1


def _window_kind(accounts: list[str] | None) -> str:
    """The MeasuredWindow anchor a window over `accounts` reads: None is
    the spend total (every account, cards included), a list the income
    total over the bank accounts (coverage.SPEND / coverage.INCOME)."""
    return coverage.SPEND if accounts is None else coverage.INCOME


def _ledger_anchor_dates(conn, accounts: list[str] | None,
                         ) -> tuple[dt.date | None, dt.date | None]:
    """(first row a window totals, first day the ledger is imported) for
    the window over `accounts` — read off the household's MeasuredWindow
    (engine/coverage.py), which holds the rule."""
    w = coverage.window(conn)
    kind = _window_kind(accounts)
    return w.first(kind), w.covered_from(kind)


def _pace_months(conn, accounts: list[str] | None, start: dt.date,
                 last: dt.date) -> int:
    # Never below one: a window that closes before the ledger even opens
    # (a brand-new household reading "1m") spans zero or fewer calendar
    # months, and a zero divisor is a crash or an infinity on the page.
    start = coverage.window(conn).pace_start(start, _window_kind(accounts))
    return max(1, _span_months(start, last))


def _window_label(start: dt.date, last: dt.date) -> str:
    if start.year == last.year:
        return (f"{_MON[start.month]}–{_MON[last.month]} {last.year}"
                if start.month != last.month
                else f"{_MON[last.month]} {last.year}")
    return f"{_MON[start.month]} {start.year}–{_MON[last.month]} {last.year}"


def _range_window(conn, today: dt.date, range_key: str,
                  accounts: list[str] | None,
                  ) -> tuple[dt.date, dt.date, dt.date | None, str, bool]:
    """(start, end, prev_start, label, live_month) for a site-wide range
    key. `end` is EXCLUSIVE: the day after today for "cur" (this month, the
    one window that runs to now), the first of this month for every other
    key — a range of months holds COMPLETE months only, so "3y" on the 6th
    of a month is the 36 months that have closed, not 35 and six days: a
    partial month read beside whole ones looks like a collapse every
    morning after a month turns, and a per-month pace over it is wrong.
    `prev_start` opens the previous window — the thing "vs the year
    before" compares against — and _prev_window_end cuts where it stops,
    like-for-like with this month's partial days; None for 'all', which
    has no previous window, and None when the ledger begins after the
    previous window opens — a comparison against days nobody imported
    is not a comparison.

    `accounts` are the accounts the window totals: None for spend, the
    bank accounts for income (_window_kind); 'all' starts at their first
    row.

    A ledger whose first row is in the month in progress has no complete
    month yet, so every range of months would be empty — a household
    reading Cash Flow in its first month would see nothing at all, while
    Today shows the month's spend. Those ranges fall back to the month so
    far (the "cur" window, with no previous one) and `live_month` says so,
    so the page can name the window it is showing."""
    months = FLOW_RANGES[range_key]      # KeyError = caller's bug; route validates
    month_start = today.replace(day=1)
    live = False
    # The first row the window totals opens 'all' (by its month) and
    # decides `live`; the first imported day decides whether the previous
    # window is covered. Both are the household's MeasuredWindow.
    w = coverage.window(conn)
    kind = _window_kind(accounts)
    first_own = w.first_month(kind)
    if range_key != "cur":
        # judged on the rows this window totals, the same anchor as its
        # start: older rows it never counts (plan history, loans) would
        # otherwise hide that it has no complete month yet
        live = first_own is not None and first_own >= month_start
    if range_key == "cur" or live:
        end = today + dt.timedelta(days=1)
        last = today
        start = month_start
        prev = None if live else _months_back(start, 1)
    else:
        # the last complete month closes where this month opens
        end = month_start
        last = month_start - dt.timedelta(days=1)
        if months is None:
            start = (min(first_own, last.replace(day=1))
                     if first_own else last.replace(day=1))
            prev = None
        else:
            start = _months_back(last.replace(day=1), months - 1)
            prev = _months_back(start, months)
    if prev is not None and not w.prev_covered(prev, kind):
        # The previous window is a comparison only where the ledger covers
        # all of it. One that opens before the first row would total the
        # months nobody imported as $0, so a household a year old reads its
        # whole spend as a huge rise vs the year before — and a ledger
        # whose first row falls mid-month has no days before it, so a
        # partial first month set against a whole one reads as a large
        # rise. Unknown is the honest answer, and the pages hide the delta on it. A first row
        # on the first business day still covers from the 1st
        # (coverage.covers).
        prev = None
    return start, end, prev, _window_label(start, last), live


def _cashflow_window(conn, today: dt.date, range_key: str,
                     accounts: list[str] | None,
                     ) -> tuple[dt.date, dt.date, dt.date | None, str, bool]:
    """The ONE window every Cash Flow tab reads — Overview, Spending and
    Income cover the same months (specs/counted-figures.md). It opens on
    whichever of the two ledgers (spend over every account, income over
    the bank accounts) begins first, so "All" starts at the same month on
    every tab and a household whose cards predate its bank feed sees the
    same range of months everywhere; the months the bank feed misses are
    marked (_incomplete_months), never cut. The one exception is the
    Spending tab's "All", which opens at the first charge it counts
    (_spending_span). `accounts` names the rows the
    CALLER totals, and decides only whether its previous window is a
    comparison: the earlier ledger covering it says nothing about the
    later one."""
    w = coverage.window(conn)
    sf = w.first_month(coverage.SPEND)
    inf = w.first_month(coverage.INCOME)
    anchor = (_bank_account_ids(conn)
              if inf is not None and (sf is None or inf < sf) else None)
    start, end, prev, label, live = _range_window(conn, today, range_key,
                                                  anchor)
    if prev is not None and not w.prev_covered(prev, _window_kind(accounts)):
        prev = None
    return start, end, prev, label, live


def ledger_ranges(conn, today: dt.date) -> dict[str, dict]:
    """The days each site-wide range key covers, by Cash Flow's own window
    (_cashflow_window): "cur" is this month to today, every other key is
    COMPLETE months ("3m" on Nov 17 is Aug 1 – Oct 31), and a ledger with
    no complete month yet reads the month so far for each, as Cash Flow
    does. `start` and `end` are inclusive ISO dates. "all" is no date
    bound at all ("" both): the Transactions page lists the whole ledger,
    this month included, where Cash Flow's "All" stops at the last
    complete month. The ONE definition a Transactions range button sends
    back as its date filter (specs/ledger-figures.md)."""
    out = {}
    for key in FLOW_RANGES:
        if key == "all":
            out[key] = {"start": "", "end": ""}
            continue
        start, end, _prev, _label, _live = _cashflow_window(
            conn, today, key, None)
        out[key] = {"start": start.isoformat(),
                    "end": (end - dt.timedelta(days=1)).isoformat()}
    return out


def _incomplete_months(conn, start: dt.date, last: dt.date) -> list[str]:
    """'YYYY-MM' of every month from `start` through `last` whose income
    the bank feed does not measure — Cash Flow's ONE answer to "is this
    month's income complete" (`_income_known`). Such a month is still
    counted in what came in and went out on every tab; it is flagged, and
    left out of saved and the savings rate, in each range's total, the
    year list and the month bars alike (specs/counted-figures.md)."""
    cov = _income_coverage(conn)
    out, cur = [], start.replace(day=1)
    while cur <= last:
        ym = cur.strftime("%Y-%m")
        if not _income_known(cov, ym):
            out.append(ym)
        cur = (cur + dt.timedelta(days=32)).replace(day=1)
    return out


# the words for a month, or a year holding one, whose income is incomplete
INCOME_INCOMPLETE = "income incomplete"


def income_incomplete_note(known: int, total: int) -> str | None:
    """The words under a year's left-over figure when some of its `total`
    months have incomplete income: left over and the rate count the
    `known` ones. None for a year measured in every month. One wording for Cash
    Flow's year list, the Year lens totals and the yearly email."""
    if known >= total:
        return None
    if not known:
        return f"{INCOME_INCOMPLETE} — no month has bank data"
    return f"{INCOME_INCOMPLETE} — left over counts {known} of {total} months"


def _prev_window_end(today: dt.date, range_key: str, start: dt.date,
                     prev: dt.date | None) -> dt.date | None:
    """EXCLUSIVE end of the previous window, cut like-for-like with the
    current one. A window that runs to today holds its last month only up
    to today, so the previous window stops the same number of days into
    its own last month: on the 3rd, "this month" (three days) compares
    with the first three days of last month, not with all of it — a whole
    month set against a partial one reads as a collapse every morning
    after a month turns. On the last day of a month the current month is
    whole, so the previous one is too (Feb 28 against all of January).
    Every other key is whole months on both sides, so the previous window
    simply stops where this one starts; 'all' has no previous window."""
    if prev is None or range_key != "cur":
        return start if prev is not None else None
    months = FLOW_RANGES[range_key]
    pm = _months_back(today.replace(day=1), months)
    pm_days = calendar.monthrange(pm.year, pm.month)[1]
    today_is_month_end = (today + dt.timedelta(days=1)).day == 1
    day = pm_days if today_is_month_end else min(today.day, pm_days)
    return min(start, pm.replace(day=day) + dt.timedelta(days=1))


# how the page names the window a comparison is against, per range key
_PREV_NAME = {"1m": "the month before that", "3m": "the 3 months before",
              "6m": "the 6 months before", "1y": "the year before",
              "3y": "the 3 years before", "5y": "the 5 years before"}


def _day_span(a: dt.date, b: dt.date) -> str:
    """"Nov 1–17", "Nov 1" — the days a window holds, `b` inclusive."""
    if a == b:
        return f"{_MON[a.month]} {a.day}"
    if (a.year, a.month) == (b.year, b.month):
        return f"{_MON[a.month]} {a.day}–{b.day}"
    return f"{_MON[a.month]} {a.day}–{_MON[b.month]} {b.day}"


def window_words(range_key: str, start: dt.date, last: dt.date,
                 prev: dt.date | None, prev_end: dt.date | None,
                 live: bool) -> dict:
    """The words a windowed Cash Flow tab prints around its figures,
    decided once for the Overview, Spending and Income tabs:

    so_far     the window runs to today ("This month", or a ledger still in
               its first month): the figures are the month so far, and no
               per-month pace is printed beside them
    heading    "Nov 1–17 so far" for such a window, else the range label
    prev_text  what "the window before" is: "vs Oct 1–17" for the month so
               far (the same days of last month, _prev_window_end), "vs the
               year before" for a range of whole months; null without a
               previous window"""
    so_far = range_key == "cur" or live
    heading = (f"{_day_span(start, last)} so far" if so_far
               else _window_label(start, last))
    prev_text = None
    if prev is not None and prev_end is not None:
        prev_text = ("vs " + _day_span(prev, prev_end - dt.timedelta(days=1))
                     if range_key == "cur"
                     else f"vs {_PREV_NAME.get(range_key, 'the window before')}")
    return {"so_far": so_far, "heading": heading, "prev_text": prev_text}


def income_still_due(conn, today: dt.date, so_far: bool, *,
                     memo: dict | None = None) -> dict | None:
    """The paychecks still due this month beside a month-so-far window
    (budget.expected_income_remaining, Today's and Budget's "still due"),
    as `{amount, text}`; null for a window of closed months, a household
    with no income series, or nothing left to come."""
    if not so_far:
        return None
    try:
        amt = budget.expected_income_remaining(
            conn, budget.load_config(conn, memo=memo), today, memo=memo)
    except Exception:          # noqa: BLE001 — a words line never fails the tab
        log.exception("income still due failed")
        return None
    amt = round(float(amt or 0), 2)
    if amt <= 0:
        return None
    return {"amount": amt,
            "text": f"{_money_words(amt)} in paychecks still due this month"}


def _money_words(v: float) -> str:
    return f"${whole_dollars(v):,}"


def dropped_words(dropped: list, prev_text: str | None) -> str | None:
    """The sentence under the Income headline when payers of the previous
    window paid nothing in this one — the reason the change beside the
    total fell (specs/shown-figures.md, "Income tab: a payer who stopped").
    `dropped` is every such payer, in the shape of income_window's
    `dropped_sources` rows ([payer, 0, 0, previous amount, logo], largest
    first) — the whole list, not the ten that reply keeps; three are
    named, the rest counted. None when no payer dropped out or there is no previous
    window to have paid in."""
    if not dropped or not prev_text:
        return None
    named = [f"{r[0]} ({_money_words(r[3])})" for r in dropped[:3]]
    rest = len(dropped) - len(named)
    if rest:
        named.append(f"{rest} other{'' if rest == 1 else 's'}")
    who = (named[0] if len(named) == 1
           else ", ".join(named[:-1]) + " or " + named[-1])
    when = prev_text[3:] if prev_text.startswith("vs ") else prev_text
    return f"Nothing this time from {who}, paid {when}."


def income_payer_rows(conn):
    """Every income row of the household's bank accounts (_INCOME_WHERE,
    the excluded accounts left out, the whole history) with its payer —
    the display merchant — for paychecks.payer_verdicts."""
    excl, ex_p = excluded_accounts_sql(conn)
    return conn.execute(
        f"""SELECT {DISPLAY_MERCHANT} payee, t.name, t.date,
                   COALESCE(t.category_detailed,'') AS d,
                   {INCOME_NET} AS amt
             FROM transactions t {MC_JOIN}
            WHERE {_INCOME_WHERE} {excl}""",
        (_bank_account_ids(conn), *ex_p)).fetchall()


def _mkey(name: str) -> str:
    # the income-by-source fold is the paycheck payer key, so a source row's
    # key is the key its verdict is filed under
    return _paychecks.payer_key(name)


def _fold_merchants(rows, limit: int | None) -> list[tuple[str, dict]]:
    """Display names that differ only in punctuation/case are one merchant
    the canonical map hasn't caught up with — over a wide window "A-B-C"
    and "A B C" both survive their own eras and rank as two rows. Fold by
    the letters alone; the bigger variant keeps the name and logo."""
    folded: dict[str, dict] = {}
    for r in rows:
        k = _mkey(r["payee"])
        f = folded.get(k)
        if f is None:
            folded[k] = {"payee": r["payee"], "amt": float(r["amt"] or 0),
                         "n": r["n"], "logo": r["logo"],
                         "names": [r["payee"]]}
        else:
            f["names"].append(r["payee"])
            if float(r["amt"] or 0) > f["amt"] and r["payee"]:
                f["payee"], f["logo"] = r["payee"], r["logo"] or f["logo"]
            f["amt"] += float(r["amt"] or 0)
            f["n"] += r["n"]
            f["logo"] = f["logo"] or r["logo"]
    out = sorted(folded.items(), key=lambda x: -x[1]["amt"])
    return out[:limit] if limit else out


def _spend_scope(conn, a: dt.date, b: dt.date) -> tuple[str, tuple]:
    """The rows the Spending tab counts between `a` and `b` (exclusive), as
    a (WHERE fragment, params) pair over `transactions t`: the spend
    predicate, the excluded accounts and the dates. Every figure of the
    tab and the drill that lists its rows read this one fragment, so a
    change to what counts moves both together (specs/spending-drill.md)."""
    excl, ex_p = excluded_accounts_sql(conn)
    return (f"{SPEND_WHERE} AND t.date >= %s AND t.date < %s {excl}",
            (a, b, *ex_p))


def _spend_cats(conn, where: str, params: tuple) -> dict[str, float]:
    """Spend by category over a scope: a hand-split row counts under each
    of its parts, for its share, reimbursements netted."""
    pamt2 = _R2.format(f"SUM({PART_NET_JOINED})")
    return {r["cat"]: float(r["amt"] or 0) for r in conn.execute(
        f"SELECT {PART_CAT} cat, {pamt2} amt FROM transactions t "
        f"{REIMB_PARTIAL_JOIN} {SPLIT_JOIN} WHERE {where} GROUP BY cat",
        params)}


def _spend_cat_keys(conn, where: str, params: tuple) -> dict[str, str]:
    """For each category `_spend_cats` sums (the spaced form, which folds
    'RENT_AND_UTILITIES' and 'RENT AND UTILITIES' together), the value the
    ledger STORES for it — the one carrying the most of its spend — so the
    tab can link a category with a filter the Transactions page and its
    category list know. "?" is a row with no category, as the search's
    own filter spells it."""
    keys: dict[str, tuple[float, str]] = {}
    for r in conn.execute(
            f"SELECT {PART_CAT} cat, COALESCE({_categories.PART_CAT}, '?') k,"
            f" SUM({PART_NET_JOINED}) amt FROM transactions t "
            f"{REIMB_PARTIAL_JOIN} {SPLIT_JOIN} WHERE {where} "
            f"GROUP BY cat, k ORDER BY k", params):
        amt = float(r["amt"] or 0)
        if r["cat"] not in keys or amt > keys[r["cat"]][0]:
            keys[r["cat"]] = (amt, r["k"])
    return {c: k for c, (_a, k) in keys.items()}


def _spend_merchants(conn, where: str, params: tuple
                     ) -> list[tuple[str, dict]]:
    """Spend by merchant over a scope, folded by `_fold_merchants`; each
    folded entry also carries the display names it folded (`names`)."""
    amt2 = _R2.format(f"SUM({NET_AMOUNT_JOINED})")
    rows = conn.execute(
        f"SELECT {DISPLAY_MERCHANT} payee, {amt2} amt, COUNT(*) n, "
        f"       max({MERCHANT_LOGO}) logo "
        f"FROM transactions t {MC_JOIN} {REIMB_PARTIAL_JOIN} WHERE {where} "
        f"GROUP BY payee ORDER BY amt DESC", params).fetchall()
    return _fold_merchants(rows, None)


def _spending_span(conn, today: dt.date, range_key: str,
                   ) -> tuple[dt.date, dt.date, dt.date | None, str, bool]:
    """The Spending tab's window: Cash Flow's (_cashflow_window), except
    that "All" opens where its `/mo` divides from — the month of the first
    row a spend window totals (MeasuredWindow.pace_start, the rule
    _pace_months reads) — so its heading, total and per-month figure
    describe one span (specs/counted-figures.md, "Spending → All starts at
    the first counted charge"). Every other range, and every other tab,
    keeps the shared window."""
    start, end, prev, label, live = _cashflow_window(conn, today, range_key,
                                                     None)
    if range_key == "all" and not live:
        opens = coverage.window(conn).pace_start(start, coverage.SPEND)
        if opens != start and opens < end:
            start = opens
            label = _window_label(start, end - dt.timedelta(days=1))
    return start, end, prev, label, live


def _spending_window_scope(conn, today: dt.date, range_key: str,
                           side: str = "current"):
    """One side of the Spending tab's window: (start, end exclusive, WHERE,
    params, live) for "current" or "previous" — the previous equal window
    `spending_window` compares against. None for "previous" when the range
    has none (see _cashflow_window). The window itself is the tab's
    (_spending_span): spend is summed over every account, cards
    included."""
    start, end, prev, _label, live = _spending_span(conn, today, range_key)
    if side == "previous":
        if prev is None:
            return None
        start, end = prev, _prev_window_end(today, range_key, start, prev)
    where, params = _spend_scope(conn, start, end)
    return start, end, where, params, live


SPEND_DRILL_SIDES = ("current", "previous")


def spending_window_ledger(conn, today: dt.date, range_key: str, *,
                           category: str | None = None,
                           merchant: str | None = None,
                           side: str = "current") -> dict:
    """The rows behind ONE of the Spending tab's figures, and that figure:
    a category's total (`category`, the stored key the tab links with,
    or the reports' spaced form an older client sends — either is read
    through the spaced form the figure summed by), a merchant's (`merchant`, folded by the same payer key
    the tab folds names with), or with neither the window's whole spend.
    `side="previous"` reads the previous equal window — a dropped
    merchant's figure. Returns {txn_ids, amount, label, from, to}: `amount`
    is computed by the same queries as `spending_window`, so it is the
    figure the row printed, `label` the words the chip over the rows
    prints, and `by_day` the figure's amount on each day it has one. ValueError for an unknown range or side, or a previous side the
    range does not have (specs/spending-drill.md)."""
    if range_key not in FLOW_RANGES:
        raise ValueError("unknown range")
    if side not in SPEND_DRILL_SIDES:
        raise ValueError("side must be current or previous")
    if category and merchant:
        raise ValueError("a figure is a category or a merchant, not both")
    scope = _spending_window_scope(conn, today, range_key, side)
    if scope is None:
        raise ValueError("this range has no previous window")
    start, end, where, params, live = scope
    last_day = end - dt.timedelta(days=1)
    # each day's share of the figure, by the figure's own amounts (a
    # category counts a split charge for its part, reimbursements netted):
    # what the list's day dividers and per-visit average are read from
    def _days(joins: str, amount: str, more: str, more_p: tuple) -> dict:
        return {r["d"].isoformat(): float(r["amt"] or 0)
                for r in conn.execute(
                    f"SELECT t.date d, {_R2.format(f'SUM({amount})')} amt "
                    f"FROM transactions t {joins} WHERE {where} {more} "
                    f"GROUP BY t.date", (*params, *more_p))}
    if category:
        subject = _categories.display_label(category)
        # the figure folds a key's underscored and spaced spellings into
        # one category (PART_CAT), so the rows behind it are matched the
        # same way, whichever spelling the link carries
        category = category.replace("_", " ")
        amount = round(_spend_cats(conn, where, params).get(category, 0.0), 2)
        ids = [r["id"] for r in conn.execute(
            f"SELECT DISTINCT t.id FROM transactions t {SPLIT_JOIN} "
            f"WHERE {where} AND {PART_CAT} = %s", (*params, category))]
        by_day = _days(f"{REIMB_PARTIAL_JOIN} {SPLIT_JOIN}", PART_NET_JOINED,
                       f"AND {PART_CAT} = %s", (category,))
    elif merchant:
        key = _mkey(merchant)
        hit = next((f for k, f in _spend_merchants(conn, where, params)
                    if k == key), None)
        subject = hit["payee"] if hit else merchant
        amount = round(hit["amt"], 2) if hit else 0.0
        names = [n for n in (hit["names"] if hit else []) if n is not None]
        has_null = bool(hit) and None in hit["names"]
        match = (f"AND ({DISPLAY_MERCHANT} = ANY(%s)"
                 + (f" OR {DISPLAY_MERCHANT} IS NULL" if has_null else "")
                 + ")")
        ids = [r["id"] for r in conn.execute(
            f"SELECT t.id FROM transactions t {MC_JOIN} WHERE {where} "
            f"{match}", (*params, names))] if hit else []
        by_day = (_days(f"{MC_JOIN} {REIMB_PARTIAL_JOIN}", NET_AMOUNT_JOINED,
                        match, (names,)) if hit else {})
    else:
        subject = "All spending"
        # the headline is the sum of the category figures, as on the tab
        amount = round(sum(_spend_cats(conn, where, params).values()), 2)
        ids = [r["id"] for r in conn.execute(
            f"SELECT t.id FROM transactions t WHERE {where}", params)]
        by_day = _days(REIMB_PARTIAL_JOIN, NET_AMOUNT_JOINED, "", ())
    # the days the rows were read over, in the tab's own heading words
    # ("Nov 1–17 so far", "Dec 2025–Nov 2026"); a previous side says so
    span = (window_words(range_key, start, last_day, None, None,
                         live)["heading"] if side == "current"
            else (_day_span(start, last_day) if range_key == "cur"
                  else _window_label(start, last_day))
            + ", the window before")
    label = f"{subject} · {span}"
    return {"txn_ids": ids, "amount": amount, "label": label,
            "from": start.isoformat(), "to": last_day.isoformat(),
            "by_day": {d: v for d, v in by_day.items() if v}}


def per_month_basis(months: int, span: int,
                    over: str = "with spending") -> str | None:
    """What a windowed tab's "/mo" divides by, in words, when that is
    fewer months than the heading names: a range longer than the rows it
    totals keeps its window and its figures, and the pace is over the
    months from where those rows begin (_pace_months), so "$250/mo" under
    a 36-month heading reads "over the 20 months with spending" (Spending)
    or "over the 18 months since the bank feed began" (Income, whose
    divisor starts at the bank feed's first row). None when the divisor is
    the heading's own months (specs/counted-figures.md)."""
    if months >= span:
        return None
    return f"over the {months} month{'s' if months != 1 else ''} {over}"


def _window_basis(range_key: str, live: bool, months: int, start: dt.date,
                  last_day: dt.date, over: str) -> str | None:
    """per_month_basis for a window: none beside the month so far, which
    prints no pace."""
    if live or range_key == "cur":
        return None
    return per_month_basis(months, _span_months(start, last_day), over)


def spending_window(conn, today: dt.date, range_key: str) -> dict:
    """The Spending tab's windowed view: total, monthly pace, categories and
    merchants ranked over one site-wide timeframe, each with its total over
    the PREVIOUS equal window so the page can lead with what changed. Pure
    SQL aggregation — no month walk — so every range is cheap. Each figure's
    rows are `spending_window_ledger`'s, read over the same scope."""
    # spend is summed over every account, cards included, so the window
    # is anchored on the first row of any account, not of checking; "All"
    # opens at the first charge it counts (_spending_span)
    start, end, prev, label, live = _spending_span(conn, today, range_key)
    # the last day the window covers — today for "cur" (and for a ledger
    # still in its first month), the end of the last complete month for
    # every other key. Both the reported end and the "/mo" divisor are the
    # WINDOW's, never the wall clock's (flow_breakdown reads the same day
    # off `end`).
    last_day = end - dt.timedelta(days=1)
    amt2 = _R2.format(f"SUM({NET_AMOUNT_JOINED})")

    prev_end = _prev_window_end(today, range_key, start, prev)
    cur_where, cur_p = _spend_scope(conn, start, end)
    prev_scope = _spend_scope(conn, prev, prev_end) if prev else None
    cats = _spend_cats(conn, cur_where, cur_p)
    prev_cats = _spend_cats(conn, *prev_scope) if prev else {}
    by_month = [[r["ym"], float(r["amt"] or 0)] for r in conn.execute(
        f"SELECT to_char(t.date,'YYYY-MM') ym, {amt2} amt FROM transactions t "
        f"{REIMB_PARTIAL_JOIN} WHERE {cur_where} GROUP BY ym ORDER BY ym",
        cur_p)]

    merch_all = _spend_merchants(conn, cur_where, cur_p)
    merch = _listed(merch_all)[:25]
    prev_folded = _spend_merchants(conn, *prev_scope) if prev else []
    prev_merch = {k: round(v["amt"], 2) for k, v in prev_folded}
    # Spend that stopped is a change too, and often the largest: the
    # headline delta already counts it, so a row list without it cannot
    # add up to the headline. Prev-only categories follow the ranked
    # current ones at 0; prev-only merchants get their own list, so the
    # ranked merchant list stays a list of where money went this window.
    current_keys = {k for k, _f in merch_all}
    dropped_merch = [(k, f) for k, f in prev_folded
                     if k not in current_keys and f["amt"] > 0][:10]
    total = round(sum(cats.values()), 2)
    prev_total = round(sum(prev_cats.values()), 2) if prev else None
    ranked = sorted(cats.items(), key=lambda x: -x[1])
    gone = [(c, v) for c, v in sorted(prev_cats.items(), key=lambda x: -x[1])
            if c not in cats and round(v, 2) > 0]
    months = _pace_months(conn, None, start, last_day)
    # each category is NAMED by the value the ledger stores for it (the
    # clients label it), so a link built from the row is a filter the
    # Transactions page can keep while it is narrowed
    keys = {**(_spend_cat_keys(conn, *prev_scope) if prev else {}),
            **_spend_cat_keys(conn, cur_where, cur_p)}
    categories = ([[c, round(v, 2),
                    round(prev_cats[c], 2) if prev and c in prev_cats
                    else (0.0 if prev else None)]
                   for c, v in ranked]
                  + [[c, 0.0, round(v, 2)] for c, v in gone])
    categories = [[keys.get(r[0], r[0]), *r[1:]] for r in categories]
    merchants = [[f["payee"], round(f["amt"], 2), f["n"],
                  (prev_merch.get(k, 0.0) if prev else None),
                  f["logo"]] for k, f in merch]
    dropped = [[f["payee"], 0.0, 0, round(f["amt"], 2), f["logo"]]
               for _k, f in dropped_merch]
    return {
        "from": start.isoformat(), "to": last_day.isoformat(), "label": label,
        "months": months,
        # true when the ledger has no complete month yet and the range shows
        # the month so far instead (see _range_window)
        "live_month": live,
        # so_far / heading / prev_text (window_words)
        **window_words(range_key, start, last_day, prev, prev_end, live),
        "total": total, "prev_total": prev_total,
        # the window's spend a month over the months it measures, and each
        # category's in the order of `categories` — the "/mo" the tab
        # prints, decided here so no client divides
        "per_month": round(total / max(months, 1), 2),
        # what that "/mo" divides by, when fewer months than the heading
        # names (per_month_basis); null otherwise
        "per_month_basis": _window_basis(range_key, live, months, start,
                                         last_day, "with spending"),
        "per_month_by_category": [round(r[1] / max(months, 1), 2)
                                  for r in categories],
        # the window against the previous one, and each category's and
        # listed merchant's in row order (`change`; null where the range
        # has no previous window)
        "change": change(total, prev_total),
        "by_month": by_month,
        # months of the window whose income the bank feed misses — the
        # same list on every Cash Flow tab, for the same window
        "incomplete_months": _incomplete_months(conn, start, last_day),
        # spending is dated by the day it was paid (the lenses date a bill
        # by the month it belongs to)
        "dating": "paid",
        # [category, amount, previous-window amount|null], ranked
        "categories": categories,
        "category_changes": [change(r[1], r[2]) for r in categories],
        # each category's whole-percent share of the window's spend, in the
        # order of `categories`, adding up to 100 across every category
        # spent on (specs/shown-figures.md); null for one spent on only
        # before, or that refunds took to nothing
        "category_shares": shown_shares([round(v, 2) for _c, v in ranked],
                                        total) + [None] * len(gone),
        # [payee, amount, visits, previous-window amount|null, logo] —
        # prev looked up by the same fold key, so a renamed variant still
        # compares against its own history
        "merchants": merchants,
        "merchant_changes": [change(r[1], r[3]) for r in merchants],
        # same shape, for merchants paid only in the previous window
        "dropped_merchants": dropped,
        # what "What changed" prints, in order; empty with no previous
        # window
        "movers": (_movers(total, categories, dropped)
                   if prev_total is not None else []),
    }


_MONTH_WORD = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september"
    r"|october|november|december)\b", re.I)


def _month_free_key(name: str | None) -> str:
    """A payer's key with its month names left out: the line a bank
    re-labels every month ("March interest paid", "April interest
    paid") is one payer."""
    return _mkey(_MONTH_WORD.sub(" ", name or ""))


def income_window(conn, today: dt.date, range_key: str) -> dict:
    """The Income tab's windowed view: total in, the kind split (paychecks /
    interest & dividends / other — the flow picture's classifier), the
    monthly series, and the previous equal window's figures for the delta.
    Pure SQL plus a per-row classify pass — cheap at any range."""
    bank = _bank_account_ids(conn)
    # The window opens, and its pace divides, from the income anchor of the
    # household's MeasuredWindow, which leaves out a brokerage's cash
    # account: its rows are summed when stamped INCOME, but one that only
    # ever received sweeps would otherwise open "All" years before the
    # first paycheck and spread the income over months nobody measured.
    start, end, prev, label, live = _cashflow_window(conn, today, range_key,
                                                     bank)
    # the window's own last day, as on the Spending tab — "1m" ends where
    # last month did, so its pace divides by that one month
    last_day = end - dt.timedelta(days=1)
    # which payers are paychecks: the one verdict (specs/paychecks.md)
    verdicts = _paychecks.payer_verdicts(conn)
    excl, ex_p = excluded_accounts_sql(conn)

    def _kinds(a: dt.date, b: dt.date) -> dict[str, float]:
        out = {"paychecks": 0.0, "interest": 0.0, "other": 0.0}
        for r in conn.execute(
                f"""SELECT {DISPLAY_MERCHANT} payee, t.name,
                          COALESCE(t.category_detailed,'') AS d,
                          {INCOME_NET} AS amt
                     FROM transactions t {MC_JOIN}
                    WHERE {_INCOME_WHERE} AND t.date >= %s AND t.date < %s
                    {excl}""",
                (bank, a, b, *ex_p)):
            out[verdicts.kind(r["payee"], r["name"], r["d"])] += float(
                r["amt"] or 0)
        return {k: round(v, 2) for k, v in out.items()}

    prev_end = _prev_window_end(today, range_key, start, prev)
    kinds = _kinds(start, end)
    prev_kinds = _kinds(prev, prev_end) if prev else None
    by_month = [[r["ym"], round(float(r["amt"] or 0), 2)] for r in conn.execute(
        f"""SELECT to_char(t.date,'YYYY-MM') ym,
                   SUM({INCOME_NET}) amt FROM transactions t
            WHERE {_INCOME_WHERE} AND t.date >= %s AND t.date < %s {excl}
            GROUP BY ym ORDER BY ym""", (bank, start, end, *ex_p))]

    # WHO paid — the window's income grouped by payer, punctuation-folded
    # like the Spending tab's merchants, each with its previous-window
    # figure. "By source" names the employer, the bank paying interest,
    # a person paying you back through a payment app — not just the three
    # kinds.
    def _sources(a: dt.date, b: dt.date, limit: int | None):
        rows = conn.execute(
            f"""SELECT {DISPLAY_MERCHANT} payee,
                       {_R2.format(f'SUM({INCOME_NET})')} amt, COUNT(*) n,
                       max({MERCHANT_LOGO}) logo
                 FROM transactions t {MC_JOIN}
                WHERE {_INCOME_WHERE} AND t.date >= %s AND t.date < %s {excl}
                GROUP BY payee ORDER BY amt DESC""",
            (bank, a, b, *ex_p)).fetchall()
        return _fold_merchants(rows, limit)

    src_all = _sources(start, end, None)
    src = _listed(src_all)[:15]
    prev_folded = _sources(prev, prev_end, None) if prev else []
    prev_src = {k: round(v["amt"], 2) for k, v in prev_folded}
    # a payer who stopped paying is a change, as on the Spending tab — but
    # not one whose amount prints as $0, nor a line that only renamed its
    # month (a bank's "March interest …" is paid again as "April …")
    current_keys = {k for k, _f in src_all}
    current_months = {_month_free_key(n) for _k, f in src_all
                      for n in f["names"]}
    # every one of them: the sentence counts them all, the list keeps ten
    dropped_src = [(k, f) for k, f in prev_folded
                   if k not in current_keys and whole_dollars(f["amt"]) >= 1
                   and not any((m := _month_free_key(n)) and m in current_months
                               for n in f["names"])]
    months = _pace_months(conn, bank, start, last_day)
    total = round(sum(kinds.values()), 2)
    prev_total = (round(sum(prev_kinds.values()), 2)
                  if prev_kinds is not None else None)
    sources = [[f["payee"], round(f["amt"], 2), f["n"],
                (prev_src.get(k, 0.0) if prev else None),
                f["logo"]] for k, f in src]
    words = window_words(range_key, start, last_day, prev, prev_end, live)
    share_of = dict(zip((k for k, _f in src_all),
                        shown_shares([round(f["amt"], 2) for _k, f in src_all],
                                     total)))
    return {
        "from": start.isoformat(), "to": last_day.isoformat(), "label": label,
        "months": months,
        "live_month": live,
        **words,
        # the paychecks still to come this month, beside the month so far
        "income_due": income_still_due(conn, today, words["so_far"]),
        "incomplete_months": _incomplete_months(conn, start, last_day),
        "total": total,
        # the window's income a month, over the months income is measured
        # in (`months`), and the paychecks' share of it in whole percent —
        # the figures the tab prints, decided here so no client divides
        "per_month": round(total / months, 2),
        # what that "/mo" divides by, when fewer months than the heading
        # names (per_month_basis); null otherwise
        "per_month_basis": _window_basis(range_key, live, months, start,
                                         last_day,
                                         "since the bank feed began"),
        "paycheck_share": (round(100 * kinds["paychecks"] / total)
                           if total else None),
        "prev_total": prev_total,
        "kinds": kinds, "prev_kinds": prev_kinds,
        # each kind as the legend prints it: the Overview picture's own
        # dollars for the same window
        "kinds_shown": income_kinds_shown(kinds, total),
        # the window against the previous one, each kind's, and each
        # listed payer's in the order of `sources` (`change`; null where
        # the range has no previous window)
        "change": change(total, prev_total),
        "kind_changes": ({k: change(kinds[k], prev_kinds[k]) for k in kinds}
                         if prev_kinds is not None else None),
        "by_month": by_month,
        # [payer, amount, deposits, previous-window amount|null, logo]
        "sources": sources,
        "source_changes": [change(r[1], r[3]) for r in sources],
        # each listed payer's whole-percent share of the window's income,
        # in the order of `sources`: decided over EVERY payer, so the
        # shares add up to 100 across them all (only the top fifteen are
        # listed)
        "source_shares": [share_of[k] for k, _f in src],
        # same shape, for payers seen only in the previous window
        "dropped_sources": [[f["payee"], 0.0, 0, round(f["amt"], 2),
                             f["logo"]] for _k, f in dropped_src[:10]],
        # those payers in words, printed under the headline beside the
        # change they explain (null when none dropped out)
        "dropped_text": dropped_words(
            [[f["payee"], 0.0, 0, round(f["amt"], 2), f["logo"]]
             for _k, f in dropped_src], words.get("prev_text")),
        # in the order of `sources`: each payer's key, whether its deposits
        # count as paychecks and the household's word on it — what the
        # row's paycheck control shows and writes back
        "source_paychecks": [verdicts.source(f["payee"]) for _k, f in src],
    }


def flow_breakdown(conn, today: dt.date, range_key: str) -> dict:
    """Where the money came from and where it went, for one timeframe from
    FLOW_RANGES (the site-wide 3m/6m/1y/3y/5y/all vocabulary). Computed per
    range on demand — not bundled into the report — because the fixed split
    walks month_status month by month, and 'all' over a decade of history
    is a couple of seconds nobody should pay just to see the default view.
    Sources are income split by kind (paychecks, interest and dividends,
    everything else); sinks are spend split into FIXED (rows a recurring
    bill matched) and VARIABLE (the rest), with what is left as saved.
    Same predicates as every other figure on the page (_INCOME_WHERE /
    SPEND_WHERE), so the picture and the table agree.

    Read under ONE snapshot (a read-only REPEATABLE READ transaction) when
    the connection is not already in a transaction: the closed months and
    runs it files in the process cache are then exactly the state their
    stamps describe. Read statement by statement, a write committed and
    undone while the walk runs is seen by the dated tables' probe (it moves
    twice), but the tenant-wide stamp's row counts and row-version sums
    (the accounts, the connections' other tables) come back to where they
    were, so only the snapshot tells that the walk read the write."""
    # One memo for the whole call: bills, config and envelope priors load
    # once. The config loads before the snapshot opens: a first-run budget
    # seed is a write, and inside the snapshot nothing may write.
    memo: dict = {}
    budget.load_config(conn, memo=memo)
    if conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
        return _flow_breakdown(conn, today, range_key, memo)
    try:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, "
                         "READ ONLY")
            return _flow_breakdown(conn, today, range_key, memo)
    except psycopg.errors.ReadOnlySqlTransaction:
        # a reader that writes is a bug to find, not a page to fail: read
        # again statement by statement, under the probe guards below
        log.exception("cash-flow picture wrote inside its snapshot")
        return _flow_breakdown(conn, today, range_key, memo)


def _flow_breakdown(conn, today: dt.date, range_key: str, memo: dict) -> dict:
    """flow_breakdown's body, on whatever snapshot its caller holds."""
    bank = _bank_account_ids(conn)
    # the picture totals spend over every account, so 'all' starts at the
    # ledger's first row of any kind
    start, end, _prev, label, live = _cashflow_window(conn, today, range_key,
                                                      None)
    # the last day the window covers — today for "cur", the end of the
    # last complete month for every other key
    last_day = end - dt.timedelta(days=1)
    # The window is the one every Cash Flow tab reads (_cashflow_window),
    # summed whole on both sides. A month the bank feed does not measure is
    # still counted — its deposits as they are, its spend in full — and
    # named in `incomplete_months`, so the page marks it rather than
    # leaving it out (specs/counted-figures.md). Only a window with no
    # measured month at all is income_unknown: $0 came in would read all
    # its spend as a loss.
    mw = coverage.window(conn)
    incomplete = _incomplete_months(conn, start, last_day)
    months_total = _span_months(start, last_day)
    income_unknown = len(incomplete) == months_total
    # in and out are summed over every month; saved and the rate only over
    # the months the bank feed measures, so a month with spend and no feed
    # never reads as a loss. Runs of consecutive months alike, tagged.
    gaps = set(incomplete)
    runs: list[tuple[dt.date, dt.date, bool]] = []
    cur = start
    while cur < end:
        nxt = min((cur + dt.timedelta(days=32)).replace(day=1), end)
        known = cur.strftime("%Y-%m") not in gaps
        if runs and runs[-1][2] == known and runs[-1][1] == cur:
            runs[-1] = (runs[-1][0], nxt, known)
        else:
            runs.append((cur, nxt, known))
        cur = nxt
    periods = {range_key: (start, label)}

    # Fixed is what the budget engine counts as fixed for each month —
    # the charges its recurring bills matched (envelope use included) —
    # so the picture agrees with the Budget page and the daily verdict.
    # A closed month is matched exactly as the Month lens matches it: under
    # the budget and bill schedule frozen in its snapshot when it has one,
    # else against TODAY's live bills (never `historical`, which drops the
    # bills altogether: a charge is "fixed" because a bill claims it).
    # Read any other way, a bill deleted since would make that month's
    # rent "variable" here while the Month lens still calls it fixed.
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    # `memo` is flow_breakdown's, one for the whole call: a 5y window costs
    # seconds, not tens of seconds — and the config read here is the one
    # month_status reads below, so the spend total and the fixed split
    # cannot be judged under two configs.
    # The budget's excluded_accounts (the Accounts page 'excl' toggle)
    # apply to the spend TOTAL exactly as month_status applies them to the
    # fixed side; otherwise a bill paid from an excluded card is invisible
    # to the fixed match yet still counted in total_out, and its dollars
    # surface as "variable" on a picture that promises to agree with the
    # Budget page. Same helper as every other report in this module — the
    # picture and the Spending tab must not exclude different ledgers.
    cfg = budget.load_config(conn, memo=memo)
    excl_sql, ex_p = excluded_accounts_sql(conn, memo=memo)

    stamp = _fixed_inputs_stamp(conn, cfg)
    # what this request learns is filed only if nothing it reads moved
    # between the stamps and the end of the walk. Read BEFORE the stamps:
    # read after, a write landing between the two would be in the probe
    # and the walk but not in the stamps the walk's months are filed
    # under. A write AND its undo inside that span move the probe twice.
    probe_before = _fixed_month_probe(conn)
    # the dated rows, stamped per month: a closed month's entry is good
    # while the months its walk reads are unchanged
    month_stamps = _fixed_month_stamps(conn)
    # which payers are paychecks: the one verdict (specs/paychecks.md); its
    # fingerprint keys the cached runs, since a payer turning into one moves
    # the kinds of every run holding its deposits. The verdict reads every
    # deposit, so it is kept under the same stamps as the closed runs: a
    # repeat visit with nothing written reads no ledger row.
    verdicts = _cached_verdicts(conn, tid, stamp, month_stamps, memo)
    pending: list[tuple] = []
    has_annual = any(e.get("period_months", 1) > 1
                     for e in budget._envelope_bills(conn, memo=memo))
    # which closed months froze a budget: one read, so a month without a
    # snapshot costs no lookups (budget_snapshots is in `stamp`)
    snapped = {(r["year"], r["month"]) for r in conn.execute(
        "SELECT year, month FROM budget_snapshots").fetchall()}
    # a month judged under a frozen schedule reads ITS envelopes: an annual
    # one there (gone from today's list) still settles over the whole year
    if not has_annual and snapped:
        for r in conn.execute("SELECT bills FROM budget_snapshots").fetchall():
            sched = r["bills"] if isinstance(r["bills"], dict) else {}
            rows = sched.get("rows") if isinstance(sched.get("rows"), list) else []
            if any(isinstance(b, dict) and isinstance(b.get("raw"), dict)
                   and b["raw"].get("bill_type") == "envelope"
                   and budget.envelope_months(b["raw"]) > 1 for b in rows):
                has_annual = True
                break

    # months of the window before the household's first bill began: their
    # spend cannot be split into fixed and variable, so the picture shows
    # it apart as "unsplit"
    unscheduled: list[dt.date] = []

    def _fixed_for(start: dt.date, stop: dt.date) -> float:
        # CLOSED months land in a short-lived process cache — an
        # all-history window on a decades-long ledger is hundreds of
        # month_status calls (seconds); the stamp in the key misses on
        # any change to what the total is computed from. Each entry is
        # (fixed actual, whether a bill schedule reached the month).
        total, cur = 0.0, start
        month_start = today.replace(day=1)
        now = dt.datetime.now().timestamp()
        while cur <= stop:
            key = (tid, stamp, cur.isoformat())
            digest = _fixed_month_digest(
                month_stamps, _fixed_month_reach(cur),
                _fixed_month_stop(cur, has_annual))
            hit = _FIXED_MONTH_CACHE.get(key)
            if (hit is not None and cur < month_start and hit[1] > now
                    and hit[2] == digest):
                total += hit[0][0]
                if not hit[0][1]:
                    unscheduled.append(cur)
                cur = (cur + dt.timedelta(days=32)).replace(day=1)
                continue
            last = min(stop, (cur + dt.timedelta(days=32)).replace(day=1)
                       - dt.timedelta(days=1))
            try:
                # its own savepoint: inside flow_breakdown's snapshot a
                # failed statement would abort every month after it
                with conn.transaction():
                    snap = frozen = None
                    if cur < month_start and (cur.year, cur.month) in snapped:
                        snap = budget.snapshot_config(conn, cur.year, cur.month)
                        if snap is not None:
                            # the bill schedule and the disabled set come from
                            # the snapshot; the account exclusions stay the
                            # live ones, which the total this fixed figure is
                            # split out of (run_out) is read under — one
                            # month's two halves must not use two exclusion
                            # lists
                            snap = {**snap, "excluded_accounts":
                                    budget.excluded_account_ids(cfg)}
                        frozen = budget.snapshot_bill_rows(conn, cur.year,
                                                           cur.month)
                    # a closed month is matched as the Month lens matches it:
                    # closed, so a payment dated in the next month homes a late
                    # bill where the lens homes it (_fixed_month_stop reaches
                    # that far)
                    st = budget.month_status(conn, last, historical=False,
                                             cfg=snap, bill_rows=frozen,
                                             memo=memo,
                                             closed=cur < month_start)
                fb = st["buckets"]["fixed"]
                sched = _schedule_reached(fb)
                # a month no schedule reached is unsplit whole: a charge a
                # bill matched before its start is not called fixed, so
                # fixed + variable + unsplit stays what went out
                v = float(fb["actual"] or 0) if sched else 0.0
                total += v
                if not sched:
                    unscheduled.append(cur)
                if cur < month_start:              # the live month stays live
                    pending.append((key, ((v, sched),
                                          now + _FIXED_CACHE_TTL, digest)))
            except Exception:                      # noqa: BLE001
                log.exception("fixed-spend month %s failed", cur)
            cur = (cur + dt.timedelta(days=32)).replace(day=1)
        return total

    out: dict = {}
    for key, (start, label) in periods.items():
        inc = {"paychecks": 0.0, "interest": 0.0, "other": 0.0}
        total_out = 0.0
        fixed = 0.0
        known_in = known_out = 0.0
        for a, b, run_known in runs:
            # A run of CLOSED months sums to the same in and out until one
            # of its inputs moves, and the stamp names them all (the
            # ledger, the bills the income kinds are read from, the
            # config); the bank accounts, the exclusion and the measured
            # window ride in the key too. The all-time window is two whole-ledger scans per run
            # otherwise, on every visit. Kept beside the closed months'
            # fixed totals, under the same expiry, so one clear drops both.
            rkey = (tid, stamp, f"run:{a.isoformat()}:{b.isoformat()}:"
                                f"{','.join(bank)}:{excl_sql}:{ex_p!r}:"
                                f"{verdicts.fingerprint}:{mw.fingerprint}")
            # the run reads only the rows dated inside it
            rdigest = _fixed_month_digest(month_stamps, a,
                                          b - dt.timedelta(days=1))
            hit = _FIXED_MONTH_CACHE.get(rkey)
            now = dt.datetime.now().timestamp()
            if hit is not None and hit[1] > now and hit[2] == rdigest:
                run_inc, run_out = hit[0]
            else:
                # income per kind, each kind summed in scan order; the cache
                # keeps these few totals, never the rows (a multi-year run
                # holds every deposit in history)
                run_inc: dict[str, float] = {}
                for r in conn.execute(
                        f"""SELECT {DISPLAY_MERCHANT} payee, t.name,
                                  COALESCE(t.category_detailed,'') AS d,
                                  {INCOME_NET} AS amt
                             FROM transactions t {MC_JOIN}
                            WHERE {_INCOME_WHERE} AND t.date >= %s AND t.date < %s
                            {excl_sql}""",
                        (bank, a, b, *ex_p)):
                    # the same exclusion on the IN side as on the OUT side
                    # below — judged under different rules, an excluded
                    # account that takes $6k in and pays $5.5k out reads as
                    # "+$5,500 stayed"
                    k = verdicts.kind(r["payee"], r["name"], r["d"])
                    run_inc[k] = run_inc.get(k, 0.0) + float(r["amt"] or 0)
                run_out = float(conn.execute(
                    f"""SELECT {_R2.format(f"COALESCE(SUM({NET_AMOUNT_JOINED}), 0)")} amt
                         FROM transactions t {REIMB_PARTIAL_JOIN}
                        WHERE {SPEND_WHERE} AND t.date >= %s AND t.date < %s
                        {excl_sql}""",
                    (a, b, *ex_p)).fetchone()["amt"] or 0)
                if b <= today.replace(day=1):     # the live month stays live
                    try:                   # a cache miss never fails the page
                        pending.append((rkey, ((dict(run_inc), run_out),
                                               now + _FIXED_CACHE_TTL,
                                               rdigest)))
                    except Exception:      # noqa: BLE001
                        log.exception("cash-flow run cache write failed")
            for k, v in run_inc.items():
                inc[k] += v
            total_out += run_out
            if run_known:
                known_in += sum(run_inc.values())
                known_out += run_out
            fixed += min(_fixed_for(a, b - dt.timedelta(days=1)), run_out)
        sp = {"fixed": round(fixed, 2), "variable": round(total_out - fixed, 2)}
        split = _flow_split(conn, sp, unscheduled, months_total, excl_sql,
                            ex_p)
        split["fixed_basis_text"] = _fixed_basis_words(
            budget.live_schedule(conn, memo=memo), start, last_day,
            set(unscheduled) if split["unsplit"] > 0 else set())
        total_in = round(sum(inc.values()), 2)
        total_out = round(total_out, 2)
        known_in = round(known_in, 2)
        saved = round(known_in - known_out, 2)
        inc_out = {"total": total_in,
                   **{k: round(v, 2) for k, v in inc.items()}}
        # an empty window misreports nothing (0 in, 0 out, nothing lost),
        # and the clients draw no picture for it, so it keeps its zeros
        unknown = income_unknown and total_out != 0
        if unknown:
            inc_out = {k: None for k in inc_out}
            saved = None
        invested = investment_funding_between(conn, start, end)
        inc_out["shown"], out_shown = _flow_shown(inc_out, total_out, sp,
                                                  saved)
        _in, split_shown = _flow_shown(inc_out, total_out, sp, saved,
                                       unsplit=split["unsplit"])
        out[key] = {
            "from": start.isoformat(), "to": last_day.isoformat(),
            "label": label,
            "live_month": live,
            # so_far / heading (window_words; the picture compares with
            # nothing, so it has no prev_text)
            **{k: v for k, v in window_words(range_key, start, last_day,
                                             None, None, live).items()
               if k != "prev_text"},
            "income_due": None,
            # no window is cut or holed any more (every tab reads the same
            # months); kept false/0 for installed clients that read them
            "income_clipped": False,
            "income_gap_months": 0,
            # months of the window whose income the bank feed misses:
            # counted, and marked on the page
            "incomplete_months": incomplete,
            # Cash Flow dates spending by the day it was paid
            "dating": "paid",
            # true when no month of the window has known income: in, saved
            # and rate are then null (unknown), never $0 and a loss
            "income_unknown": unknown,
            "in": inc_out,
            "out": {"total": total_out, **{k: round(v, 2) for k, v in sp.items()},
                    "shown": out_shown,
                    # `variable` keeps its meaning (every dollar not fixed)
                    # for installed clients; the picture draws the split
                    # (_flow_split): fixed, the variable spend of the
                    # months a bill schedule reached, and the spend of the
                    # months none did, unsplit
                    **split, "split_shown": split_shown},
            # over the months with bank data only (`saved_months_known` of
            # `saved_months_total`); in and out cover every month
            "saved": saved,
            "rate": (round(100 * saved / known_in, 1)
                     if known_in and saved is not None else None),
            "saved_months_known": months_total - len(incomplete),
            "saved_months_total": months_total,
            # what moved to investments over the window: its own line
            # under what was left over, never part of it
            "invested": invested,
            "invested_text": invested_words(invested),
        }
    if pending:
        try:                       # a cache miss never fails the page
            if (_fixed_month_probe(conn) == probe_before
                    and _fixed_inputs_stamp(conn, cfg) == stamp):
                _fixed_cache_turn(tid, stamp)
                for k, v in pending:
                    _FIXED_MONTH_CACHE[k] = v
        except Exception:          # noqa: BLE001
            log.exception("cash-flow cache write failed")
    res = out[range_key]
    res["income_due"] = income_still_due(conn, today, res["so_far"],
                                         memo=memo)
    return res


def _schedule_reached(fixed_bucket: dict) -> bool:
    """Whether the household had a bill schedule by the month whose
    month_status fixed bucket this is. Decided once, by
    budget.month_reached, from the bills' starts (scheduled_since): every
    month from the first bill's start on is reached (specs/honest-figures.md,
    "Months before any bill are unsplit"); this only reads its answer."""
    return bool(fixed_bucket.get("reached"))


def _flow_split(conn, sp: dict, unscheduled: list, months_total: int,
                excl_sql: str, ex_p: tuple) -> dict:
    """The window's spend split three ways for the picture: fixed (the
    charges bills matched), variable over the months a bill schedule
    reached, and `unsplit` — what went out in the months before the
    household's first bill began, which cannot be called fixed or
    variable. A window with no scheduled month at all, or
    no unscheduled one, has nothing unsplit: every non-fixed dollar is
    variable, as before. `unsplit_text` says what the band is, and what
    share fixed is of the months it can be measured in."""
    out = {"variable_scheduled": sp["variable"], "unsplit": 0.0,
           "unsplit_text": None}
    if not unscheduled or len(unscheduled) >= months_total:
        return out
    starts = sorted(unscheduled)
    amt = float(conn.execute(
        f"""SELECT {_R2.format(f"COALESCE(SUM({NET_AMOUNT_JOINED}), 0)")} amt
             FROM transactions t {REIMB_PARTIAL_JOIN}
            WHERE {SPEND_WHERE} AND date_trunc('month', t.date)::date = ANY(%s)
            {excl_sql}""", (starts, *ex_p)).fetchone()["amt"] or 0)
    unsplit = round(min(max(amt, 0.0), max(sp["variable"], 0.0)), 2)
    if unsplit <= 0:
        return out
    n = len(starts)
    rest = months_total - n
    variable_scheduled = round(sp["variable"] - unsplit, 2)
    # fixed as a share of the months a schedule reached — the picture's
    # own shares are of the whole window
    share = shown_shares([sp["fixed"], variable_scheduled])[0]
    covered = ("" if share is None else
               f" In the other {rest} month{'s' if rest != 1 else ''} the "
               f"charges your bills matched are {share}% of what went out.")
    out.update(
        variable_scheduled=variable_scheduled,
        unsplit=unsplit,
        # said as measured: these months are the ones before the first
        # of the household's bills began (budget.month_reached), and the
        # share is of the charges today's bills matched
        unsplit_text=(f"${whole_dollars(unsplit):,} went out in {n} "
                      f"month{'s' if n != 1 else ''} before your first "
                      "bill — not split into fixed and variable."
                      + covered))
    return out


def _fixed_basis_words(schedule: tuple[list, list], start: dt.date,
                       last_day: dt.date, unsplit: set) -> str | None:
    """What Fixed means on a window reaching back before most of today's
    bills began (specs/honest-figures.md, "Fixed is today's bills, and a
    long window says so"): the months the picture splits (those not in
    the `unsplit` band) that budget.month_mostly_unstarted calls early,
    counted in one sentence; None when there are none."""
    split = []
    cur = start.replace(day=1)
    while cur <= last_day:
        nxt = (cur + dt.timedelta(days=32)).replace(day=1)
        if cur not in unsplit:
            split.append(nxt - dt.timedelta(days=1))
        cur = nxt
    early = sum(1 for end in split
                if budget.month_mostly_unstarted(*schedule, end))
    if not early:
        return None
    lead = "Fixed counts the bills you track today; "
    if len(split) == 1:
        return lead + "this month is from before most of them began."
    these = "the other" if unsplit else "these"
    return (lead + f"{early} of {these} {len(split)} months "
            f"{'is' if early == 1 else 'are'} from before most of them "
            "began.")


_INCOME_KINDS = ("paychecks", "interest", "other")


def income_kinds_shown(kinds: dict, total: float) -> dict[str, int]:
    """Each income kind in the whole dollars it prints, adding up to
    `total` as printed — the one figure the Overview picture and the
    Income tab's legend both show (specs/shown-figures.md, "One income
    figure on Overview and Income")."""
    return dict(zip(_INCOME_KINDS, shown_dollars(
        [kinds[k] for k in _INCOME_KINDS], total)))


def _flow_shown(inc: dict, total_out: float, sp: dict,
                saved: float | None, *,
                unsplit: float | None = None) -> tuple[list | None, list]:
    """The flow picture's node figures as it prints them, one
    {key, dollars, share} per node in the clients' order. The income
    side's dollars add up to "came in" and its shares to 100; spending's
    fixed and variable add up to "went out", and what stayed (or the
    overspend) is that figure in whole dollars. A sink's share is of the
    larger of in and out, the height the picture draws, so the three add
    up to 100 when every month of the window is measured; the overspend
    carries no share. The income side is None when no bank feed covers
    the window (nothing on it is known)."""
    in_shown = None
    if inc.get("total") is not None:
        keys = _INCOME_KINDS
        vals = [inc[k] for k in keys]
        dollars = income_kinds_shown(inc, inc["total"])
        in_shown = [{"key": k, "dollars": dollars[k], "share": p}
                    for k, p in zip(keys, shown_shares(vals))]
        if saved is not None and saved < 0:
            in_shown.append({"key": "overspent",
                             "dollars": whole_dollars(-saved), "share": None})
    keys = ["fixed", "variable"]
    vals = [sp["fixed"], sp["variable"]]
    if unsplit:
        # the split the picture draws: variable is the scheduled months'
        keys.append("unsplit")
        vals = [sp["fixed"], round(sp["variable"] - unsplit, 2), unsplit]
    dollars = shown_dollars(vals, total_out)
    if saved is not None and saved > 0:
        keys.append("saved")
        vals.append(saved)
        dollars.append(whole_dollars(saved))
    base = max(total_out, inc.get("total") or 0)
    out_shown = [{"key": k, "dollars": d, "share": p}
                 for k, d, p in zip(keys, dollars,
                                    shown_shares(vals, base))]
    return in_shown, out_shown


def _counted_rows_where(conn, accounts: list[str]) -> tuple[str, tuple]:
    """(WHERE clause over `transactions t`, its parameters) selecting the
    rows of `accounts` that an income total over them can count: live,
    not on an excluded account, not on a shadow (a HIDDEN account or the
    non-primary copy of a linked pair), not business-entity money. Every
    question of the form "from when, and in which years, is income
    measured" reads these rows and no others. A row the income total
    drops must not decide coverage either: an old checking account the
    user hid still holds years of payroll, and judged on its rows those
    years read as "$0 came in" against their card spend, a loss nobody
    made, where the honest answer is that the income is unknown."""
    excl, ex_p = excluded_accounts_sql(conn)
    return (f"""t.removed=0 AND t.account_id = ANY(%s) {excl}
             {_NOT_SHADOW.format(col="t.account_id")}
             {budget.PERSONAL_ONLY_SQL}""", (accounts, *ex_p))


def _bank_coverage_years(conn) -> set[str]:
    """Years with bank rows the income total counts (MeasuredWindow
    .income_years). Which of their months are known is the window's
    month_known, not this set."""
    return set(coverage.window(conn).income_years)


class _IncomeCoverage(tuple):
    """(years with bank rows, first measured month) that also carries the
    MeasuredWindow it was read from, so `_income_known` / `_known_months`
    answer with the window's month-by-month rule, never a year-grained
    copy of it."""
    window: "coverage.MeasuredWindow"


def _income_coverage(conn) -> tuple[set[str], str | None]:
    """(years with bank rows, first month 'YYYY-MM' whose income is measured
    whole, or None) — the household's MeasuredWindow income half
    (engine/coverage.py holds the rule and its edge cases)."""
    w = coverage.window(conn)
    out = _IncomeCoverage((set(w.income_years), w.income_from))
    out.window = w
    return out


def _income_known(cov, key: str) -> bool:
    """Whether month `key` ('YYYY-MM') has complete income: the bank feed
    measures it (MeasuredWindow.month_known). The one predicate under
    every Cash Flow figure — `_incomplete_months` is its list form."""
    return cov.window.month_known(key)


def _known_months(cov, through: str) -> list[str]:
    """Every known 'YYYY-MM' through `through` (MeasuredWindow.known_months)."""
    return cov.window.known_months(through)


def _investment_funding(conn) -> list[list]:
    """Net external cash into investments per year (+ in, − out); see
    `_funding_by_year` for what counts."""
    by_year = _funding_by_year(conn)
    return [[y, round(by_year[y], 2)] for y in sorted(by_year)]


def investment_funding_between(conn, start: dt.date, end: dt.date) -> float:
    """Net external cash into investments from `start` up to (not
    including) `end`: the by-year figure's own rows, over those days."""
    return round(sum(_funding_by_year(conn, start, end).values()), 2)


def invested_words(amount: float) -> str | None:
    """A window's investment funding as Cash Flow prints it under what was
    left over: None when it rounds to $0."""
    if not whole_dollars(amount):
        return None
    return (f"moved to investments {_money_words(amount)}" if amount > 0
            else f"took out of investments {_money_words(-amount)}")


def _funding_by_year(conn, start: dt.date | None = None,
                     end: dt.date | None = None) -> dict[str, float]:
    """Net external cash into investments (+ in, − out) by year, over the
    whole ledger or the days from `start` up to `end`.

    Measured from the BANK side — money that actually left the user's real
    depository accounts for an investment provider, netted against money that
    came back — PLUS workplace-plan payroll contributions (which bypass
    the bank entirely). Two reasons this is the right lens:
      • Provider-to-provider rollovers net to ~0: the checking→provider leg
        (+) is offset by the provider→checking leg (−), leaving only the
        genuine new money.
      • It is immune to a provider's internal INVBANKTRAN/BUYMF churn, which
        never touches the bank.
    Investment-provider depository accounts (a robo-advisor's cash sweep) are
    excluded so only the real-bank side of each transfer is counted once.
    Negative years are genuine net withdrawals.

    Shadow-excluded like every other money aggregate — a checking account
    fed by two linked sources would otherwise report each transfer
    to the investment provider twice."""
    by_year: dict[str, float] = {}
    # Real-bank depository accounts, resolved first so the ledger is read by
    # account id (an index probe) rather than joined inside a full scan; the
    # provider-name test runs in SQL as the same substring match. NULL
    # institution names fail the NOT IN test here as they did in the join.
    bank_ids = [r["id"] for r in conn.execute(
        """SELECT a.id FROM accounts a JOIN items i ON i.id = a.item_id
           WHERE a.type = 'depository'
             AND LOWER(i.institution_name) NOT LIKE ALL(%s)""", (_INV_PATTERNS,))]
    days = "AND t.date >= %s AND t.date < %s" if start and end else ""
    day_p = (start, end) if start and end else ()
    for r in conn.execute(
            f"""SELECT to_char(t.date,'YYYY') yr, SUM(t.amount) amt
               FROM transactions t
               WHERE t.removed = 0 AND t.account_id = ANY(%s)
                 AND LOWER(t.name) LIKE ANY(%s) {days}
                 {_NOT_SHADOW.format(col="t.account_id")}
                 {budget.PERSONAL_ONLY_SQL}
               GROUP BY yr""",
            (bank_ids, _INV_PATTERNS, *day_p)):
        # Plaid sign: +amount = money OUT of the bank = INTO investments.
        by_year[r["yr"]] = by_year.get(r["yr"], 0.0) + r["amt"]
    # workplace-plan payroll contributions: the plan-CSV door writes its
    # rows under its items' accounts, so the account ids make this an index
    # probe; the row's own type says it was a contribution
    plan_ids = [r["id"] for r in conn.execute(
        """SELECT a.id FROM accounts a JOIN items i ON i.id = a.item_id
           WHERE i.aggregator = 'plan_csv'""")]
    for r in conn.execute(
            f"""SELECT to_char(t.date,'YYYY') yr, SUM(ABS(t.amount)) amt
               FROM transactions t
               WHERE t.account_id = ANY(%s) AND t.removed = 0
                 AND LOWER(t.raw ->> 'type') LIKE '%%contribution%%' {days}
               GROUP BY yr""", (plan_ids, *day_p)):
        by_year[r["yr"]] = by_year.get(r["yr"], 0.0) + r["amt"]
    return by_year


def _graph_timeframes(conn, today: dt.date | None, graph: dict) -> dict:
    """`graph` (_cash_graph) with what each timeframe reads of it:
    `extremes` (_saved_extremes) and `windows` (_drawn_windows), both
    judged on that timeframe's Cash Flow window (_cashflow_window)."""
    today = today or dt.date.today()
    frames = {key: _cashflow_window(conn, today, key, None)
              for key in FLOW_RANGES}
    return {**graph, "extremes": _saved_extremes(graph, frames),
            "windows": _drawn_windows(today, graph, frames)}


def _saved_extremes(graph: dict, frames: dict) -> dict:
    """Per timeframe, the best and worst saved month as [YYYY-MM, saved]
    among the months that are over, have known income and fall inside
    that timeframe's Cash Flow window (`frames`); both None when the
    window holds fewer than two such months. The month in progress and
    the forecast are never candidates (specs/counted-figures.md, "Best
    and worst month are the window's")."""
    done = graph["points"][:graph["forecast_from"]]
    out = {}
    for key, (start, end, _prev, _label, live) in frames.items():
        first, stop = start.strftime("%Y-%m"), end.strftime("%Y-%m")
        # `end` is the first day after the window: a window of complete
        # months stops before that month, one running to today has none
        inside = ([] if live or key == "cur"
                  else [p for p in done if first <= p[0] < stop])
        if len(inside) < 2:
            out[key] = {"best": None, "worst": None}
            continue
        out[key] = {"best": list(max(inside, key=lambda p: p[1])),
                    "worst": list(min(inside, key=lambda p: p[1]))}
    return out


# the line under "Left over, by month", by whether the bars reach this month
_LEFT_OVER_NOTE = ("left over = income − spending; spending is dated by "
                   "the day it was paid")
_LEFT_OVER_NOTE_LIVE = ("left over = income − spending, this month so far "
                        "included; spending is dated by the day it was paid")


def _drawn_windows(today: dt.date, graph: dict, frames: dict) -> dict:
    """Per timeframe, the months "Left over, by month" draws: `first` and
    `last` (month keys, inclusive) over the graph's slots — finished
    months, the month in progress, the forecast — and `note`, the line
    under the chart. A timeframe of complete months ends with its last
    complete month; the month in progress and the forecast follow only a
    timeframe that includes this month: "cur", "all" (the whole graph),
    and any key whose window is the month so far because the ledger has no
    complete month yet (specs/counted-figures.md, "The month bars end
    where the window ends")."""
    cur = today.strftime("%Y-%m")
    slots = [p[0] for p in graph["points"]]
    end_slot = max([*slots, cur])
    out = {}
    for key, (start, end, _prev, _label, live) in frames.items():
        first = start.strftime("%Y-%m")
        if live or key in ("cur", "all"):
            if key == "all":
                first = min([*slots, first])
            out[key] = {"first": first, "last": end_slot,
                        "note": _LEFT_OVER_NOTE_LIVE}
        else:
            out[key] = {"first": first,
                        "last": (end - dt.timedelta(days=1)).strftime("%Y-%m"),
                        "note": _LEFT_OVER_NOTE}
    return out


# ---- a card payment's two legs ----------------------------------------------
# A card payment is two rows: the debit leaving the household's checking and
# the credit landing on the card, the same amount to the cent, posted up to
# CARD_SETTLE_DAYS apart. Each credit backs at most ONE checking leg, and
# the category-marked payments claim theirs before a plain transfer out may.
# The Cash Flow month counts a settlement by it, and the forecast finds a
# payment still on its way (forecast.payments_on_their_way) by it.
CARD_SETTLE_DAYS = 5


def card_settlement_legs(conn, since: dt.date, until: dt.date
                         ) -> tuple[list[dict], list[dict]]:
    """The checking-side legs a card payment can be, dated [since, until):
    (rows the feed marks as a card payment, rows filed as a plain transfer
    out), each {date, amount, account_id, pending}; personal accounts only,
    excluded and shadow accounts left out."""
    excl, ex_p = excluded_accounts_sql(conn)
    shape = f"""FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE t.removed = 0 AND t.amount > 0
              AND a.type NOT IN ('credit', 'loan')
              AND t.date >= %s AND t.date < %s {excl}
              {_NOT_SHADOW.format(col="t.account_id")}
              {budget.PERSONAL_ONLY_SQL}"""
    cols = "SELECT t.id, t.date, t.amount, t.account_id, t.pending"
    pay_rows = conn.execute(
        f"""{cols} {shape}
              AND COALESCE(t.category_detailed,'')
                  = 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
            ORDER BY t.date, t.id""",
        (since, until, *ex_p)).fetchall()
    xfer_rows = conn.execute(
        f"""{cols} {shape}
              AND COALESCE(t.category_override, t.category_primary, '')
                  = 'TRANSFER_OUT'
              AND COALESCE(t.category_detailed,'')
                  != 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
            ORDER BY t.date, t.id""",
        (since, until, *ex_p)).fetchall()
    return pay_rows, xfer_rows


def card_credit_inflows(conn, since: dt.date, through: dt.date
                        ) -> list[dict]:
    """The card-side legs a card payment can be, dated [since, through]:
    money into a credit account shaped like a payment (a transfer or
    payment category, or none) — a merchant refund must not back a
    "payment". {date, amount (positive), account_id, pending, txn_id}."""
    excl, ex_p = excluded_accounts_sql(conn)
    return [{"date": r["date"], "amount": float(-r["amount"]),
             "account_id": r["account_id"], "pending": r["pending"],
             "txn_id": r["id"]}
            for r in conn.execute(
                f"""SELECT t.id, t.date, t.amount, t.account_id, t.pending
                FROM transactions t JOIN accounts a ON a.id = t.account_id
                WHERE t.removed = 0 AND t.amount < 0 AND a.type = 'credit'
                  AND (COALESCE(t.category_override,
                                t.category_primary, '') = ''
                       OR COALESCE(t.category_override, t.category_primary)
                          IN ('TRANSFER_IN', 'TRANSFER_OUT',
                              'LOAN_PAYMENTS'))
                  AND t.date >= %s AND t.date <= %s {excl}
                  {_NOT_SHADOW.format(col="t.account_id")}
                  {budget.PERSONAL_ONLY_SQL}
                ORDER BY t.date, t.id""",
                (since, through, *ex_p)).fetchall()]


def claim_card_settlement(credits: list, day: dt.date,
                          amount: float) -> int | None:
    """Pair one checking leg (`day`, `amount`) with the first unclaimed
    credit in `credits` — a list of (date, amount) or of {date, amount},
    in the caller's order — of the same amount to the cent, at most
    CARD_SETTLE_DAYS apart. The credit is removed from the list and its
    index returned; None when nothing pairs."""
    for i, c in enumerate(credits):
        cd, ca = (c["date"], c["amount"]) if isinstance(c, dict) else c
        if (abs(ca - amount) <= 0.01
                and abs((as_date(cd) - as_date(day)).days)
                <= CARD_SETTLE_DAYS):
            del credits[i]
            return i
    return None


def _cash_graph(conn, income_m: dict, spend_m: dict,
                today: dt.date | None = None) -> dict:
    """One continuous monthly net-cash-flow series — every
    historical month whose income is known (MeasuredWindow: the same
    income − spend the per-year charts render), then the forward forecast (forecast.HORIZON_DAYS)
    folded into calendar months. The forward part is DERIVED from engine/forecast's
    autopay-statement balance series (balance delta over each month =
    net flow), so it reconciles with the Today page by construction —
    no second money model. The current month's actual-so-far is measured
    CASH-basis to match (see the cash-basis comment below). Returns
    {points: [[YYYY-MM, net]…],
     forecast_from: index of the first month carrying forecast}."""
    from . import forecast
    # the household's day when the caller has it (the API resolves it per
    # request); the process clock is the fallback for callers without it
    today = today or dt.date.today()
    cur_key = today.strftime("%Y-%m")
    # the month's rows by date range — the (tenant, date) index answers
    # it, where a formatted-date test had to scan the ledger
    cur_start = today.replace(day=1)
    cur_next = (cur_start.replace(year=cur_start.year + 1, month=1)
                if cur_start.month == 12
                else cur_start.replace(month=cur_start.month + 1))
    # Saved is income − spending for every known month before this one
    # (`points` up to `forecast_from`, the meaning installed apps read).
    # The month in progress is its own field, `current`: what it has saved
    # so far, the figure its year row sums (monthly_by_year) — drawn as a
    # solid bar after the history. The forecast follows in `points` from
    # `forecast_from`, the current month among it as the whole month's
    # cash outlook: hollow, beside the saved bars, never added into one
    # (specs/counted-figures.md).
    known = _known_months(_income_coverage(conn), cur_key)
    points = [[k, round(income_m.get(k, 0.0) - spend_m.get(k, 0.0), 2)]
              for k in known if k < cur_key]
    current = ([cur_key, round(income_m.get(cur_key, 0.0)
                               - spend_m.get(cur_key, 0.0), 2)]
               if cur_key in known else None)
    forecast_from = len(points)
    try:
        # forecast.HORIZON_DAYS, not a second hard-coded horizon. A longer
        # horizon here than the rest of the product measures would render
        # days no backtest covers — the walk's optimism grows with the
        # horizon, so the furthest days are the least trustworthy part of
        # the picture while looking exactly as confident as day 1.
        fc = forecast.build(conn, today=today, days=forecast.HORIZON_DAYS)
    except Exception:
        fc = None
    if fc is None:
        return {"points": points, "forecast_from": forecast_from,
                "current": current,
                "forecast_days": forecast.HORIZON_DAYS}
    # month-by-month balance deltas along the forecast walk; the current
    # month combines actual-so-far with its forecast remainder
    start_bal = {cur_key: fc["checking"]}
    end_bal: dict[str, float] = {}
    prev = fc["checking"]
    for d_iso, bal in fc["pace_usual"]["series"]:
        mk = d_iso[:7]
        start_bal.setdefault(mk, prev)
        end_bal[mk] = bal
        prev = bal
    # The current month's actual-so-far, CASH basis. The forward deltas
    # come from a CASH walk of checking (pace_usual, the usual walk, pays
    # card spending when the household pays the card — forecast.build), so
    # the actual added to the SAME month must be
    # cash too. An economic MTD (income − spend, where spend counts card
    # purchases at CHARGE time) double-counts card spend:
    #   $300 charged this month, still on the card
    #     economic MTD:      −300   (the charge, in spend_m)
    #     forecast autopay:  −300   (the walk pays the same balance off)
    #     graph:             −600   for $300 of economic outflow.
    # Cash basis on the checking side instead:
    #   + income deposits (income_m — depository-only, shadow-excluded)
    #   − spend on NON-credit accounts (a debit purchase left cash the
    #     moment it posted; a card purchase did not)
    #   − card payments that POSTED this month (that is when card spend
    #     becomes cash out; the walk only schedules what is STILL owed, so
    #     a payment already made is never re-scheduled)
    # Invariant: each card dollar reaches the current month exactly once —
    # via the posted payment if the card was paid this month, via the
    # walk's autopay if not. Dropping credit spend WITHOUT adding posted
    # payments back would count a charge-paid-this-month zero times.
    # (A charge from a PRIOR month settled this month appears in that
    # month's economic point and again here as the cash leaves — that is
    # the fixed seam where the economic history meets the cash forecast,
    # not an intra-model double count.)
    # Excluded accounts are invisible here too, on BOTH legs — the income
    # and spend series this point sits in already drop them, and dropping
    # one leg only would charge the month a settlement whose purchases it
    # never counted.
    excl, ex_p = excluded_accounts_sql(conn)
    mtd_spend_cash = conn.execute(
        f"""SELECT COALESCE(SUM({NET_AMOUNT_JOINED}), 0) AS amt
            FROM transactions t JOIN accounts a ON a.id = t.account_id
            {REIMB_PARTIAL_JOIN}
            WHERE {SPEND_WHERE} AND a.type != 'credit'
              AND t.date >= %s AND t.date < %s {excl}""",
        (cur_start, cur_next, *ex_p)).fetchone()["amt"]
    pay_rows, xfer_rows = card_settlement_legs(conn, cur_start, cur_next)
    mtd_card_pay = sum(float(r["amount"]) for r in pay_rows)
    # The category is not the only way a settlement shows up. A
    # sparse source (SimpleFIN, bare CSV) files the checking-side autopay
    # ("CHASE AUTOPAY") as TRANSFER_OUT — the cash still left checking, and
    # the walk won't re-schedule it (the card balance already dropped), so
    # missing it under-counts the month by the whole payment. Doctrine:
    # a checking-side TRANSFER_OUT IS a card settlement when a credit-
    # account inflow pairs with it (claim_card_settlement) — the
    # category-marked payments claim theirs first, so a coincidental
    # same-amount transfer can't inherit an already-counted settlement.
    # The pairing is the evidence; an unpaired TRANSFER_OUT stays a plain
    # own-money move.
    if xfer_rows:
        month_start = today.replace(day=1)
        credit_in = [(r["date"], r["amount"]) for r in card_credit_inflows(
            conn, month_start - dt.timedelta(days=CARD_SETTLE_DAYS),
            today + dt.timedelta(days=CARD_SETTLE_DAYS))]
        for r in pay_rows:
            claim_card_settlement(credit_in, r["date"], float(r["amount"]))
        for r in xfer_rows:
            if claim_card_settlement(credit_in, r["date"],
                                     float(r["amount"])) is not None:
                mtd_card_pay += float(r["amount"])
    # Posted savings-goal transfers, symmetric with the walk.
    # forecast.build schedules each goal's plan NET of what already
    # posted this month (posted = max(0, _matched_net since the 1st)), and
    # the posted money is already out of live checking — the walk's start —
    # so without this term it appears in neither the walk's deltas nor the
    # MTD figure: counted ZERO times. Subtracting the SAME per-goal posted amount
    # here makes the month's savings-out exactly
    #   posted (MTD) + max(0, plan − posted) (walk) = max(plan, posted)
    # — once, never zero, never twice. Scope mirrors the walk precisely:
    # goals with a plan the walk schedules, skipping plan-only goals (no
    # ledger to observe; the walk carries their full plan instead).
    # Both sides read posted_for_plan — the same helper the walk nets against —
    # so checking-leg-only transfers count here exactly once too, and
    # token-less goals sharing an account partition instead of each
    # claiming the full net. A business's goal is the walk's exception too
    # (savings.household_goals): the walk leaves it out, so this does.
    from . import savings as _savings
    cfg = budget.load_config(conn)
    first = today.replace(day=1)
    mtd_savings = sum(
        max(0.0, _savings.posted_for_plan(conn, cfg, g, since=first))
        for g in _savings.household_goals(conn, cfg)
        if float(g.get("monthly_plan") or 0) > 0
        and not _savings.is_plan_only(g))
    mtd_actual = (income_m.get(cur_key, 0.0) - mtd_spend_cash
                  - mtd_card_pay - mtd_savings)
    # The walk ends HORIZON_DAYS out, almost never on a month end. A month
    # it reaches only a few days into is not that month's net flow — rent
    # and the savings plan on the 1st and one paycheck read as a losing
    # month, or a payday right before the cut as a winning one — so, like
    # every other partial month in the reports (_range_window holds
    # complete months only), it is not drawn. The current month is whole
    # by construction: actual-so-far plus the walk's remainder.
    series = fc["pace_usual"]["series"]
    if series:
        last_d = as_date(series[-1][0])
        if (last_d.strftime("%Y-%m") != cur_key
                and (last_d + dt.timedelta(days=1)).day != 1):
            end_bal.pop(last_d.strftime("%Y-%m"), None)
    for mk in sorted(end_bal):
        net = end_bal[mk] - start_bal[mk]
        if mk == cur_key:
            net += mtd_actual
        points.append([mk, round(net, 2)])
    # the horizon the caption names — the forecast's own, never typed
    return {"points": points, "forecast_from": forecast_from,
            "current": current,
            "forecast_days": forecast.HORIZON_DAYS}


def compute_cashflow(conn, today: dt.date | None = None) -> dict:
    # Year and month spend from one scan: GROUPING SETS rounds each grouping's
    # SUM in SQL as the two separate queries did (a year is NOT the sum of its
    # rounded months), so the figures are unchanged and the scan is shared.
    spend_y: dict = {}
    spend_m: dict = {}
    excl, ex_p = excluded_accounts_sql(conn)
    for r in conn.execute(
            f"SELECT to_char(t.date,'YYYY') yr, to_char(t.date,'YYYY-MM') p, "
            f"{_R2.format(f'SUM({NET_AMOUNT_JOINED})')} amt "
            f"FROM transactions t {REIMB_PARTIAL_JOIN} WHERE {SPEND_WHERE} {excl} "
            f"GROUP BY GROUPING SETS ((yr), (p))", ex_p or None):
        if r["p"] is None:
            spend_y[r["yr"]] = r["amt"]
        else:
            spend_m[r["p"]] = r["amt"]
    income_y, income_m = _income_year_month(conn)
    years = sorted(set(spend_y) | set(income_y))
    savings = []
    # Out is every month of the year and In every deposit counted in it,
    # as a range's total counts them. Saved and the rate are over the
    # year's months with complete income — the ONE rule, by the month
    # (_incomplete_months), that the flow picture and the month bars read:
    # a year holding an incomplete month is flagged with a note and saves
    # over its other months; a year with none complete has no In, Saved
    # or rate (specs/counted-figures.md). `savings_by_year_months` counts
    # its months: [complete, total], the total counting the year's months
    # from the ledger's first through this one.
    today = today or dt.date.today()
    cur_key = today_key = today.strftime("%Y-%m")
    first_key = min(list(spend_m) + list(income_m), default=today_key)
    incomplete = _incomplete_months(
        conn, dt.date(int(first_key[:4]), int(first_key[5:7]), 1), today)
    missing = set(incomplete)
    # month labels are abbreviations so a chart's x-axis reads by month
    _MO = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    year_months: dict = {}
    year_notes: dict = {}
    # per-year monthly saved (income − spend), for the expandable per-year
    # chart. `monthly_by_year` holds the months whose income is complete,
    # not the months that happened to receive a deposit: a covered month
    # with no pay and real spend is the month that lost the most, and
    # dropping it leaves the bars summing to more than the year's Saved
    # figure. Months before the feed starts stay out (unknown, not $0).
    # The month in progress is in the year's Saved figure, so it is a bar
    # too; months still ahead are not.
    monthly_by_year: dict = {}
    for key in _known_months(_income_coverage(conn), cur_key):
        monthly_by_year.setdefault(key[:4], []).append(
            [_MO[int(key[5:]) - 1],
             round(income_m.get(key, 0.0) - spend_m.get(key, 0.0), 2)])
    # the same bars for the year list's rows, with a slot (null) for each
    # incomplete month, so a chart keeps the month's place and marks it
    months_by_year: dict = {}
    year_dollars_: dict = {}
    for y in years:
        sp = spend_y.get(y, 0)
        keys = [f"{y}-{mo:02d}" for mo in range(1, 13)
                if first_key <= f"{y}-{mo:02d}" <= today_key]
        known = [k for k in keys if k not in missing]
        year_months[y] = [len(known), len(keys)]
        saved_m = {k: round(income_m.get(k, 0.0) - spend_m.get(k, 0.0), 2)
                   for k in known}
        if keys:
            months_by_year[y] = [[_MO[int(k[5:]) - 1], saved_m.get(k)]
                                 for k in keys]
        if len(known) < len(keys):
            year_notes[y] = income_incomplete_note(len(known), len(keys))
        if not known:
            # no month of the year has bank data: its income is unknown,
            # not $0, and there is no saved figure
            savings.append([y, None, round(sp, 2), None, None])
            year_dollars_[y] = year_dollars(None, sp, None,
                                            every_month=False)
            continue
        inc = income_y.get(y, 0)
        k_in = sum(income_m.get(k, 0.0) for k in known)
        net = k_in - sum(spend_m.get(k, 0.0) for k in known)
        rate = round(100 * net / k_in, 1) if k_in else None
        savings.append([y, round(inc, 2), round(sp, 2), round(net, 2), rate])
        # the row in the whole dollars it prints, as the Year lens's
        # totals print the same year (year_dollars)
        year_dollars_[y] = year_dollars(round(inc, 2), round(sp, 2),
                                        round(net, 2),
                                        every_month=len(known) == len(keys))
    # investment funding: net external cash into investments per year
    funding = _investment_funding(conn)
    # investment-funding shortfall signal (budget.cash_events, current
    # month; guarded — a cash_events failure must not sink the page)
    try:
        # `until` is exclusive: month_status passes tomorrow so a pull that
        # landed TODAY — the one the Today page is flagging this morning —
        # counts month-to-date here too, not from tomorrow
        ce = budget.cash_events(conn, today.replace(day=1),
                                today + dt.timedelta(days=1))
    except Exception:
        ce = {}
    reported = reported_income(conn)
    # full career-earnings spine: uncapped Medicare wages where SSA has them
    # (best true-wage figure), else SS earnings / reported wages. One point per
    # year, so the chart reaches back to the first-ever paycheck.
    career = [
        [r["year"], r["inc"]] for r in conn.execute(
            "SELECT year, COALESCE(medicare_earnings, ss_earnings, primary_wages, wages) inc "
            "FROM income_annual "
            "WHERE COALESCE(medicare_earnings, ss_earnings, primary_wages, wages) IS NOT NULL "
            "ORDER BY year")]
    return {
        # spending is dated by the day it was paid; the Month and Year
        # lenses date a bill by the month it belongs to
        "dating": "paid",
        "savings_by_year": savings,
        "savings_by_year_months": year_months,
        "savings_by_year_notes": year_notes,
        "savings_by_year_dollars": year_dollars_,
        "monthly_by_year": monthly_by_year,
        "months_by_year": months_by_year,
        # every month, ledger start through this one, whose income is
        # incomplete, and the words for one
        "incomplete_months": incomplete,
        "incomplete_label": INCOME_INCOMPLETE,
        "cash_graph": _graph_timeframes(
            conn, today, _cash_graph(conn, income_m, spend_m, today=today)),
        "reported_income": reported,
        "career_income": career,
        "income_by_month": [[p, income_m[p]] for p in sorted(income_m)],
        "investment_funding": funding,
        "brokerage_funding_mtd": ce.get("funding_mtd_total", 0),
        "brokerage_funding_ytd": ce.get("funding_ytd_total", 0),
    }


# A year's wages as Reported income shows them: the return's household
# wages, else the sum of the W-2 wages each person's column holds
# (taxdocs.resum writes those; a year whose return was removed keeps them).
REPORTED_WAGES_SQL = (
    "COALESCE(wages, CASE WHEN primary_wages IS NOT NULL "
    "OR spouse_wages IS NOT NULL THEN COALESCE(primary_wages, 0) "
    "+ COALESCE(spouse_wages, 0) END)")


def reported_income(conn) -> list[list]:
    """The career income reference, newest year first: [year, total
    income (joint if MFJ), wages, investment income, tax paid, joint].
    Wage-only years (W-2s on record, no return) are included, with the
    W-2 wages and a blank Total income, so the reference is complete."""
    return [
        [r["year"], r["total_income"], r["wages"], r["investment_income"],
         r["tax_paid"], r["joint"]]
        for r in conn.execute(
            f"""SELECT year, total_income, w.wages, investment_income,
                       tax_paid, joint
                  FROM income_annual,
                       LATERAL (SELECT {REPORTED_WAGES_SQL} AS wages) w
                 WHERE (total_income IS NOT NULL AND total_income > 0)
                    OR w.wages > 0
                 ORDER BY year DESC""")]


# ---- investment fees ----------------------------------------------------
# investment accounts ONLY — advisory / admin / participant /
# account fees on brokerage, retirement and crypto accounts. Bank, card
# and ATM fees are ordinary spending and belong on Spending; folding them
# in makes the total meaningless next to the balances they erode (this
# report renders as a section of Net Worth).
# word-boundary fee match: '%fee%' also matches coffee/toffee in names AND
# FOOD_AND_DRINK_COFFEE in category_detailed. Pad names with spaces and
# categories with underscores so only the standalone tokens FEE/FEES qualify.
_FEE_WHERE = r"""t.removed=0 AND a.type='investment' AND (
      ('_' || UPPER(COALESCE(t.category_detailed,'')) || '_') LIKE '%\_FEE\_%' ESCAPE '\'
      OR ('_' || UPPER(COALESCE(t.category_detailed,'')) || '_') LIKE '%\_FEES\_%' ESCAPE '\'
      OR (' ' || LOWER(t.name) || ' ') LIKE '% fee %'
      OR (' ' || LOWER(t.name) || ' ') LIKE '% fees %')"""


def _advisory_fee_estimate(conn, today: dt.date | None = None) -> dict[str, dict[str, float]]:
    """Estimated advisory fees by platform and year, for the robo-advised
    accounts the household names in config: `advisory_fees` maps a lowercase
    fragment of the institution's name to the yearly rate as a fraction
    (0.0025 for 0.25%), and `advisory_fee_exempt_masks` lists the account
    masks at those institutions that are self-directed and pay no fee. A
    robo-advisor's exports carry NO fee rows — the cut comes out of the
    balance — so an estimate is the only honest way to show it. Returns
    {platform label: {year: fee}}; nothing when no fee is configured."""
    from . import budget, links
    from .receipts import _like_literal
    cfg = budget.load_config(conn)
    fees = cfg.get("advisory_fees") or {}
    if not isinstance(fees, dict) or not fees:
        return {}
    exempt = [str(m).upper()
              for m in (cfg.get("advisory_fee_exempt_masks") or [])]
    # the shadow exclusion compute_fees applies to the itemized rows: a
    # dual-linked account would otherwise be walked twice and estimate twice
    # the advisory cut on one real balance — and the same entity separation,
    # for the same reason compute_fees applies it
    shadows = links.shadow_ids(conn)
    out: dict[str, dict[str, float]] = {}
    for token, rate in fees.items():
        try:
            rate = float(rate)
        except (TypeError, ValueError):
            continue
        if not str(token).strip() or rate <= 0:
            continue
        rows = conn.execute(
            f"""SELECT a.id, i.institution_name inst
               FROM accounts a JOIN items i ON i.id=a.item_id
               WHERE lower(i.institution_name) LIKE %s ESCAPE '\\'
                 AND a.type='investment'
                 AND NOT (upper(COALESCE(a.mask, '')) = ANY(%s))
                 AND NOT (a.id = ANY(%s))
                 AND ({budget.COMBINE_OR} a.entity_id IS NULL)""",
            # the typed fragment is a name, read literally: its own % and
            # _ are characters to find, not wildcards
            ("%" + _like_literal(str(token).lower()) + "%", exempt,
             shadows)).fetchall()
        if not rows:
            continue
        est = _managed_fee_by_year(conn, [r["id"] for r in rows], rate, today)
        if est:
            out[f"{rows[0]['inst']} advisory (estimated {rate * 100:g}%)"] = est
    return out


def _managed_fee_by_year(conn, managed: list[str], rate: float,
                         today: dt.date | None = None) -> dict[str, float]:
    """`rate` × the average managed value at the start and end of each year,
    reconstructed from holdings and transactions the way the net-worth
    trend is; the current year prorated, years under $1 dropped."""
    # per-account share walk-back (same machinery as the net-worth trend)
    hold_now: dict = {}
    for h in conn.execute("SELECT account_id, symbol, quantity, price FROM holdings"):
        hold_now.setdefault(h["account_id"], {})[h["symbol"]] = [h["quantity"] or 0.0, h["price"] or 0.0]
    obs: dict = {}
    deltas: dict = {}
    first = None              # earliest managed-account ledger date, from the same rows
    for r in conn.execute(
            "SELECT account_id, date, raw FROM transactions "
            "WHERE removed=0 AND account_id = ANY(%s)", (managed,)):
        d_iso = _iso(r["date"])
        if first is None or d_iso < first:
            first = d_iso
        s = _security(r["raw"])
        if not s or not s[0]:
            continue
        deltas.setdefault((r["account_id"], s[0]), []).append((d_iso, s[1]))
        if s[2] and s[2] > 0:
            obs.setdefault(s[0], []).append((d_iso, s[2]))
    today_d = today or dt.date.today()
    today = today_d.isoformat()
    for posn in hold_now.values():
        for tk, (sh, pr) in posn.items():
            if pr and pr > 0:
                obs.setdefault(tk, []).append((today, pr))
    curve = {tk: sorted(set(o)) for tk, o in obs.items()}
    curve_dates = {tk: [d for d, _ in c] for tk, c in curve.items()}
    # sorted deltas + suffix sums: shares after a date in one bisect (the
    # same device _reconstruct_trend uses) instead of re-summing per year
    delta_dates: dict = {}
    delta_after: dict = {}
    acct_tickers: dict = {}
    for key, dl in deltas.items():
        acct_tickers.setdefault(key[0], set()).add(key[1])
        dl.sort(key=lambda x: x[0])
        delta_dates[key] = [d for d, _ in dl]
        acc = [0.0] * (len(dl) + 1)
        for i in range(len(dl) - 1, -1, -1):
            acc[i] = acc[i + 1] + dl[i][1]
        delta_after[key] = acc

    def price(tk, date):
        c = curve.get(tk)
        if not c:
            return None
        i = bisect.bisect_right(curve_dates[tk], date)
        if i == 0:
            return c[0][1]
        if i >= len(c):
            return c[-1][1]
        (d0, p0), (d1, p1) = c[i - 1], c[i]
        span = (dt.date.fromisoformat(d1) - dt.date.fromisoformat(d0)).days or 1
        return p0 + (p1 - p0) * ((dt.date.fromisoformat(date) - dt.date.fromisoformat(d0)).days / span)

    if not first:
        return {}

    def managed_value(date):
        tot = 0.0
        for aid in managed:
            posn = hold_now.get(aid, {})
            tks = acct_tickers.get(aid, set()) | set(posn)
            for tk in tks:
                dd = delta_dates.get((aid, tk))
                after = (delta_after[(aid, tk)][bisect.bisect_right(dd, date)]
                         if dd else 0.0)
                sh = posn.get(tk, [0.0, 0.0])[0] - after
                if abs(sh) < 1e-6:
                    continue
                p = price(tk, date)
                if p is not None:
                    tot += sh * p
        return tot

    est = {}
    y0, y1 = int(first[:4]), today_d.year
    prev = managed_value(f"{y0 - 1}-12-31")
    # a ledger that opens at funding held nothing before it: the first
    # year runs from that day at the funded value, not a whole year
    # averaged up from $0
    opened = None
    if abs(prev) < 1:
        # it opens on the first day it holds a security, not its first
        # row: a cash deposit that funds the buys days later manages
        # nothing, and opening there averages the first year up from $0
        for d in sorted({d for dd in delta_dates.values() for d in dd}):
            v = managed_value(d)
            if abs(v) >= 1:
                opened, prev, y0 = dt.date.fromisoformat(d), v, int(d[:4])
                break
        else:
            opened, prev = dt.date.fromisoformat(first), managed_value(first)
    for y in range(y0, y1 + 1):
        end = min(f"{y}-12-31", today)
        cur = managed_value(end)
        # elapsed days against the year's real length — a fixed 365 reads
        # 366/365 on a leap year's Dec 31 and overstates the fee
        start = opened if opened and opened.year == y else dt.date(y, 1, 1)
        frac = (((dt.date.fromisoformat(end) - start).days + 1)
                / (366 if calendar.isleap(y) else 365))
        fee = rate * ((prev + cur) / 2) * frac
        if fee >= 1:
            est[str(y)] = round(fee, 2)
        prev = cur
    return est


def compute_fees(conn, today: dt.date | None = None) -> dict:
    """Investment fee drag by platform and by year (see `_FEE_WHERE`).

    Summed SIGNED, not ABS: positive is money out (the house convention), so
    a refunded or rebated fee arrives negative and belongs NETTED off the
    drag. ABS would turn a $25 reversal into $25 more drag — answering "what
    did the platform charge and give back" with the sum of both."""
    # Investment accounts are a handful; naming their ids lets the ledger be
    # read by account id (an index probe over the investment rows) instead of
    # a full scan joined to accounts. _FEE_WHERE keeps the type test, and its
    # literal LIKE percents are doubled because a parameter now rides along.
    # Shadow exclusion, the same one _live_accounts and _reconstruct_trend
    # apply to the balances beside this number: a brokerage reached through
    # two aggregators is two account rows carrying one fee history, so
    # summing over both reports twice what the platform charged.
    from . import budget, links
    shadows = links.shadow_ids(conn)
    # and the entity separation every sibling aggregate applies: a
    # brokerage assigned to a business is business money, out of the
    # personal fee picture unless the household combines entities
    inv_ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM accounts WHERE type='investment'"
        f" AND NOT (id = ANY(%s))"
        f" AND ({budget.COMBINE_OR} entity_id IS NULL)", (shadows,))]
    fee_where = _FEE_WHERE.replace("%", "%%")
    by_platform = conn.execute(
        f"""SELECT i.institution_name inst, {_R2.format('SUM(t.amount)')} amt, COUNT(*) n
           FROM transactions t JOIN accounts a ON a.id=t.account_id JOIN items i ON i.id=a.item_id
           WHERE t.account_id = ANY(%s) AND {fee_where}
           GROUP BY inst ORDER BY amt DESC""", (inv_ids,)).fetchall()
    by_year = {r["yr"]: r["amt"] for r in conn.execute(
        f"""SELECT to_char(t.date,'YYYY') yr, {_R2.format('SUM(t.amount)')} amt
           FROM transactions t JOIN accounts a ON a.id=t.account_id
           WHERE t.account_id = ANY(%s) AND {fee_where}
           GROUP BY yr""", (inv_ids,))}
    advisory = _advisory_fee_estimate(conn, today=today)
    for est in advisory.values():
        for y, v in est.items():
            by_year[y] = round(by_year.get(y, 0) + v, 2)
    platforms = [[r["inst"], r["amt"], r["n"]] for r in by_platform]
    # what each row's count counts, said beside it (specs/shown-figures.md):
    # fee rows are charges, an estimate covers years
    counted = [f"{r['n']} charge{'' if r['n'] == 1 else 's'}"
               for r in by_platform]
    for label, est in advisory.items():
        platforms.append([label, round(sum(est.values()), 2), len(est)])
        counted.append(f"{len(est)} year{'' if len(est) == 1 else 's'}, "
                       "estimated")
    if advisory:
        order = sorted(range(len(platforms)), key=lambda i: -platforms[i][1])
        platforms = [platforms[i] for i in order]
        counted = [counted[i] for i in order]
    return {
        "by_platform": platforms,
        "platform_counts": counted,
        "by_year": sorted(([y, v] for y, v in by_year.items())),
        "total": round(sum(p[1] for p in platforms), 2),
    }


# ---- interest & fees paid: what holding cash and carrying a card costs ----

# A row that is a bank or card charging for the account itself — a fee, or
# interest on a carried balance — rather than a purchase. The aggregator's
# BANK_FEES family (interest charge, ATM, overdraft, foreign transaction,
# late/other) by tag, older imports by their legacy primary, and the words a
# statement prints when no tag arrived. Interest EARNED is income and never
# lands here: an INCOME primary is excluded outright, so "Interest Paid" on
# a savings account stays on the income side of the page.
#
# Read per PART of a hand-split row (categories.SPLIT_JOIN in the query):
# a part is a fee exactly when the person put it under BANK_FEES, and the
# bank's tag and statement words speak only for an unsplit row — a fee row
# split into fee and goods is only its fee part, and a fee folded into a
# purchase counts once split out.
_CASH_COST_WHERE = (r"""t.removed=0 AND t.pending=0 AND t.account_id = ANY(%s)
  AND UPPER(COALESCE(t.category_override, t.category_primary, ''))
      NOT IN ('INCOME','TRANSFER_IN','TRANSFER_OUT','LOAN_PAYMENTS')
  AND (UPPER(COALESCE(""" + _categories.PART_CAT + r""", ''))
           IN ('BANK_FEES','BANK FEES')
       OR (sp.line IS NULL AND (
           UPPER(COALESCE(t.category_detailed,'')) LIKE 'BANK\_FEES%%' ESCAPE '\'
        OR UPPER(COALESCE(t.category_detailed,'')) = 'BANK FEES'
        OR (' ' || LOWER(t.name) || ' ') LIKE '%% interest charge%%'
        OR (' ' || LOWER(t.name) || ' ') LIKE '%% purchase interest %%'
        OR (' ' || LOWER(t.name) || ' ') LIKE '%% finance charge %%')))""")
_INTEREST_WORDS = ("interest charge", "purchase interest", "finance charge",
                   "interest charged")


def _cash_account_ids(conn, *,
                      types: tuple[str, ...] = ("depository", "credit"),
                      ) -> list[str]:
    """Depository and credit accounts at a real bank — the accounts whose
    fees and interest are the cost of holding cash and carrying a balance.
    An investment provider's accounts are out: their fee drag is
    compute_fees' subject, and counting a custodian fee here would report
    it twice on one page. The provider is told by its name, not by the
    login holding an investment account: a bank that also offers a
    brokerage account in the same login is still the bank whose overdraft
    and card interest the household paid. `types` narrows the set — the
    interest-earned figure beside the costs reads the depository half of
    this same set, so "net vs earned" compares the same accounts."""
    from . import links
    shadows = links.shadow_ids(conn)
    return [r["id"] for r in conn.execute(
        f"""SELECT a.id FROM accounts a JOIN items i ON i.id=a.item_id
            WHERE a.type = ANY(%s)
              AND LOWER(COALESCE(i.institution_name,'')) NOT LIKE ALL(%s)
              AND NOT (a.id = ANY(%s))
              AND ({budget.COMBINE_OR} a.entity_id IS NULL)""",
        (list(types), _INV_PATTERNS, shadows))]


def cash_cost_kind(name: str, detailed: str) -> str:
    """'interest' for a carried-balance charge, 'fee' for everything else the
    bank charged for the account."""
    if (detailed or "").upper() == "BANK_FEES_INTEREST_CHARGE":
        return "interest"
    low = " " + (name or "").lower() + " "
    return "interest" if any(w in low for w in _INTEREST_WORDS) else "fee"


def cash_cost_rows(conn, since: dt.date | None = None) -> list[dict]:
    """Every fee and interest charge (and their refunds — negative rows) on
    the household's bank and card accounts, newest first, each classified.
    `since` bounds the scan for the daily edge check."""
    ids = _cash_account_ids(conn)
    if not ids:
        return []
    excl, ex_p = excluded_accounts_sql(conn)
    bound = "AND t.date >= %s" if since else ""
    params: tuple = (ids, *((since,) if since else ()), *ex_p)
    rows = conn.execute(
        f"""SELECT t.id, t.date, {_categories.PART_AMOUNT} AS amount,
                   t.name, t.account_id,
                   COALESCE(t.category_detailed,'') AS detailed,
                   {DISPLAY_MERCHANT} payee, a.name AS account
              FROM transactions t {MC_JOIN} {SPLIT_JOIN}
              JOIN accounts a ON a.id = t.account_id
             WHERE {_CASH_COST_WHERE} {bound} {excl}
             ORDER BY t.date DESC, t.amount DESC""", params).fetchall()
    out = []
    for r in rows:
        out.append({"id": r["id"], "date": as_date(r["date"]),
                    "amount": float(r["amount"] or 0),
                    "name": (r["payee"] or r["name"] or "").strip(),
                    "account": r["account"] or "", "account_id": r["account_id"],
                    "kind": cash_cost_kind(r["name"], r["detailed"])})
    return out


def compute_cash_costs(conn, today: dt.date | None = None) -> dict:
    """Interest and fees paid, by year: what holding money and carrying a
    balance cost, netted against the refunds the bank gave back (an ATM
    fee reimbursed is not a fee paid). The interest-earned figure for the
    current year is beside it so the page can say whether the household's
    cash costs more than it pays. Empty `by_year` means nothing to show."""
    today = today or dt.date.today()
    rows = cash_cost_rows(conn)
    years: dict[str, dict] = {}
    interest_ever = 0.0
    latest = None
    for r in rows:
        y = str(r["date"].year)
        b = years.setdefault(y, {"year": y, "interest": 0.0, "fees": 0.0,
                                 "refunds": 0.0, "net": 0.0, "biggest": None})
        amt = r["amount"]
        if amt < 0:
            b["refunds"] += -amt
            # a reversed interest charge cost nothing, so it comes off the
            # all-time figure as it does off its year's net
            if r["kind"] == "interest":
                interest_ever += amt
        elif r["kind"] == "interest":
            b["interest"] += amt
            interest_ever += amt
        else:
            b["fees"] += amt
        b["net"] += amt
        if amt > 0 and (b["biggest"] is None or amt > b["biggest"][1]):
            b["biggest"] = [r["name"], round(amt, 2), r["date"].isoformat(),
                            r["kind"]]
        if amt > 0 and latest is None:
            latest = [r["name"], round(amt, 2), r["date"].isoformat(),
                      r["kind"], r["account"]]
    by_year = [{k: (round(v, 2) if isinstance(v, float) else v)
                for k, v in b.items()}
               for b in sorted(years.values(), key=lambda b: b["year"],
                               reverse=True)]
    # each year as it is printed (specs/shown-figures.md): whole dollars
    # in which interest + fees − refunds give the net, and the largest
    # charge with its full date
    for b in by_year:
        interest, fees, back = shown_dollars(
            [b["interest"], b["fees"], -b["refunds"]], b["net"])
        b["shown"] = {"interest": interest, "fees": fees, "refunds": -back,
                      "net": whole_dollars(b["net"])}
        big = b["biggest"]
        b["biggest_text"] = (
            f"{big[0]} {_money_words(big[1])} · "
            f"{dt.date.fromisoformat(big[2]):%m/%d/%y}" if big else None)
    # interest earned this year, on the same accounts the costs are read
    # from, so "net vs earned" compares like with like (dividends are a
    # brokerage's, not a bank's). Depository only: a card's interest refund
    # is already netted on the cost side as a negative charge.
    bank = _cash_account_ids(conn, types=("depository",))
    excl, ex_p = excluded_accounts_sql(conn)
    earned = conn.execute(
        f"""SELECT COALESCE(SUM({INCOME_NET}),0) AS amt FROM transactions t
             WHERE {_INCOME_WHERE} AND t.date >= %s
               AND (COALESCE(t.category_detailed,'') = 'INCOME_INTEREST_EARNED'
                    OR (' ' || LOWER(t.name) || ' ') LIKE '%% interest %%')
               {excl}""",
        (bank, dt.date(today.year, 1, 1), *ex_p)).fetchone()["amt"]
    ytd = next((b for b in by_year if b["year"] == str(today.year)), None)
    earned_ytd = round(float(earned or 0), 2)
    paid_d = ytd["shown"]["net"] if ytd else 0
    earned_d = whole_dollars(earned_ytd)
    return {"by_year": by_year, "ytd": ytd,
            # what interest and fees cost this year, net of refunds — $0
            # with nothing charged; the tile prints it
            "paid_ytd": round(ytd["net"], 2) if ytd else 0.0,
            "interest_ever": round(max(0.0, interest_ever), 2),
            "earned_ytd": earned_ytd,
            # interest earned on cash this year less what interest and fees
            # cost this year: the "vs interest earned on cash" figure
            "vs_earned_ytd": round(earned_ytd - (ytd["net"] if ytd else 0.0),
                                   2),
            # the tiles as printed: "vs interest earned" is the one
            # printed figure less the other
            "shown": {"paid_ytd": paid_d, "earned_ytd": earned_d,
                      "vs_earned_ytd": earned_d - paid_d},
            "latest": latest}


REPORTS = {
    "networth": compute_networth,
    "spending": compute_spending,
    "cashflow": compute_cashflow,
    "fees": compute_fees,
    "cash_costs": compute_cash_costs,
}
