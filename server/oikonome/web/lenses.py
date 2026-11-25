"""Timeframe lenses (Month / Year) over the frozen engine.

Every lens is a VIEW composed from `budget.month_status` outputs — the
verdict semantics, spend definition and bucket math change ZERO here. The
compose functions return JSON-safe dicts and are the single source for BOTH
the /api/lens/* endpoints and the monthly/yearly email bodies: an email
renderer calls the same function the SPA fetches, so the two surfaces
cannot diverge (the same single-source pattern todayview.py uses for the
Today card and its email).

Composition rules:

* MONTH — the report card. Past months are FINAL (as-of = month end, so
  frac=1 and the engine's own verdict IS the final verdict); the current
  month is in-progress with a pace projection (the same projection the
  Today card shows, judged with the month-end tolerance).

* YEAR — 12 month cells (final verdict + margin; the current month shows
  the projection; future months blank) + annual totals + category YoY.
  Closed months are judged against their frozen budget SNAPSHOT
  (budget_snapshots, written nightly for the current month); a closed
  month with no snapshot gets NO verdict — the product wasn't budgeting
  then — and shows net income vs spending instead (status "net"), which
  is a cash-flow fact, not a budget verdict.

Past months run month_status(historical=False) DELIBERATELY: the report
card needs the fixed/variable split ("bills paid vs planned"). The schedule
projected backward is the month's FROZEN one when its snapshot carries bill
rows; only months predating bill freezing fall back to the live table as
the best available decomposition.
The engine already guards fabrication: occurrences before a schedule-only
bill's floor are excluded from expectations unless a real payment matched.
(gather's historical=True rule is about the DAILY pipeline never
projecting bills onto past dates; a report card wants the opposite.)

Amount sign follows the engine: positive = money out.
"""

from __future__ import annotations

import calendar
import datetime as dt
import logging

log = logging.getLogger("oikonome.lenses")

from ..engine import budget, coverage, merchant_sql, reporting
from ..engine import categories as _categories

# ---- shared helpers ---------------------------------------------------------

_R = lambda v: round(v, 2)  # noqa: E731 — payload rounding, one name


def _verdict(variance: float, tolerance: float) -> str:
    """The engine's frozen verdict formula (budget.month_status), reused
    verbatim at other scopes — no new judgment rules."""
    return ("ON BUDGET" if abs(variance) <= tolerance
            else "OVER BUDGET" if variance > 0 else "UNDER BUDGET")


def _snapshot_config(conn, y: int, m: int, memo: dict | None) -> dict | None:
    """budget.snapshot_config for (y, m), read once per lens request: the
    month's state and the exclusion fragment below must see the SAME
    frozen settings, and a year page asks for them month by month."""
    key = ("lens_snapshot_config", y, m)
    if memo is not None and key in memo:
        return memo[key]
    snap = budget.snapshot_config(conn, y, m)
    if memo is not None:
        memo[key] = snap
    return snap


def lens_excluded_sql(conn, today: dt.date, *, memo: dict | None = None,
                      since: dt.date | None = None,
                      until: dt.date | None = None) -> tuple[str, tuple]:
    """The lenses' account-exclusion predicate over `transactions t`, as a
    (SQL fragment, params) pair spliced where reporting.excluded_accounts_sql
    would be.

    The Month and Year lenses read a closed month AS IT CLOSED: a month
    with a budget snapshot is judged under the snapshot's settings
    (month_state), and the accounts excluded then are part of those
    settings. So every lens figure for that month — spend, income, the
    year's totals and category rows, their drill-downs — leaves out the
    snapshot's accounts, and every other day (the month in progress, a
    closed month with no snapshot) today's. One rule for the whole lens:
    spend read under one list and income under another made an account
    excluded since keep its outflows in a month and lose its deposits.
    Cash Flow deliberately reads every month under today's list instead
    (reporting.excluded_accounts_sql).

    When no closed snapshot differs from today's list the fragment is
    exactly reporting's, so the common case costs nothing extra.
    `since`/`until` bound which months' snapshots are read, for a caller
    whose query is bounded to them (the fragment is then only valid
    inside that span)."""
    key = ("lens_excluded_sql", today, since, until)
    if memo is not None and key in memo:
        return memo[key]
    live = budget.excluded_account_ids(budget.load_config(conn, memo=memo))
    arms: list[list] = []          # [first, exclusive end, ids]
    # every closed snapshot in ONE read (a household with years of them
    # paid a round trip each), each cleaned exactly as snapshot_config
    # cleans it and filed in the memo month_state reads
    for r in conn.execute(
            "SELECT year, month, config FROM budget_snapshots "
            "WHERE (year, month) < (%s, %s) ORDER BY year, month",
            (today.year, today.month)).fetchall():
        y, m = int(r["year"]), int(r["month"])
        if not (1 <= y <= 9998 and 1 <= m <= 12):
            continue                 # a malformed restored row is no month
        if ((since is not None and budget._month_after(y, m) <= since)
                or (until is not None and dt.date(y, m, 1) >= until)):
            continue
        mkey = ("lens_snapshot_config", y, m)
        if memo is not None and mkey in memo:
            snap = memo[mkey]
        else:
            raw = r["config"]
            snap = None
            if isinstance(raw, dict):
                snap = budget._clean_snapshot_config(raw)
                if not budget._snapshot_budgets_numeric(snap):
                    snap = None
            if memo is not None:
                memo[mkey] = snap
        ids = budget.excluded_account_ids(snap) if snap is not None else live
        if sorted(ids) == sorted(live):
            continue
        first, end = dt.date(y, m, 1), budget._month_after(y, m)
        if arms and arms[-1][1] == first and sorted(arms[-1][2]) == sorted(ids):
            arms[-1][1] = end
        else:
            arms.append([first, end, ids])
    if not arms:
        out = (("AND NOT (t.account_id = ANY(%s))", (live,)) if live
               else ("", ()))
    else:
        whens = " ".join("WHEN t.date >= %s AND t.date < %s "
                         "THEN t.account_id = ANY(%s::text[])" for _ in arms)
        params: list = []
        for first, end, ids in arms:
            params += [first, end, list(ids)]
        params.append(list(live))
        out = (f"AND NOT (CASE {whens} "
               f"ELSE t.account_id = ANY(%s::text[]) END)", tuple(params))
    if memo is not None:
        memo[key] = out
    return out


def _income_year_month(conn, excl: str, ex_p: tuple,
                       since: dt.date | None = None,
                       until: dt.date | None = None) -> tuple[dict, dict]:
    """reporting._income_year_month — the same bank-income definitions and
    the same one GROUPING SETS pass — under the lens's exclusion fragment
    (lens_excluded_sql) instead of today's list."""
    bound, b_p = "", []
    if since is not None:
        bound += " AND t.date >= %s"
        b_p.append(since)
    if until is not None:
        bound += " AND t.date < %s"
        b_p.append(until)
    years: dict = {}
    months: dict = {}
    for r in conn.execute(
            f"SELECT to_char(t.date,'YYYY') yr, to_char(t.date,'YYYY-MM') p, "
            f"{reporting._R2.format(f'SUM({reporting.INCOME_NET})')} amt "
            f"FROM transactions t WHERE {reporting._INCOME_WHERE}{bound} {excl} "
            f"GROUP BY GROUPING SETS ((yr), (p))",
            (reporting._bank_account_ids(conn), *b_p, *ex_p)):
        if r["p"] is None:
            years[r["yr"]] = r["amt"]
        else:
            months[r["p"]] = r["amt"]
    return years, months


def month_state(conn, y: int, m: int, today: dt.date, *,
                 memo: dict | None = None) -> dict:
    """month_status for (y, m) with the fullest as-of date available:
    today for the current month, month end for past months, the 1st for
    future months (plan only — no rows exist yet).

    Closed months are judged against their frozen budget snapshot when one
    exists (`budget_source` 'snapshot') — the budget the month actually ran
    under, not today's numbers applied retroactively. With no snapshot the
    live config applies, tagged 'live' so surfaces can say so. The bill
    SCHEDULE follows the same rule (`bills_source`): frozen rows when the
    snapshot carries them, the live table for months that predate bill
    freezing.

    `memo` is the lens request's engine cache (budget._memo_key): the live
    config, the bill schedules and every spend window the twelve months of
    a year page share are read once, not once per month — and the month's
    state itself, which the yearly mail's detail walk asks for again
    (callers only read it)."""
    key = ("lens_month_state", y, m, today)
    if memo is not None and key in memo:
        return memo[key]
    last = calendar.monthrange(y, m)[1]
    snap = frozen = None
    if (y, m) == (today.year, today.month):
        as_of = today
    elif (y, m) > (today.year, today.month):
        as_of = dt.date(y, m, 1)
    else:
        as_of = dt.date(y, m, last)
        snap = _snapshot_config(conn, y, m, memo)
        frozen = budget.snapshot_bill_rows(conn, y, m)
    st = budget.month_status(conn, as_of, historical=False, cfg=snap,
                             bill_rows=frozen, memo=memo,
                             closed=(y, m) < (today.year, today.month))
    st["as_of"] = as_of
    st["budget_source"] = "snapshot" if snap is not None else "live"
    st["bills_source"] = "snapshot" if frozen is not None else "live"
    st["bill_rows"] = frozen    # the schedule the month was judged under
    st["snapshot_config"] = snap   # …and the budget (None = the live one)
    if memo is not None:
        memo[key] = st
    return st


def _projection(st: dict) -> dict:
    """Month-end pace projection for an in-progress month, judged against
    the month's spendable — the budget's paced share, the whole variable
    budget unless the ledger began inside the month — with the frozen
    tolerance rule (at month end, expected = that spendable, so
    tolerance = max($50, 5% of it)).

    The window is the verdict's own (budget.pace_window): the pace is
    spend through yesterday over the finished days since the pace start
    (budget.paced_daily), carried over every paced day of the month.
    Today has not finished, so counting it as a completed $0 day would
    project an on-pace household as under budget while the verdict says
    on budget; the days before the ledger are no data, not days of no
    spending. Until the window is settled (the month's first days, a
    ledger days old) the verdict calls nothing under, so the projection
    is the plan itself — unless even that pace runs over, which the
    verdict says at once too."""
    var_budget = st["variable_budget_paced"]
    daily = budget.paced_daily(st)
    pace_total = (daily * st["pace"]["paced_days"] if daily is not None
                  else var_budget)
    variance = pace_total - var_budget
    tolerance = max(50.0, var_budget * 0.05)
    verdict = _verdict(variance, tolerance)
    if not st["pace_settled"] and verdict != "OVER BUDGET":
        pace_total, variance, verdict = var_budget, 0.0, "ON BUDGET"
    return {"pace_total": _R(pace_total), "variance": _R(variance),
            "tolerance": _R(tolerance), "verdict": verdict}


def _paced(st: dict, amount: float) -> float:
    """A variable budget figure at the month's spendable share. A ledger
    that began inside the month may spend only the budget's share from
    its first day to month end (budget.pace_window); the hero judges
    that share (variable_budget_paced), so every row beneath it — a
    bucket, its remainder, a carve-out — and every sum of months reads
    the same share, or the rows add up to money the hero says the month
    never had. Bills are not paced: they are owed on their schedule."""
    return amount * st.get("month_share", 1.0)


# What a month that gets no verdict says: the pill, and the sentence under
# it. Written here once; the page, the phone and the monthly email print
# them (specs/budget-snapshots.md, "A month's standing").
STANDING_WORDS = {
    "no_budget": ("no budget set",
                  "No budget saved yet, so this month gets no verdict — "
                  "set one on the Budget page."),
    "before_budget": ("no budget then",
                      "This month is from before you had a budget, so it "
                      "gets no verdict: what came in, what went out and "
                      "the difference."),
    "no_data": ("no data", "No data for this month."),
}
# standings of a closed month no budget of its own stands behind
PAST_STANDINGS = ("before_budget", "no_data")


def month_standing(conn, y: int, m: int, today: dt.date, *,
                   memo: dict | None = None) -> str:
    """Whether a budget of the household's own stands behind (y, m): the
    ONE answer the Month view, the Year grid and both report emails read.

    - "judged": a closed month has its frozen snapshot (snapshots are only
      taken of a saved budget; a row that does not read as one is none);
      the month in progress, or a future one, has a saved budget
      (budget.budgets_set, never the loader's unsaved first-month guess);
    - "no_budget": the month in progress, or a future one, with none;
    - "before_budget": a closed month with no snapshot. It is never judged
      against today's budget, and today's bills are never laid over it;
    - "no_data": such a month with no row dated in it that its figures
      could count (_months_with_rows) — it says so, and is never a verdict
      or a row of zeros."""
    key = ("lens_month_standing", y, m, today)
    if memo is not None and key in memo:
        return memo[key]
    if (y, m) >= (today.year, today.month):
        bkey = ("lens_budgets_set",)
        if memo is not None and bkey in memo:
            saved = memo[bkey]
        else:
            saved = budget.budgets_set(conn)
            if memo is not None:
                memo[bkey] = saved
        out = "judged" if saved else "no_budget"
    elif _snapshot_config(conn, y, m, memo) is not None:
        out = "judged"
    elif m in _months_with_rows(conn, y, memo):
        out = "before_budget"
    else:
        out = "no_data"
    if memo is not None:
        memo[key] = out
    return out


def _months_with_rows(conn, y: int, memo: dict | None) -> set[int]:
    """The months of `y` with a row dated in them that the month's figures
    could count: a live row (pending included, as the spending figures
    include it) on a household account the budget math reads — not on an
    account the household excluded, a linked shadow of another, or a
    business's. A month without one has no data, whatever the totals over
    it add to."""
    key = ("lens_months_with_rows", y)
    if memo is not None and key in memo:
        return memo[key]
    from ..engine import links
    excluded = (budget.excluded_account_ids(budget.load_config(conn))
                + links.shadow_ids(conn))
    out = {r["m"] for r in conn.execute(
        "SELECT DISTINCT EXTRACT(MONTH FROM t.date)::int AS m"
        "  FROM transactions t WHERE t.removed = 0"
        "   AND t.date >= %s AND t.date < %s"
        "   AND NOT (t.account_id = ANY(%s))"
        f"  {budget.PERSONAL_ONLY_SQL}",
        (dt.date(y, 1, 1), dt.date(y + 1, 1, 1), excluded)).fetchall()}
    if memo is not None:
        memo[key] = out
    return out


def cell_words(cell: dict, standing: str) -> dict:
    """What a Year cell prints, so no client decides it: `label` (the
    pill of a cell that is not a verdict), `figure` (the whole-dollar
    figure under it), `tone` and `note`.

    A cell never prints its spend under the word "net": with income the
    bank feed does not measure (`income` None — MeasuredWindow
    .month_known, the rule of specs/counted-figures.md) the cell says
    "spent" and carries the note "income incomplete"; a month with no
    data says "no data" and prints no figure. A verdict cell keeps its
    verdict and says its margin in words ("over by $X", "under by $X",
    "on budget"): on the Year page only a net is signed
    (specs/budget-snapshots.md, "The Year page's words")."""
    from . import charts
    out = {"label": None, "figure": None, "tone": None, "note": None}
    if cell["status"] == "future":
        return out
    if standing == "no_data":
        return {**out, "label": "no data", "tone": "mut"}
    if cell.get("income") is None:
        out["note"] = reporting.INCOME_INCOMPLETE
    if cell["status"] != "net":
        v = cell.get("variance")
        if v is None:
            return out
        r = round(v)
        if r == 0:
            return {**out, "figure": "on budget", "tone": "pos"}
        return {**out, "tone": "neg" if r > 0 else "pos",
                "figure": f"{'over' if r > 0 else 'under'} by "
                          f"{charts._money(abs(r))}"}
    net = cell.get("net")
    if net is None:
        return {**out, "label": "spent", "tone": "mut",
                "figure": charts._money(cell.get("spend") or 0)}
    r = round(net)
    if r == 0:
        # what prints is $0: even, neither a gain nor a loss, whichever
        # side of zero the cents fell
        return {**out, "label": "net", "tone": "mut", "figure": "$0"}
    return {**out, "label": "net", "tone": "pos" if r > 0 else "neg",
            "figure": f"−${-r:,}" if r < 0 else f"+${r:,}"}


def _unbudgeted_state(conn, y: int, m: int, *,
                      memo: dict | None = None) -> dict:
    """month_status for a closed month from before the household had a
    budget, read with an EMPTY bill schedule: nothing is planned, due,
    awaiting or scheduled in a month the bills did not exist in, and every
    row is spending of the day it is dated. Only the Month view's card
    reads this; the pages that open a closed month's rows keep
    month_state."""
    key = ("lens_unbudgeted_state", y, m)
    if memo is not None and key in memo:
        return memo[key]
    as_of = dt.date(y, m, calendar.monthrange(y, m)[1])
    st = budget.month_status(conn, as_of, historical=False, bill_rows=[],
                             memo=memo, closed=True)
    st["as_of"] = as_of
    st["budget_source"] = st["bills_source"] = "live"
    st["bill_rows"] = []
    st["snapshot_config"] = None
    if memo is not None:
        memo[key] = st
    return st


def _ser_txn(r, fixed: bool, bill_payments: dict | None = None) -> dict:
    return {"date": r["date"].isoformat(), "amount": _R(r["amount"]),
            # a bill's payment under the bill's name, as Bills lists it;
            # every other row as the ledger names it — the raw descriptor
            # is only in the opened transaction
            "payee": ((bill_payments or {}).get(r.get("txn_id"))
                      or merchant_sql.shown_payee(r)),
            "category": r["category"],
            "account": r.get("account"), "pending": bool(r["pending"]),
            "fixed": fixed}


def _variable_rows(st: dict) -> list[dict]:
    """The month's everyday rows as the engine split them: the food and
    other buckets' rows (carve-outs sit inside their parent's list), an
    envelope's spill as its own slice."""
    return list(st["buckets"]["food"].get("rows") or []) + \
        list(st["buckets"]["other"].get("rows") or [])


def _with_items(conn, txns: list[dict]) -> list[dict]:
    """Each serialised row gains `items`: what was bought, for the
    merchants whose charge never says — an Amazon order's refined item
    summary, a Costco receipt's line descriptions. Empty for every other
    row. One query per source for the whole list; a lookup failure leaves
    the rows without items rather than failing the lens."""
    import json as _json
    ids = [t["txn_id"] for t in txns if t.get("txn_id")]
    items: dict[str, list[str]] = {}
    if ids:
        try:
            for r in conn.execute(
                    """SELECT am.transaction_id AS id, s.summary,
                              o.items_json
                         FROM amazon_matches am
                         LEFT JOIN amazon_summaries s ON s.dedup_key = am.dedup_key
                         LEFT JOIN amazon_orders o ON o.dedup_key = am.dedup_key
                        WHERE am.transaction_id = ANY(%s)""", (ids,)).fetchall():
                names: list[str] = []
                if r["summary"]:
                    names = [x.strip() for x in str(r["summary"]).split(" · ")
                             if x.strip()]
                elif r["items_json"]:
                    raw = r["items_json"]
                    if isinstance(raw, str):
                        raw = _json.loads(raw or "[]")
                    names = [str(i.get("title") or "")[:60].strip()
                             for i in (raw or []) if isinstance(i, dict)]
                if names:
                    items.setdefault(r["id"], []).extend(names)
            for r in conn.execute(
                    """SELECT cm.transaction_id AS id, cr.items_json, cr.summary
                         FROM costco_matches cm
                         JOIN costco_receipts cr ON cr.dedup_key = cm.dedup_key
                        WHERE cm.transaction_id = ANY(%s)""", (ids,)).fetchall():
                raw = r["items_json"]
                if isinstance(raw, str):
                    raw = _json.loads(raw or "[]")
                names = []
                for i in raw or []:
                    if not isinstance(i, dict):
                        continue
                    d = str(i.get("description") or "").strip()
                    if d:
                        d = d.title()
                        if d not in names:
                            names.append(d)
                if not names and r["summary"]:
                    names = [str(r["summary"])]
                if names:
                    items.setdefault(r["id"], []).extend(names)
        except Exception:                                # noqa: BLE001
            log.warning("items for the largest transactions skipped",
                        exc_info=True)
    out = []
    for t in txns:
        t = dict(t)
        t["items"] = items.get(t.pop("txn_id", None) or "", [])
        out.append(t)
    return out


def _variable_ser(r: dict) -> dict:
    """A variable row serialised with the id the items lookup follows: a
    spill carries the purchase it was carved from under `overflow_of`."""
    return {**_ser_txn(r, False),
            "txn_id": r.get("txn_id") or r.get("overflow_of")}


HOUSING_LABEL = "Mortgage, rent and utilities"


def ring_label(category, detailed=None) -> str:
    """The label a category takes in the reports' spending ring. The
    feed files the mortgage under loan payments and the rent, power and
    water under rent and utilities; a reader asking where the money
    went wants the roof and what keeps it lit as one answer, so both
    read as one housing slice. Other loan payments (a car, a student
    loan) keep their own label — the detailed category tells them
    apart — and everything else is its category as the ledger shows it."""
    c = str(category or "")
    if c == "RENT AND UTILITIES":
        return HOUSING_LABEL
    if c == "LOAN PAYMENTS" and detailed == "LOAN_PAYMENTS_MORTGAGE_PAYMENT":
        return HOUSING_LABEL
    return c


def _by_category(rows) -> tuple[list, list, float]:
    """MTD-by-category with Amazon subcategories clustered under one
    parent — the todayview.build_context grouping, at any scope.

    Each entry is [label, amount, key]: the label is the DISPLAY form
    ("FOOD AND DRINK"), the key the value the ledger stores and compares
    ("FOOD_AND_DRINK"), so a client can turn the row it renders into the
    filter that opens it. The clustered "Amazon" parent has no key — it
    is several stored categories added up, and no single filter lists it;
    its children carry their own override strings. "?" is the key for
    rows with no category at all, and a composed label (envelope
    overflow) has none, because nothing stores it.
    """
    by_cat: dict[str, float] = {}
    keys: dict[str, str | None] = {}
    for r in rows:
        lab, key = budget.listed_category(r)
        by_cat[lab] = by_cat.get(lab, 0.0) + r["amount"]
        if lab not in keys or keys[lab] is None:
            keys[lab] = key
    amazon = {k: v for k, v in by_cat.items() if k.startswith("Amazon")}
    entries = [[k, _R(v), keys.get(k)]
               for k, v in by_cat.items() if k not in amazon]
    if amazon:
        entries.append(["Amazon", _R(sum(amazon.values())), None])
    entries.sort(key=lambda x: -x[1])
    subs = sorted(([k.replace("Amazon - ", ""), _R(v), keys.get(k)]
                   for k, v in amazon.items()), key=lambda x: -x[1])
    return entries, subs, _R(sum(amazon.values()))


def month_category_ledger(st: dict, key: str) -> dict:
    """The rows a month lens category label summed — the `_by_category`
    entry whose key is `key` — and that label's number.

    The label is personal spend as the month verdict counts it: money out
    only, reimbursements netted off, no business rows, bill occurrences and
    envelope rules applied. A category filter over the ledger has none of
    that, so a label with a refund or a business row beside it opened a
    list that did not add up to it. The engine names the rows instead,
    the way a bucket tile's link does."""
    label = next((budget.listed_category(r)[0] for r in st["rows"]
                  if budget.listed_category(r)[1] == key), None)
    rows = [r for r in st["rows"]
            if label is not None and budget.listed_category(r)[0] == label]
    ids: list[str] = []
    seen: set[str] = set()
    for r in rows:
        tid = r.get("txn_id") or r.get("overflow_of")
        if tid and tid not in seen:
            seen.add(tid)
            ids.append(tid)
    return {"txn_ids": ids, "amount": _R(sum(r["amount"] for r in rows))}


def year_category_ledger(conn, key: str, year: int,
                         today: dt.date | None = None) -> dict:
    """The rows a year lens category label summed, and its number: the
    same spend predicate, reimbursement netting and accounts left out
    (lens_excluded_sql, as of the household's `today`) as `year_summary`'s
    category totals, matched on the display form the label groups by."""
    from .. import localtime
    today = today or localtime.household_day(conn)
    excl, ex_p = lens_excluded_sql(conn, today, since=dt.date(year, 1, 1),
                                   until=dt.date(year + 1, 1, 1))
    rows = conn.execute(
        f"SELECT t.id, {reporting.PART_NET_JOINED} amt "
        f"FROM transactions t {reporting.REIMB_PARTIAL_JOIN} {reporting.SPLIT_JOIN} "
        f"WHERE {reporting.SPEND_WHERE} "
        f"AND t.date >= %s AND t.date < %s "
        f"AND {reporting.PART_CAT} = REPLACE(%s, '_', ' ') {excl}",
        (dt.date(year, 1, 1), dt.date(year + 1, 1, 1), key, *ex_p)).fetchall()
    # a hand-split row is listed once however many of its parts matched
    # (one per category by construction, so at most once here)
    return {"txn_ids": list(dict.fromkeys(r["id"] for r in rows)),
            "amount": _R(sum(r["amt"] or 0 for r in rows))}


def year_bucket_ledger(conn, bucket: str, year: int,
                       today: dt.date | None = None) -> dict | None:
    """The rows a YEAR money map label counted, and its number: that bucket
    over the year's elapsed BUDGETED months — the months `year_summary`
    sums into `buckets_annual` (a closed month with no snapshot had no plan
    and contributes nothing there either) — each month's rows named by
    `budget.bucket_ledger` against that month's own state. None when the
    name is a bucket of none of those months, which is the caller's 400.
    `months` names the months summed, so a per-day pace can divide by the
    days those months cover rather than by the whole year's.

    `today` decides which months have elapsed, so it is the HOUSEHOLD's day
    — on the container's UTC clock a west-coast evening on the 31st has
    already opened the next month, and the year's last bucket would be
    summed one month wider than the map that links to it."""
    from .. import localtime
    today = today or localtime.household_day(conn)
    snap_months = budget.snapshot_months(conn, year)
    memo: dict = {}
    budget.share_spend_window(memo, *budget.year_spend_window(year))
    ids: list[str] = []
    seen: set[str] = set()
    amount = 0.0
    label = None
    months: list[int] = []
    for m in range(1, 13):
        if (year, m) > (today.year, today.month):
            continue
        if (year, m) < (today.year, today.month) and m not in snap_months:
            continue
        st = month_state(conn, year, m, today, memo=memo)
        if bucket in ("food", "other"):
            # The year map draws a parent bucket WHOLE — it sends no child
            # rows, so its label is the raw actual, carve-outs included.
            # Its listing counts the same money: the month tile's
            # display-number decomposition would drop the carve-out
            # purchases the year label still adds in.
            whole = st["buckets"][bucket]
            st = {"buckets": {**st["buckets"],
                              bucket: {**whole, "children": []}}}
        info = budget.bucket_ledger(st, bucket)
        if info is None:
            continue
        label = label or info["label"]
        amount += info["amount"]
        months.append(m)
        for t in info["txn_ids"]:
            if t not in seen:
                seen.add(t)
                ids.append(t)
    if label is None:
        return None
    return {"bucket": bucket, "label": label, "amount": _R(amount),
            "txn_ids": ids, "months": months}


def _year_detail(conn, year: int, today: dt.date, memo: dict,
                 snap_months: set[int]) -> dict:
    """See year_summary(with_detail=True). Categories cover every elapsed
    month; the bars' figures (children, envelopes, bills paid) only the
    budgeted ones, like `buckets_annual`."""
    cats: dict[str, float] = {}
    cat_keys: dict[str, str | None] = {}
    fixed_total = 0.0
    # the paid bills by their charges' transaction category, over the
    # same months fixed_total covers, so the report can show the bills
    # by kind instead of as one lump
    bill_cats: dict[str, float] = {}
    bills_paid = 0
    children: dict[str, dict] = {}
    envelopes: dict[str, dict] = {}
    walked = budgeted = 0
    for m in range(1, 13):
        if (year, m) > (today.year, today.month):
            continue
        st = month_state(conn, year, m, today, memo=memo)
        walked += 1
        b = st["buckets"]
        variable_rows = _variable_rows(st)
        for label, amt, key in _by_category(variable_rows)[0]:
            cats[label] = cats.get(label, 0.0) + amt
            cat_keys.setdefault(label, key)
        fixed_total += b["fixed"]["actual"]
        for r in b["fixed"].get("occ_rows") or []:
            if r.get("category"):
                lab = ring_label(r["category"], r.get("category_detailed"))
                bill_cats[lab] = bill_cats.get(lab, 0.0) + r["amount"]
        is_budgeted = ((year, m) == (today.year, today.month)
                       or m in snap_months)
        if not is_budgeted:
            continue
        budgeted += 1
        bills_paid += len(b["fixed"].get("occ_rows") or [])
        for k in ("food", "other"):
            for c in b[k].get("children") or []:
                e = children.setdefault(c["name"], {"actual": 0.0,
                                                     "month_budget": 0.0,
                                                     "parent": k})
                e["actual"] += c["actual"]
                e["month_budget"] += _paced(st, c["month_budget"])
        for env in b["fixed"].get("envelopes") or []:
            e = envelopes.setdefault(env["payee"], {
                "used": 0.0, "monthly": 0.0, "overflow": 0.0,
                "show_today": False})
            e["used"] += env["used"]
            e["monthly"] += env["monthly"]
            e["overflow"] += env["overflow"]
            e["show_today"] = e["show_today"] or bool(env.get("show_today"))
    categories = sorted(([k, _R(v), cat_keys.get(k)] for k, v in cats.items()),
                        key=lambda x: -x[1])
    return {"categories_variable": categories, "fixed_total": _R(fixed_total),
            "categories_bills": sorted(([k, _R(v)] for k, v in bill_cats.items()),
                                       key=lambda x: -x[1]),
            "bills_paid": bills_paid,
            "children": {k: {"actual": _R(v["actual"]),
                             "month_budget": _R(v["month_budget"]),
                             "parent": v["parent"]}
                         for k, v in children.items()},
            "envelopes": {k: {"used": _R(v["used"]), "monthly": _R(v["monthly"]),
                              "overflow": _R(v["overflow"]),
                              "show_today": v["show_today"]}
                          for k, v in envelopes.items()},
            "months_walked": walked, "budgeted_months": budgeted}


def _month_income_actual(conn, year: int, month: int, today: dt.date,
                         memo: dict | None = None) -> float | None:
    """The month's bank income by the Cash Flow definitions — reporting's
    month grain (_income_by), read for this one month only: grouping the
    whole ledger to keep one row made every Month-lens view pay for every
    bank row ever held. The accounts left out are the lens's
    (lens_excluded_sql): a closed month's income drops what its spend
    drops."""
    excl, ex_p = lens_excluded_sql(
        conn, today, memo=memo, since=dt.date(year, month, 1),
        until=budget._month_after(year, month))
    return conn.execute(
        f"SELECT {reporting._R2.format(f'SUM({reporting.INCOME_NET})')} amt "
        f"FROM transactions t WHERE {reporting._INCOME_WHERE} "
        f"AND t.date >= %s AND t.date < %s {excl}",
        (reporting._bank_account_ids(conn), dt.date(year, month, 1),
         budget._month_after(year, month), *ex_p)).fetchone()["amt"]


def _first_year(conn) -> int | None:
    row = conn.execute("SELECT MIN(date) AS d FROM transactions "
                       "WHERE removed = 0").fetchone()
    return row["d"].year if row and row["d"] else None


def _Coverage(conn):
    """Which months have known income: the household's MeasuredWindow
    (engine/coverage.py) — the object Cash Flow reads too, so the lenses
    and that page cannot disagree about which months are known."""
    return coverage.window(conn)


# ---- week lens --------------------------------------------------------------


def _lens_goal(conn, cfg, goal, today: dt.date) -> dict | None:
    """The plan savings goal both lenses show, so the clients need not fetch
    the whole Today payload for one number. Always present (None = no plan
    goal). rate_90d is the figure savings.progress gives the same goal (None
    for a plan-only goal): the clients' estimate when no saved figure."""
    from ..engine import savings as _sv
    if goal is None:
        return None
    rate = None
    if not _sv.is_plan_only(goal):
        since = _sv.pace_since(today)
        cover = _sv._pace_months(conn, cfg, goal, since, today)
        rate = round(_sv.matched_net(conn, cfg, goal, since=since) / cover, 2)
    return {"monthly_plan": round(float(goal.get("monthly_plan") or 0), 2),
            "rate_90d": rate}


def lens_savings_row(goal: dict | None, saved: float | None,
                     months: int = 1, *, projected: str) -> list[dict]:
    """A lens map's Savings row: the standing Savings goal (`goal`, the
    reply's `savings_goal`) at its monthly plan times `months`, against
    what the ledger says was transferred over the same months (`saved`:
    the Month lens's `saved_month`, the Year lens's `saved_ytd`). With no
    such figure the only one is the goal's 3-month pace rate times `months`,
    captioned `projected` so the row never calls it what was transferred;
    a plan-only goal (no rate) carries its plan alone. No row without a
    goal, or with neither a plan nor anything saved."""
    if goal is None or months <= 0:
        return []
    plan = (goal.get("monthly_plan") or 0) * months
    rate = goal.get("rate_90d")
    if saved is not None:
        actual, caption, label = saved, "transferred vs plan", None
    elif rate is not None:
        actual, caption, label = rate * months, projected, None
    else:
        actual, caption, label = 0.0, None, "—"
    if plan <= 0 and actual <= 0:
        return []
    return [{"label": "Savings / Investment", "judged": False,
             "actual": _R(actual), "expected": _R(plan), "budget": _R(plan),
             "actual_label": label, "caption": caption}]


def closed_bill_words(st: dict) -> dict:
    """What a month that is OVER says about its bills, from its
    breakdown (bills_breakdown.closed). Nothing in a closed month is
    "still due" or "awaiting": a bill was paid, paid for less, or not
    paid.

    - `breakdown`: the month's parts;
    - `notes`: the lines the bills card prints — "not paid: NAME $Y" for
      each scheduled occurrence no payment was matched to, except a bill
      with another payment matched in the same month (`paid_bills`),
      which is never also listed as unpaid; how the month's figures add
      up to its plan is the Bills row's table;
    - `status`: the words beside the map's Bills row
      (bills_breakdown.row_status): how the month came out — "$N not
      paid", "$U under plan", "over plan by $X" — or None when there is
      nothing to say.

    One writer for the page, the phone and the monthly email
    (specs/budget-snapshots.md, "A closed month's bills")."""
    from . import bills_breakdown
    try:
        bd = bills_breakdown.closed(st)
    except (TypeError, ValueError, KeyError, IndexError):
        # a stored status this reader cannot parse (a malformed due date
        # in a cached one): the row has no words rather than wrong ones
        import logging
        logging.getLogger("oikonome.lenses").exception(
            "closed month's bills could not be read")
        return {"breakdown": None, "notes": [], "status": None}
    return {"breakdown": bd, "notes": bills_breakdown.card_lines(bd),
            "status": bills_breakdown.row_status(bd)}


def running_breakdown(conn, st: dict, today: dt.date, *,
                      memo: dict | None = None,
                      parts: dict | None = None) -> dict | None:
    """The running month's bills in parts (bills_breakdown.running) — the
    walk read once (`parts`, running_parts), the figure Today's Bills
    row, Budget and the Bills page's "Still due" print. None when it
    cannot be read: the row then has no parts rather than mismatched
    ones."""
    from . import bills_breakdown
    try:
        return bills_breakdown.running(
            conn, budget.load_config(conn, memo=memo), st, today, memo=memo,
            parts=parts)
    except Exception:                    # noqa: BLE001 — the card must render
        import logging
        logging.getLogger("oikonome.lenses").exception(
            "bills breakdown failed on a lens")
        return None


def running_parts(conn, st: dict, today: dt.date, *,
                  memo: dict | None = None) -> dict | None:
    """The running month's month-end figure and its two halves
    (report.month_end_parts), read once for the lens's Bills and
    Envelopes rows. None when it cannot be read."""
    from . import report
    try:
        return report.month_end_parts(
            conn, budget.load_config(conn, memo=memo), today,
            st["buckets"]["fixed"]["envelopes"], memo=memo)
    except Exception:                    # noqa: BLE001 — the card must render
        import logging
        logging.getLogger("oikonome.lenses").exception(
            "month-end figure failed on a lens")
        return None


def envelope_month(st: dict, y: int, m: int, kind: str,
                   parts: dict | None = None) -> dict:
    """A lens month's pools (envelope_breakdown.month) from its own
    state alone; the running month's set-aside to month end is
    `parts`'s envelopes half."""
    from . import envelope_breakdown
    return envelope_breakdown.month(
        st["buckets"]["fixed"], kind=kind, y=y, m=m,
        reserve=(parts or {}).get("envelopes") or 0)


def _lens_map(food: dict, other: dict, fixed: dict, *, y: int,
              m: int | None, pending: float, savings: list[dict],
              excess: float | None, bills: dict | None = None,
              envelopes: dict | None = None,
              excess_gone: bool = False) -> dict:
    """A lens reply's money map: its rows as sent and the "Variable
    spending" row over them, built by the rows Today sends
    (todayview.map_rows). The buckets arrive at the lens's paced share.
    `bills` is the month's (or, on the Year, the year's) bills breakdown
    (bills_breakdown): the Bills row prints its figures, its caption and
    its sentence, as Today's does."""
    from . import bills_breakdown, todayview
    rows = todayview.map_rows(
        food, other, fixed,
        ledger=lambda k: todayview.ledger_path(k, y, m),
        pending=pending, savings=savings, excess=excess,
        excess_gone=excess_gone)
    for r in rows:
        if r["label"] != "Bills" or r["judged"]:
            continue
        if bills is not None:
            bills_breakdown.apply_to_row(r, bills)
        else:
            # parts that could not be read: no words or caption rather
            # than ones from another slice of the figures
            r["status"] = r["caption"] = None
    # no Bills row for a period with no dated bill; the pools, their own
    # row under Bills (envelope_breakdown)
    todayview.without_empty_bills(rows, bills, fixed)
    todayview.with_envelopes(rows,
                             envelopes, lambda k: todayview.ledger_path(k, y, m))
    return {"plan_rows": todayview.sent_plan_rows(rows),
            "plan_total": todayview.plan_total(rows)}


def month_summary(conn, year: int, month: int,
                  today: dt.date | None = None) -> dict:
    """The month report card. Past months: FINAL (the engine's verdict at
    month end, frac=1). Current month: in-progress + pace projection.
    Future months: the plan (budgets + scheduled bills, no spend)."""
    today = today or dt.date.today()
    memo: dict = {}
    standing = month_standing(conn, year, month, today, memo=memo)
    # a month from before the budget is read without a bill schedule
    past = standing in PAST_STANDINGS
    st = (_unbudgeted_state(conn, year, month, memo=memo) if past
          else month_state(conn, year, month, today, memo=memo))
    as_of = st["as_of"]
    status = ("final" if (year, month) < (today.year, today.month)
              else "in_progress" if (year, month) == (today.year, today.month)
              else "future")
    b = st["buckets"]
    # a verdict needs a budget the household made: a frozen snapshot, or a
    # saved one — never the loader's unsaved first-month guess (the Today
    # page, the app and the daily email gate on the same answer)
    judged = standing == "judged"
    label, text = STANDING_WORDS.get(standing, (None, None))

    def bucket(k):
        v = b[k]
        # the variable buckets at the hero's paced share (_paced); the
        # bills bucket is owed whole
        pace = (lambda x: _paced(st, x)) if k != "fixed" else (lambda x: x)
        out = {"actual": _R(v["actual"]), "expected": _R(v["expected"]),
               "month_budget": _R(pace(v["month_budget"]))}
        if "children" in v:     # decomposition, carried like Today
            out["children"] = [{"name": c["name"], "actual": _R(c["actual"]),
                                "expected": _R(c["expected"]),
                                "month_budget": _R(pace(c["month_budget"]))}
                               for c in v["children"]]
            out["display_actual"] = _R(v["display_actual"])
            out["display_expected"] = _R(v["display_expected"])
            out["remaining_budget"] = _R(pace(v["remaining_budget"]))
        return out

    mtd, amazon_subs, amazon_total = _by_category(st["rows"])
    # the same grouping over the variable rows alone: a reader who wants
    # "where did the everyday money go, with the bills as one line" needs
    # the bills OUT of the category list. The variable rows are the
    # buckets' own — not the raw rows minus the fixed ones: a purchase an
    # envelope partly absorbed is TWO rows to the engine, the part inside
    # the cap (fixed) and the spill (variable, carrying `overflow_of`),
    # and the raw row would count the whole swipe as everyday spend.
    variable_rows = _variable_rows(st)
    mtd_variable, _, _ = _by_category(variable_rows)
    biggest = sorted(st["rows"], key=lambda r: -r["amount"])[:10]
    biggest_variable = sorted(variable_rows, key=lambda r: -r["amount"])[:10]
    # income is the bank deposits of a covered month (0 when there were
    # none) and unknown for a month no bank account feeds: the year strip
    # and the reports' totals read the same rule, so the hero cannot call
    # a month unknown while its cell counts it as a loss
    key = f"{year:04d}-{month:02d}"
    income_actual = (_month_income_actual(conn, year, month, today, memo)
                     or 0
                     if status != "future" and _Coverage(conn).month_known(key)
                     else None)
    paid = sorted(b["fixed"].get("occ_rows") or [],
                  key=lambda r: r["date"])
    # The month map's Savings row actual. rate_90d is the average over the
    # last 3 months (savings.pace_since), not what THIS month saved, so the figure comes from the same
    # source as the Year lens' saved_ytd: the ledger's posted contribution
    # to the standing "Savings" goal within this month: the plan evidence
    # savings.month_saved reads, the checking leg standing in where the
    # destination shows less than the plan.
    from ..engine import savings as _savings
    _cfg = budget.load_config(conn)
    saved_month = None
    _goal = next((g for g in _savings.goals(_cfg)
                  if _savings.is_plan_savings_goal(g)), None)
    if (_goal is not None and not _savings.is_plan_only(_goal)
            and status != "future"):
        _first = dt.date(year, month, 1)
        _last = dt.date(year, month, calendar.monthrange(year, month)[1])
        saved_month = _R(_savings.month_saved(
            conn, _cfg, _goal, _first, min(_last, today)))
    lens_goal = _lens_goal(conn, _cfg, _goal, today)
    buckets = {k: bucket(k) for k in ("fixed", "food", "other")}
    plan_surplus = (_R(st["plan_surplus"])
                    if st.get("plan_surplus") is not None and not past
                    else None)
    from . import todayview as _tv
    # no plan stood behind a month from before the budget: no map rows
    # the month's bills in parts that add up to its plan — one
    # breakdown for the Bills row, the paid-vs-planned card and the
    # monthly email: a month that is over in a closed month's words, the
    # running month read as Today reads it (one walk), a future month's
    # plan still due. A month from before the budget has no map.
    from . import bills_breakdown as _bb
    from . import envelope_breakdown as _eb
    parts = (running_parts(conn, st, today, memo=memo)
             if status == "in_progress" and not past else None)
    bills_bd = (None if past
                else closed_bill_words(st)["breakdown"] if status == "final"
                else running_breakdown(conn, st, today, memo=memo,
                                       parts=parts)
                if status == "in_progress" else _bb.future(st))
    # the month's pools, the Envelopes row under Bills
    env_bd = (None if past else envelope_month(
        st, year, month, {"final": "closed", "in_progress": "running"}.get(
            status, "future"), parts))
    closed = status == "final"
    # the card's plan in its two parts, in cents that add up to it and
    # print as the Bills row's plan and the pools' set-aside
    split = (_eb.plan_split(b["fixed"]["month_budget"], bills_bd["plan"],
                            env_bd["set_aside"],
                            b["fixed"].get("occ_budget",
                                           b["fixed"]["month_budget"]))
             if bills_bd is not None and env_bd is not None else None)
    money_map = {"plan_rows": [], "plan_total": _tv.plan_total([])} \
        if past else _lens_map(
        buckets["food"], buckets["other"],
        # the map's Bills row reads the due-dated figures (the reply's
        # bucket keeps its three)
        dict(buckets["fixed"], **{k: _R(b["fixed"][k]) for k in (
            "occ_actual", "occ_expected", "occ_budget")
            if k in b["fixed"]}), y=year, m=month,
        bills=bills_bd, envelopes=env_bd,
        excess_gone=_tv.excess_used_up(st),
        pending=(0.0 if closed
                 else _R(b["fixed"].get("unpaid_due") or 0)),
        savings=lens_savings_row(lens_goal, saved_month,
                                 projected="avg / mo, last 3 months"),
        excess=plan_surplus)
    out = {
        "y": year, "m": month, "status": status,
        # the hero tick's label: a closed month's pace is its whole budget
        # and a future one has none (specs/money-map.md)
        "pace_label": "today's pace" if status == "in_progress" else None,
        "as_of": as_of.isoformat(), "days_in_month": st["days_in_month"],
        "verdict": st["verdict"] if judged else None,
        "variance": _R(st["variance"]) if judged else 0.0,
        "tolerance": _R(st["tolerance"]),
        # false: no budget of the household's own stands behind this
        # month, so there is no verdict and no projection to show
        "budget_set": judged,
        # which of the four standings, and for an unjudged month the pill
        # and the sentence every surface prints (month_standing)
        "standing": standing,
        "standing_label": label,
        "standing_text": text,
        # the month's income is not measured by the bank feed: the words
        # in place of an income figure and a net (counted-figures)
        "income_text": (reporting.INCOME_INCOMPLETE
                        if status != "future" and income_actual is None
                        and standing != "no_data" else None),
        "projection": (_projection(st)
                       if status == "in_progress" and judged else None),
        "variable_actual": _R(st["variable_actual"]),
        # the month's spendable: the budget's paced share, so a first
        # month the ledger began inside of reads "$X of $Y" against the
        # days it covered — the gap the verdict judges. With no saved
        # budget behind the month (budget_set false) a reader shows the
        # spend alone and ignores this, but a native build in the stores
        # predates that branch and always prints budget minus spend, red
        # when negative: a null reaches it as $0 and every month with
        # spending reads "over $0 to spend". So the figure stays numeric
        # and never below the spend — there is no budget to be over.
        "variable_budget": (_R(st["variable_budget_paced"]) if judged
                            else _R(max(st["variable_budget_paced"],
                                        st["variable_actual"]))),
        "buckets": buckets,
        # the month's money map, as Today's is sent: the rows in the
        # wizard's order with their words, and the Variable spending row
        **money_map,
        "bills": {
            # posted, planned and awaiting in the shape earlier releases
            # sent: the bills and the pools together, so an installed phone's card still adds
            # up (it prints these and nothing newer)
            "posted": _R(b["fixed"]["actual"]),
            # the charges that figure holds, counted as it counts them
            "posted_counts": posted_counts(
                len(paid), len(b["fixed"].get("rows") or []) - len(paid)),
            "planned": _R(b["fixed"]["month_budget"]),
            # "awaiting" and "scheduled but not posted" are a running
            # month's words: a closed month sends neither, and says what
            # became of its bills in `notes`
            "awaiting": (0.0 if closed
                         else _R(b["fixed"].get("unpaid_due") or 0)),
            # ...and the card a current client prints: the due-dated bills
            # in the breakdown's whole dollars (the Bills row beside it),
            # with the pools' set-aside below
            "bills_posted": (float(bills_bd["posted"])
                             if bills_bd is not None else None),
            "bills_posted_counts": posted_counts(len(paid), 0),
            "bills_planned": (split[0] if split is not None
                              else float(bills_bd["plan"])
                              if bills_bd is not None else None),
            # a month to come awaits its whole bills plan less what is
            # posted (the Bills row's "due"), as `awaiting` says for
            # the bills and pools together
            "bills_awaiting": (0.0 if closed
                               else float(bills_bd["due"])
                               if bills_bd is not None
                               and status == "future"
                               else float(bills_bd["awaiting"])
                               if bills_bd is not None else None),
            # a closed month's bills not paid, by name (bills_breakdown
            # .card_lines); the parts are the Bills row's table
            "notes": (_bb.card_lines(bills_bd) if bills_bd is not None
                      else []),
            # the plan's share of the pools, said apart from the bills
            # plan (envelope_breakdown.set_aside_dollars), and the plan
            # the two make together, as printed
            # — in cents adding up to `planned`, each printing as its
            # row's whole dollars (envelope_breakdown.plan_split)
            "envelopes_set_aside": (split[1] if split is not None
                                    else float(env_bd["set_aside"])
                                    if env_bd is not None else 0.0),
            "set_aside": (_R(split[0] + split[1]) if split is not None
                          else None),
            "breakdown": _bb.sent(bills_bd),
            # category: the transaction category on the matched charge
            # (what the bill stamps, or the feed's) and the feed's detailed
            # one, so a reader can see the bills by kind — mortgage,
            # utilities, subscriptions
            "paid": [{"date": r["date"].isoformat(),
                      "amount": _R(r["amount"]),
                      "payee": merchant_sql.shown_payee(r),
                      "category": r.get("category"),
                      "detailed": r.get("category_detailed")}
                     for r in paid],
            # [payee, amount, due, withdrawn_on] — see month_status
            "overdue": ([] if closed else
                        [[p, _R(a), due, gone]
                         for p, a, due, gone in budget.overdue_entries(st)]),
            # the pool and the period are what "how close to the cap" means
            # for an envelope that accumulates over several months — `used`
            # alone is this month's swipes against a cap that is not monthly
            "envelopes": [{"payee": e["payee"], "uid": e.get("uid"),
                           "used": _R(e["used"]),
                           "monthly": _R(e["monthly"]),
                           "overflow": _R(e["overflow"]),
                           "pool": _R(e.get("pool", e["monthly"])),
                           "period_months": int(e.get("period_months") or 1),
                           "period_used": _R(e.get("period_used", e["used"])),
                           "period_left": _R(e.get("period_left", 0)),
                           # "pin to Today": the household watches this cap
                           # like a budget of its own
                           "show_today": bool(e.get("show_today")),
                           # the pool named on its own prints its own
                           # pair, its period said: "spent of cap"
                           # (budget.envelope_shown, as every client has
                           # printed it) and the period
                           # (envelope_breakdown.pool_line)
                           "shown_used": _R(budget.envelope_shown(e)[0]),
                           "shown_of": _R(budget.envelope_shown(e)[1]),
                           "shown_period": it["shown_period"],
                           # the line a current client prints: what the
                           # period charged at the pool's merchants, the
                           # pool, and the spill the matcher counted in
                           # spending over the same period
                           # (envelope_breakdown.pool_figures)
                           "pair_spent": float(it["pair_spent"]),
                           "pair_of": float(it["pair_of"]),
                           "pair_past": float(it["pair_past"]),
                           "pair_text": it["pair_text"],
                           # the pool's part of the Envelopes row's gauge
                           # (paid out this month of what it held), what
                           # it has left, and on a settled month what
                           # went unspent (envelope_breakdown)
                           "drawn": float(it["drawn"]),
                           "cap": float(it["cap"]),
                           "left": float(it["left"]),
                           "unspent": float(it["unspent"])}
                          for e, it in zip(
                              b["fixed"].get("envelopes") or [],
                              (env_bd or _eb.month(
                                  b["fixed"], kind="closed", y=year,
                                  m=month))["items"])],
        },
        "by_category": mtd, "amazon_subs": amazon_subs,
        "amazon_total": amazon_total,
        "by_category_variable": mtd_variable,
        "biggest": [_ser_txn(r, False, st.get("bill_payments"))
                    for r in biggest],
        # the same list with the bill payments and envelope swipes out —
        # a mortgage is always the biggest row, and says nothing
        "biggest_variable": _with_items(conn, [
            _variable_ser(r) for r in biggest_variable]),
        "income": ({"actual": _R(income_actual)
                    if income_actual is not None else None,
                    "budgeted": _R(st["income"])
                    if st.get("income") is not None and not past else None,
                    # where the plan's income figure comes from —
                    # paychecks / scenario / typed (budget.income_source),
                    # the word the page labels it with; only "typed" is a
                    # budgeted figure
                    "source": None if past else st.get("income_source")}),
        # a bill is counted in the month it belongs to (month_status);
        # Cash Flow dates spending by the day it was paid
        "dating": "bill_month",
        "spend_total": _R(st["total_actual"]),
        # what the hero and the flow line print: the spendable left (below
        # zero, over) and the month's net (null while income is unknown) —
        # decided here so no client subtracts
        "remaining": _R((_R(st["variable_budget_paced"]) if judged
                         else _R(max(st["variable_budget_paced"],
                                     st["variable_actual"])))
                        - _R(st["variable_actual"])),
        "net": (_R(_R(income_actual) - _R(st["total_actual"]))
                if income_actual is not None else None),
        "variable_scale": ({"factor": round(st["variable_scale"]["factor"], 4),
                            "static_total": _R(st["variable_scale"]["static_total"]),
                            "available": _R(st["variable_scale"]["available"])}
                           if st.get("variable_scale") and not past else None),
        # the standing monthly plan surplus (the excess-cash figure,
        # same source as Today) — the month map's Excess cash row
        "plan_surplus": plan_surplus,
        "saved_month": saved_month,
        "savings_goal": lens_goal,
        # 'snapshot' when a closed month was judged against its frozen
        # budget; 'live' = today's config (current/future months, or a
        # closed month that predates snapshots — the SPA says so)
        "budget_source": st["budget_source"],
        # same flag for the bill schedule — 'live' on a closed month means
        # its snapshot predates bill freezing, so today's schedule applies
        "bills_source": st["bills_source"],
        "first_year": _first_year(conn),
    }
    # the plan's income in the plan line's own whole dollars, the figure
    # Budget and the Bills page print for the month
    out["income"]["plan_text"] = income_plan_text(
        out["income"],
        shown=None if past else (st.get("plan_line") or {}).get("income"))
    out.update(hero_words(out))
    return out


# the plan's income named by where it comes from (budget.income_source),
# the planner's words; only a typed figure is "budgeted"
_PLAN_INCOME_WORDS = {"paychecks": "expected from paychecks",
                      "scenario": "income scenario", "typed": "budgeted"}


def income_plan_text(inc: dict, *, shown: int | None = None) -> str | None:
    """The words beside the Month hero's income — "expected from
    paychecks $X" — or None when there is no plan figure, or when it is
    the same whole dollars as what came in (it would repeat the figure
    beside it). `shown` is the plan income in the whole dollars the plan
    line prints (budget.plan_line's income leg), which Budget and the
    Bills page print for the same month; without it, the income's own."""
    from ..engine.reporting import whole_dollars
    plan = inc.get("budgeted")
    if plan is None:
        return None
    dollars = whole_dollars(plan) if shown is None else int(shown)
    if (inc.get("actual") is not None
            and whole_dollars(inc["actual"]) == dollars):
        return None
    return (f"{_PLAN_INCOME_WORDS.get(inc.get('source') or '', 'planned')} "
            + ("-$" if dollars < 0 else "$") + f"{abs(dollars):,}")


def hero_words(d: dict) -> dict:
    """The Month hero's words, decided once for both clients
    (specs/money-map.md, "The Month hero's words"): the word between the
    big figure and the budget, where the pace tick stands, the verdict
    line and the projection's tail. Only a judged month has them; an
    unjudged one prints the server's standing sentence instead."""
    from .charts import _money
    out = {"remaining_word": None, "pace_expected": None,
           "variance_text": None}
    if d.get("verdict") is None:
        return out
    rem, status = d.get("remaining") or 0.0, d.get("status")
    out["remaining_word"] = ("over" if rem < 0
                             else "left unspent of" if status == "final"
                             else "left of")
    var, tol = d.get("variance") or 0.0, d.get("tolerance") or 0.0
    out["pace_expected"] = _R((d.get("variable_actual") or 0.0) - var)
    so_far = " so far" if status == "in_progress" else ""
    # under way, the variance is against the pace (what the pace expects
    # by today), not the budget — the figure beside "over" is the budget's
    # — so the line names the pace: two "over budget" amounts side by side
    # read as one figure printed twice and wrong once
    against = "pace" if status == "in_progress" else "budget"
    if var > tol:
        out["variance_text"] = {
            "text": f"{_money(var)} over {against}{so_far}", "tone": "neg"}
    elif var < -tol:
        out["variance_text"] = {
            "text": f"{_money(-var)} under {against}{so_far}", "tone": "pos"}
    else:
        out["variance_text"] = {
            "text": f"within tolerance ({_money(tol)}){so_far}",
            "tone": "warn"}
    p = d.get("projection")
    if p:
        pv, pt = p.get("variance") or 0.0, p.get("tolerance") or 0.0
        p["tail"] = (f"{_money(pv)} over" if pv > pt
                     else f"{_money(-pv)} under" if pv < -pt else "on plan")
        p["tail_tone"] = ("neg" if pv > pt else "pos" if pv < -pt
                          else "warn")
    return out


# ---- year lens --------------------------------------------------------------


def prior_year_window(year: int, today: dt.date
                      ) -> tuple[dt.date, dt.date, dt.date | None]:
    """The span of `year - 1` that `year` is compared with, as of the
    household's `today`: (start, EXCLUSIVE end, through). A year still in
    progress holds only its days up to today, so the prior year stops the
    same number of days in — eleven months of spend set against twelve
    reads as a drop in every category that never happened. The rule is
    reporting._prev_window_end's for months: the same date one year back,
    Feb 29 read as Feb 28, and on a month's last day the prior year's whole
    month (Feb 28 against a leap February's 29 days; Dec 31 against the
    whole year). `through` is the last day counted, or None when the prior
    year is whole — a closed year, a future one, or Dec 31."""
    start, whole = dt.date(year - 1, 1, 1), dt.date(year, 1, 1)
    if year != today.year:
        return start, whole, None
    py = year - 1
    pm_days = calendar.monthrange(py, today.month)[1]
    month_end = (today + dt.timedelta(days=1)).day == 1
    through = dt.date(py, today.month,
                      pm_days if month_end else min(today.day, pm_days))
    if through == dt.date(py, 12, 31):
        return start, whole, None
    return start, through + dt.timedelta(days=1), through


def cell_totals(cells: list, *, year: int | None = None,
                closed: bool = False) -> dict:
    """A year's totals: the sum of its month cells, so the twelve cells add
    up to the figure above them. The cells date a bill by the month it
    belongs to (month_status), which Cash Flow — dated by the day paid —
    does not; the page says which it reads. Spending counts every elapsed
    month. Income, saved and the savings rate are judged by the month, the
    one rule Cash Flow and the yearly email read too: they sum the cells
    whose income is complete (a cell's income is None where the bank feed
    does not measure the month), and `incomplete_text` says so when some
    cell is left out — `saved_months_known` of `saved_months_total`. Only
    a year with no such cell has no income, saved figure or rate, and
    `unknown_text` says why (specs/counted-figures.md)."""
    elapsed = [c for c in cells if c["status"] != "future"]
    known = [c for c in elapsed if c.get("income") is not None]
    spend = _R(sum(c.get("spend") or 0 for c in elapsed))
    out = {"income": None, "spend": spend, "saved": None, "rate": None,
           "saved_months_known": len(known),
           "saved_months_total": len(elapsed),
           "unknown_text": None, "incomplete_text": None}
    if known:
        inc = _R(sum(c["income"] for c in known))
        saved = _R(sum(c["income"] - (c.get("spend") or 0) for c in known))
        out.update(income=inc, saved=saved,
                   rate=round(100 * saved / inc, 1) if inc else None,
                   incomplete_text=reporting.income_incomplete_note(
                       len(known), len(elapsed)))
    elif elapsed:
        out["unknown_text"] = year_unknown_text(year, closed=closed)
    out["dollars"] = totals_dollars(out, every_month=len(known) == len(elapsed))
    return out


def totals_dollars(t: dict, *, every_month: bool) -> dict:
    """The year's totals in the whole dollars the page prints — the one
    rule Cash Flow's Left over by year reads too (reporting.year_dollars)."""
    return reporting.year_dollars(t["income"], t["spend"], t["saved"],
                                  every_month=every_month)


def plan_caption(cells: list) -> str | None:
    """Which months the Yearly budget card covers: its elapsed budgeted
    months (the `final` and `projected` cells), "May–Jul" for a run, each
    named when there are gaps (specs/budget-snapshots.md, "The Year
    page's words")."""
    ms = [c["m"] for c in cells if c["status"] in ("final", "projected")]
    if not ms:
        return None
    if len(ms) == 1:
        return (f"{calendar.month_abbr[ms[0]]}, the month with a budget"
                + _to_date_words(cells))
    names = ([f"{calendar.month_abbr[ms[0]]}–{calendar.month_abbr[ms[-1]]}"]
             if ms == list(range(ms[0], ms[-1] + 1))
             else [calendar.month_abbr[m] for m in ms])
    return (f"{', '.join(names)}, the months with a budget"
            + _to_date_words(cells))


def _to_date_words(cells: list) -> str:
    """The running month counts on the card to date, while its cell in
    the grid above is projected to month end: the caption says which is
    which, or the two "over by" figures read as one done twice."""
    run = next((c["m"] for c in cells if c["status"] == "projected"), None)
    return ("" if run is None else
            f" · {calendar.month_abbr[run]} to date (its cell above is"
            " projected to month end)")


def posted_counts(bills: int, envelope_charges: int) -> str:
    """What the Month lens's "posted" figure holds, in the words beside
    it: the bill payments alone ("3"), or with the envelope charges the
    figure counts too ("3 bills + 12 envelope charges")."""
    if envelope_charges <= 0:
        return f"{bills}"
    return (f"{bills} bill{'' if bills == 1 else 's'} + {envelope_charges} "
            f"envelope charge{'' if envelope_charges == 1 else 's'}")


def year_unknown_text(year: int | None, *, closed: bool = False) -> str:
    """The words under a year none of whose months has bank data: "so
    far" while the year runs, "had" once it is over."""
    of = f"{year}" if year is not None else "the year"
    when = "had" if closed else "so far has"
    return (f"No month of {of} {when} bank data — income and "
            "what was left over are unknown, not $0.")


def year_summary(conn, year: int, today: dt.date | None = None,
                 *, with_detail: bool = False) -> dict:
    """12 verdict cells + annual totals + category totals vs prior year.
    Verdict cells cover months with a budget (a frozen snapshot, or the
    live config for the current month); a closed month with no snapshot
    is a "net" cell — income vs spending, no verdict.

    `with_detail` adds `detail` — what the yearly email needs beyond the
    page: the variable spend by category with the bills' total apart,
    the carve-outs' and envelopes' annual figures over the budgeted
    months, and the net of every year back to the ledger's first. It walks every elapsed month's state (the
    budgeted ones are already in the memo), so it is for the mail, not
    the page."""
    today = today or dt.date.today()
    # annual per-bucket aggregates for the year money map, which
    # todayview.map_rows turns into the reply's plan_rows.
    # Sums cover the elapsed BUDGETED months only (a July view shows seven
    # months of plan, not a phantom full-year budget — and a no-snapshot
    # month contributes nothing, since no plan existed for it).
    annual = {k: {"actual": 0.0, "expected": 0.0, "month_budget": 0.0}
              for k in ("food", "other", "fixed")}
    # the Bills row's due-dated figures, summed apart (the pools are the
    # Envelopes row's, envelope_breakdown.year)
    annual_occ = {"occ_actual": 0.0, "occ_expected": 0.0, "occ_budget": 0.0}
    # standing monthly plan surplus, from the latest elapsed month's state
    # (None when nothing has elapsed — a future year has no excess to map)
    plan_surplus = surplus_st = None
    memo: dict = {}                 # one engine cache across the 12 months
    # the closed months a budget of the household's own stands behind: the
    # Month view's answer (month_standing), so the two never disagree
    snap_months = {m for m in budget.snapshot_months(conn, year)
                   if month_standing(conn, year, m, today,
                                     memo=memo) == "judged"}
    # bank coverage decides whether a cell's income is a number or unknown,
    # month by month (the Month lens hero's rule); the annual totals below
    # sum those months, so they are known only when every month is
    cov = _Coverage(conn)
    # one pass over the bank rows serves both the annual totals below and
    # the net cells' months
    # this year and last (the totals' comparison); the yearly email's
    # detail strip below reads every year, so it takes the whole history
    # every read below — income and spend, cells and totals — leaves out
    # the same accounts the month cells' states do: a closed month the
    # ones excluded when it closed, every other day today's
    # (lens_excluded_sql), or saved, the savings rate and the net cells
    # would set income filtered one way against spend filtered another
    excl, ex_p = lens_excluded_sql(conn, today, memo=memo)
    income_y, income_m = (
        _income_year_month(conn, excl, ex_p) if with_detail
        else _income_year_month(conn, excl, ex_p, dt.date(year - 1, 1, 1),
                                dt.date(year + 1, 1, 1)))
    # …and one ledger read: the months' match windows overlap five weeks
    # either side, so each month read alone pulls the year four times over
    budget.share_spend_window(memo, *budget.year_spend_window(year))

    # each budgeted month's pools, for the Year's Envelopes row
    env_months: list[dict] = []

    def month_cells(year: int, today: dt.date, snap_months, track: bool):
        """The year's twelve cells as of `today`. `track` (the lens year)
        also adds each budgeted month into the annual money map and finds
        the standing surplus; the prior year's cells only make its
        comparison totals, on the same basis as this year's."""
        out: list = []
        found: dict = {}
        for m in range(1, 13):
            if (year, m) > (today.year, today.month):
                out.append({"m": m, "status": "future", "verdict": None,
                            "variance": None, "variable_actual": None,
                            "variable_budget": None})
                continue
            if (year, m) < (today.year, today.month) and m not in snap_months:
                # no budget was recorded for this closed month — show the
                # cash-flow fact (income vs spending), never a verdict against
                # a budget that didn't exist yet
                if found.get("spend_m") is None:
                    found["spend_m"] = {r["p"]: r["amt"] for r in conn.execute(
                        f"SELECT to_char(t.date,'YYYY-MM') p, "
                        f"SUM({reporting.NET_AMOUNT_JOINED}) amt "
                        f"FROM transactions t {reporting.REIMB_PARTIAL_JOIN} "
                        f"WHERE {reporting.SPEND_WHERE} "
                        f"AND t.date >= %s AND t.date < %s {excl} GROUP BY p",
                        (dt.date(year, 1, 1), dt.date(year + 1, 1, 1), *ex_p))}
                key = f"{year:04d}-{m:02d}"
                spend = _R(found["spend_m"].get(key, 0) or 0)
                inc = (_R(income_m.get(key, 0) or 0)
                       if cov.month_known(key) else None)
                out.append({"m": m, "status": "net", "verdict": None,
                            "variance": None, "variable_actual": None,
                            "variable_budget": None,
                            "income": inc, "spend": spend,
                            "net": (_R(inc - spend)
                                    if inc is not None else None)})
                continue
            st = month_state(conn, year, m, today, memo=memo)
            if month_standing(conn, year, m, today, memo=memo) != "judged":
                # the month in progress with no budget the household saved:
                # the cash-flow fact a no-snapshot month shows, never a
                # verdict on the loader's unsaved guess
                key = f"{year:04d}-{m:02d}"
                inc = (_R(income_m.get(key, 0) or 0)
                       if cov.month_known(key) else None)
                spend = _R(st["total_actual"])
                out.append({"m": m, "status": "net", "verdict": None,
                            "variance": None, "variable_actual": None,
                            "variable_budget": None,
                            "income": inc, "spend": spend,
                            "net": (_R(inc - spend)
                                    if inc is not None else None)})
                continue
            if track and st.get("plan_surplus") is not None:
                found["surplus_st"] = st
            if track:
                # each budgeted month's bills breakdown, folded into the
                # Year's Bills row (bills_breakdown.year): a closed
                # month's paid / not paid, the running month's still due
                running = (year, m) == (today.year, today.month)
                parts = (running_parts(conn, st, today, memo=memo)
                         if running else None)
                mbd = (running_breakdown(conn, st, today, memo=memo,
                                         parts=parts)
                       if running else closed_bill_words(st)["breakdown"])
                env_months.append(envelope_month(
                    st, year, m, "running" if running else "closed", parts))
                fx = st["buckets"]["fixed"]
                for k in annual_occ:
                    annual_occ[k] += fx.get(k, fx[{"occ_actual": "actual",
                                                   "occ_expected": "expected",
                                                   "occ_budget": "month_budget"
                                                   }[k]])
                if mbd is None:
                    # a month whose parts cannot be read: the year has
                    # no parts rather than parts that do not add up
                    found["bills_failed"] = True
                else:
                    found.setdefault("bill_months", []).append(
                        dict(mbd, y=year, m=m))
            for k in (("food", "other", "fixed") if track else ()):
                b = st["buckets"][k]
                annual[k]["actual"] += b["actual"]
                annual[k]["expected"] += b["expected"]
                # a first month counts the share its cell judged (_paced)
                annual[k]["month_budget"] += (b["month_budget"] if k == "fixed"
                                              else _paced(st, b["month_budget"]))
            var_budget = _R(st["variable_budget_paced"])
            # the cash-flow fact beside the verdict, the way the Month lens
            # hero reads it (income from the bank series, spend = the month's
            # total out), so a reader walking the year sees what each month
            # kept or lost, not only how it was judged
            key = f"{year:04d}-{m:02d}"
            inc = (_R(income_m.get(key, 0) or 0)
                   if cov.month_known(key) else None)
            spend = _R(st["total_actual"])
            flow = {"income": inc, "spend": spend,
                    "net": _R(inc - spend) if inc is not None else None}
            if (year, m) == (today.year, today.month):
                proj = _projection(st)
                out.append({"m": m, "status": "projected",
                            "verdict": proj["verdict"],
                            "variance": proj["variance"],
                            "variable_actual": _R(st["variable_actual"]),
                            "variable_budget": var_budget, **flow})
            else:
                out.append({"m": m, "status": "final",
                            "verdict": st["verdict"],
                            "variance": _R(st["variance"]),
                            "variable_actual": _R(st["variable_actual"]),
                            "variable_budget": var_budget, **flow})

        if track:
            for c in out:
                c.update(cell_words(c, month_standing(
                    conn, year, c["m"], today, memo=memo)))
        return out, found.get("surplus_st"), (
            None if found.get("bills_failed")
            else found.get("bill_months") or [])

    cells, surplus_st, bill_months = month_cells(year, today, snap_months,
                                                 True)

    # annual totals — the Cash Flow report's frozen definitions (income:
    # bank deposits net of own-money transfers; spend: SPEND_WHERE), scoped
    # to the two years the lens shows. A year with any month no bank
    # account feeds has unknown income, not $0: the cells call those months
    # unknown, and a sum that counted them would set part of a year's
    # income against all of its spend and report a loss nobody made. What
    # went out is still known, so spend stays.
    # The year totals and the year × category rows below read the same two
    # years of spend; GROUPING SETS takes both groupings from one pass
    # (GROUPING(cat) = 1 marks the year-total rows).
    # cat is the DISPLAY form; cat_key is the value the ledger stores, so
    # the row the page renders can be turned back into the filter that
    # opens it. MIN, not a second GROUP BY term: grouping on the raw key
    # would split a display label whose rows store it two ways (an
    # override typed with spaces beside the underscore form), and the
    # totals would change shape just to carry a link.
    # by category, so a hand-split row's parts are joined and each counts
    # under its own category (the year total is unchanged: parts sum to
    # the row)
    # the prior year counts only the span comparable with this one
    # (prior_year_window); rows past its cut are left out of both groupings
    prev_start, prev_end, prev_through = prior_year_window(year, today)
    two_years = conn.execute(
        f"SELECT to_char(t.date,'YYYY') yr, {reporting.PART_CAT} cat, "
        f"MIN(COALESCE({_categories.PART_CAT}, '?')) cat_key, "
        f"SUM({reporting.PART_NET_JOINED}) amt, GROUPING({reporting.PART_CAT}) gc "
        f"FROM transactions t {reporting.REIMB_PARTIAL_JOIN} {reporting.SPLIT_JOIN} "
        f"WHERE {reporting.SPEND_WHERE} "
        f"AND t.date >= %s AND t.date < %s "
        f"AND NOT (t.date >= %s AND t.date < %s) {excl} "
        f"GROUP BY GROUPING SETS ((yr), (yr, cat))",
        (prev_start, dt.date(year + 1, 1, 1), prev_end, dt.date(year, 1, 1),
         *ex_p)).fetchall()
    # The prior year's comparison totals are the sum of ITS month cells
    # over the span comparable with this one (prior_year_window), under the
    # same rule as this year's (cell_totals: saved over the months whose
    # income is complete). Its cells are the net cells a closed month without a
    # frozen budget shows — the month's spend and bank income by date, the
    # cut month through `through` — read in two grouped passes: walking the
    # prior year's month states would read the ledger and match every row
    # a second time on each Year lens view, for a comparison column.
    prev_spend = {r["p"]: r["amt"] for r in conn.execute(
        f"SELECT to_char(t.date,'YYYY-MM') p, "
        f"SUM({reporting.NET_AMOUNT_JOINED}) amt "
        f"FROM transactions t {reporting.REIMB_PARTIAL_JOIN} "
        f"WHERE {reporting.SPEND_WHERE} "
        f"AND t.date >= %s AND t.date < %s {excl} GROUP BY p",
        (prev_start, prev_end, *ex_p))}
    prev_inc = {r["p"]: r["amt"] for r in conn.execute(
        f"SELECT to_char(t.date,'YYYY-MM') p, SUM({reporting.INCOME_NET}) amt "
        f"FROM transactions t WHERE {reporting._INCOME_WHERE} "
        f"AND t.date >= %s AND t.date < %s {excl} GROUP BY p",
        (reporting._bank_account_ids(conn), prev_start, prev_end, *ex_p))}
    last_prev = prev_end - dt.timedelta(days=1)
    prev_cells = []
    for m in range(1, 13):
        key = f"{year - 1:04d}-{m:02d}"
        if (year - 1, m) > (min(last_prev, today).year,
                            min(last_prev, today).month):
            prev_cells.append({"m": m, "status": "future"})
            continue
        inc = (_R(prev_inc.get(key, 0) or 0)
               if cov.month_known(key) else None)
        prev_cells.append({"m": m, "status": "net", "income": inc,
                           "spend": _R(prev_spend.get(key, 0) or 0)})

    # category totals vs prior year (effective category, Spending-report
    # definition), top 12 by the lens year's spend
    rows = [r for r in two_years if not r["gc"]]
    cur = {r["cat"]: r["amt"] for r in rows if r["yr"] == str(year)}
    prev = {r["cat"]: r["amt"] for r in rows if r["yr"] == str(year - 1)}
    # the key must be the spelling THIS year stores: the same label can
    # be backed by another string last year, and the link opens this year
    cat_key = {r["cat"]: r["cat_key"] for r in rows if r["yr"] == str(year)}
    cats = sorted(cur, key=lambda c: -cur[c])[:12]
    # [label, this year, last year, key] — the key trails so a client that
    # only knows the first three keeps working
    categories = [[c, _R(cur[c]), _R(prev.get(c, 0) or 0), cat_key.get(c)]
                  for c in cats]

    # The money map's Savings row actual. rate_90d × elapsed months is an
    # extrapolation; the ledger knows the real figure — the net posted into
    # the standing "Savings" goal (the row's key) over the lens year's
    # elapsed BUDGETED months. The row's plan is the monthly plan times
    # those months (the final and projected cells; a month with no budget
    # is a cash-flow cell every other row leaves out too), so the actual
    # is measured over exactly the same months — a transfer in an
    # unbudgeted month would otherwise count against a plan that never
    # covered it. None when there is nothing to measure (no goal,
    # plan-only, or no budgeted month), and the SPA falls back to its
    # extrapolation.
    from ..engine import savings
    saved_ytd = None
    _lens_cfg = budget.load_config(conn)
    goal = next((g for g in savings.goals(_lens_cfg)
                 if savings.is_plan_savings_goal(g)), None)
    span_end = min(today, dt.date(year, 12, 31))
    windows = []
    for c in cells:
        if c["status"] in ("final", "projected"):
            first = dt.date(year, c["m"], 1)
            last = dt.date(year, c["m"],
                           calendar.monthrange(year, c["m"])[1])
            if first <= span_end:
                windows.append((first, min(last, span_end)))
    if goal is not None and not savings.is_plan_only(goal) and windows:
        # Each budgeted month read exactly as its Month lens cell reads it
        # (savings.month_saved): the destination's own net whenever it has
        # rows that month, the checking-leg evidence only for a silent one.
        # One rule, so the row is the sum of the cells under it.
        saved_ytd = _R(sum(_R(savings.month_saved(
            conn, _lens_cfg, goal, a, b)) for a, b in windows))

    detail = None
    if with_detail:
        detail = _year_detail(conn, year, today, memo, snap_months)
        # net by year, back to the first year the ledger holds: the same
        # cash-flow definitions as `totals`, one grouped pass over every
        # month. Judged by the month, as the year's own headline (which
        # sums the month cells) and Cash Flow are: a month no bank account
        # feeds is left out of the year's income and net — it would
        # otherwise add its whole spend as a loss — and the year is marked
        # `incomplete`; a year with no measured month has no net
        all_months = {r["p"]: r["amt"] for r in conn.execute(
            f"SELECT to_char(t.date,'YYYY-MM') p, "
            f"SUM({reporting.NET_AMOUNT_JOINED}) amt "
            f"FROM transactions t {reporting.REIMB_PARTIAL_JOIN} "
            f"WHERE {reporting.SPEND_WHERE} AND t.date < %s {excl} "
            f"GROUP BY p", (dt.date(year + 1, 1, 1), *ex_p))}
        first = _first_year(conn) or year
        today_key = today.strftime("%Y-%m")
        years = []
        for yy in range(min(first, year), year + 1):
            keys = [f"{yy:04d}-{mo:02d}" for mo in range(1, 13)]
            spend = _R(sum(all_months.get(k, 0) or 0 for k in keys))
            elapsed = [k for k in keys if k <= today_key]
            known = [k for k in elapsed if cov.month_known(k)]
            inc = (_R(sum(income_m.get(k, 0) or 0 for k in known))
                   if known else None)
            if not years and not spend and not inc:
                # the ledger's first row may be an income record from
                # decades back: the strip starts at the first year with
                # money moving, not at the first year on file
                continue
            years.append({
                "y": yy, "income": inc, "spend": spend,
                "net": (_R(inc - sum(all_months.get(k, 0) or 0
                                     for k in known))
                        if known else None),
                "incomplete": 0 < len(known) < len(elapsed)})
        detail["years"] = years

    if surplus_st is not None:
        # the standing figure, not that month's own: a month with a third
        # paycheck or a refund is no measure of every other one
        from ..engine import forecast as _forecast
        as_of = surplus_st.get("as_of") or today
        closed = (as_of.year, as_of.month) < (today.year, today.month)
        # a closed month under the schedule and budget it was judged under
        plan_surplus = _forecast.standing_surplus(
            conn, surplus_st, as_of, memo=memo,
            cfg=surplus_st.get("snapshot_config") if closed else None,
            bill_rows=surplus_st.get("bill_rows") if closed else None,
            closed=closed)
    # judged by the month (the cells' own income, MeasuredWindow
    # .month_known), the rule Cash Flow's Left over by year and the yearly
    # email's year strip read too
    totals = cell_totals(cells, year=year, closed=year < today.year)
    prev_totals = cell_totals(prev_cells, year=year - 1,
                              closed=year - 1 < today.year)
    lens_goal = _lens_goal(conn, _lens_cfg, goal, today)
    buckets_annual = {k: {f: _R(v) for f, v in d.items()}
                      for k, d in annual.items()}
    # the map's Bills row reads the due-dated sums; the reply's annual
    # buckets keep their three figures
    fixed_map = dict(buckets_annual["fixed"],
                     **{k: _R(v) for k, v in annual_occ.items()})
    plan_surplus = _R(plan_surplus) if plan_surplus is not None else None
    # the year's money map covers the elapsed BUDGETED months (the final
    # and projected cells) — the months buckets_annual sums; a year with
    # none has no map
    budgeted = sum(1 for c in cells if c["status"] in ("final", "projected"))
    from . import bills_breakdown as _bb
    from . import envelope_breakdown as _eb
    year_pools = _eb.year(env_months, past=year < today.year)
    if detail is not None and year_pools is not None:
        # the yearly email's pinned pools print their Year line from these
        # (a current reply's marker: `pair_text`, row or no row)
        for it in sorted(year_pools["items"], key=_eb.listed):
            e = detail["envelopes"].get(it["payee"])
            if e is not None:
                e.update({k: it[k] for k in (
                    "uid", "pair_spent", "pair_of", "pair_past", "pair_text",
                    "period_months")})
    money_map = ({"plan_rows": None, "plan_total": None} if budgeted == 0
                 else _lens_map(
                     buckets_annual["food"], buckets_annual["other"],
                     fixed_map, y=year, m=None, pending=0.0,
                     savings=lens_savings_row(
                         lens_goal, saved_ytd, budgeted,
                         projected="estimate from the last 3 months"),
                     excess=None,
                     bills=(_bb.year(bill_months, past=year < today.year)
                            if bill_months is not None else None),
                     envelopes=year_pools))
    # what an ordinary month leaves, never a year total under the words
    # "this month", and nothing said to a year that is over budget to
    # date (the card's own Variable spending: actual against the pace of
    # the months it covers, the figure its "more than planned" reads;
    # specs/budget-snapshots.md, "The Year page's words")
    ytd_margin = sum(annual[k]["actual"] - annual[k]["expected"]
                     for k in ("food", "other"))
    excess_text = (f"An ordinary month leaves about "
                   f"${round(plan_surplus):,}"
                   if budgeted and plan_surplus is not None
                   and round(plan_surplus) > 0 and ytd_margin <= 0.5
                   else None)
    return {"y": year, "cells": cells, "detail": detail,
            "plan_caption": (plan_caption(cells)
                             if money_map["plan_rows"] is not None else None),
            "totals": totals,
            "prev_totals": prev_totals,
            # the cells and the totals date a bill by the month it belongs
            # to; Cash Flow dates spending by the day it was paid
            "dating": "bill_month",
            "prev_through": (prev_through.isoformat()
                             if prev_through is not None else None),
            "categories": categories,
            # each category's change from last year in the order of
            # `categories`, and the total spend's (reporting.change: the
            # printed figure minus the printed figure)
            "changes": {
                "categories": [reporting.change(c[1], c[2])
                               for c in categories],
                "spend": reporting.change(totals["spend"],
                                          prev_totals["spend"]),
            },
            "saved_ytd": saved_ytd,
            "savings_goal": lens_goal,
            "buckets_annual": buckets_annual,
            # the year's money map over those months, as Today's is sent
            **money_map,
            "plan_surplus": plan_surplus,
            "excess_text": excess_text,
            "first_year": _first_year(conn)}
