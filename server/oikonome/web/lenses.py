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

from ..engine import budget, reporting
from ..engine import categories as _categories

# ---- shared helpers ---------------------------------------------------------

_R = lambda v: round(v, 2)  # noqa: E731 — payload rounding, one name


def _verdict(variance: float, tolerance: float) -> str:
    """The engine's frozen verdict formula (budget.month_status), reused
    verbatim at other scopes — no new judgment rules."""
    return ("ON BUDGET" if abs(variance) <= tolerance
            else "OVER BUDGET" if variance > 0 else "UNDER BUDGET")


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
    a year page share are read once, not once per month."""
    last = calendar.monthrange(y, m)[1]
    snap = frozen = None
    if (y, m) == (today.year, today.month):
        as_of = today
    elif (y, m) > (today.year, today.month):
        as_of = dt.date(y, m, 1)
    else:
        as_of = dt.date(y, m, last)
        snap = budget.snapshot_config(conn, y, m)
        frozen = budget.snapshot_bill_rows(conn, y, m)
    st = budget.month_status(conn, as_of, historical=False, cfg=snap,
                             bill_rows=frozen, memo=memo,
                             closed=(y, m) < (today.year, today.month))
    st["as_of"] = as_of
    st["budget_source"] = "snapshot" if snap is not None else "live"
    st["bills_source"] = "snapshot" if frozen is not None else "live"
    st["bill_rows"] = frozen    # the schedule the month was judged under
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


def _judged(conn, st: dict) -> bool:
    """Does a budget the household made stand behind this month? A closed
    month's frozen snapshot does (snapshots are only taken of a saved
    budget); otherwise the stored settings must carry one
    (budget.budgets_set)."""
    return st["budget_source"] == "snapshot" or budget.budgets_set(conn)


def _ser_txn(r, fixed: bool) -> dict:
    return {"date": r["date"].isoformat(), "amount": _R(r["amount"]),
            "payee": r["payee"], "category": r["category"],
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
        by_cat[r["category"]] = by_cat.get(r["category"], 0.0) + r["amount"]
        if r["category"] not in keys:
            keys[r["category"]] = r.get("stored_category")
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
    label = next((r["category"] for r in st["rows"]
                  if r.get("stored_category") == key), None)
    rows = [r for r in st["rows"] if label is not None and r["category"] == label]
    ids: list[str] = []
    seen: set[str] = set()
    for r in rows:
        tid = r.get("txn_id") or r.get("overflow_of")
        if tid and tid not in seen:
            seen.add(tid)
            ids.append(tid)
    return {"txn_ids": ids, "amount": _R(sum(r["amount"] for r in rows))}


def year_category_ledger(conn, key: str, year: int) -> dict:
    """The rows a year lens category label summed, and its number: the
    same spend predicate and reimbursement netting as `year_summary`'s
    category totals, matched on the display form the label groups by."""
    excl, ex_p = reporting.excluded_accounts_sql(conn)
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
                e["month_budget"] += c["month_budget"]
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


def _first_year(conn) -> int | None:
    row = conn.execute("SELECT MIN(date) AS d FROM transactions "
                       "WHERE removed = 0").fetchone()
    return row["d"].year if row and row["d"] else None


class _Coverage:
    """Which months have known income. A month's income is a number only
    once a bank account feeds the ledger: from the month of the first bank
    row on record, in a year that has bank rows at all (the Cash Flow
    report's year rule). Deciding it per YEAR alone would read the months
    of a year before its checking feed starts as income $0, so each would
    show its whole spend as a loss; those months are unknown instead.
    Excluded, hidden and linked-backup accounts do not count, for the
    reason _bank_coverage_years gives: their rows are invisible to the
    income figure. The rule is reporting._income_coverage itself, not a
    copy of it, so the lenses and the Cash Flow page cannot disagree about
    which months are known."""

    def __init__(self, conn):
        self.years, self.start = reporting._income_coverage(conn)

    def month(self, key: str) -> bool:
        """`key` is 'YYYY-MM'."""
        return (self.start is not None and key >= self.start
                and key[:4] in self.years)

    def whole_year(self, y: int) -> bool:
        """Every month of `y` is covered, so a sum over the year is known."""
        return self.month(f"{y:04d}-01")


# ---- week lens --------------------------------------------------------------


def month_summary(conn, year: int, month: int,
                  today: dt.date | None = None) -> dict:
    """The month report card. Past months: FINAL (the engine's verdict at
    month end, frac=1). Current month: in-progress + pace projection.
    Future months: the plan (budgets + scheduled bills, no spend)."""
    today = today or dt.date.today()
    st = month_state(conn, year, month, today, memo={})
    as_of = st["as_of"]
    status = ("final" if (year, month) < (today.year, today.month)
              else "in_progress" if (year, month) == (today.year, today.month)
              else "future")
    b = st["buckets"]
    # a verdict needs a budget the household made: a frozen snapshot, or a
    # saved one — never the loader's unsaved first-month guess (the Today
    # page, the app and the daily email gate on the same answer)
    judged = _judged(conn, st)

    def bucket(k):
        v = b[k]
        out = {"actual": _R(v["actual"]), "expected": _R(v["expected"]),
               "month_budget": _R(v["month_budget"])}
        if "children" in v:     # decomposition, carried like Today
            out["children"] = [{"name": c["name"], "actual": _R(c["actual"]),
                                "expected": _R(c["expected"]),
                                "month_budget": _R(c["month_budget"])}
                               for c in v["children"]]
            out["display_actual"] = _R(v["display_actual"])
            out["display_expected"] = _R(v["display_expected"])
            out["remaining_budget"] = _R(v["remaining_budget"])
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
    income_actual = (next((r["amt"] for r in reporting._income_by(conn, "month")
                           if r["p"] == key), None) or 0
                     if status != "future" and _Coverage(conn).month(key)
                     else None)
    paid = sorted(b["fixed"].get("occ_rows") or [],
                  key=lambda r: r["date"])
    # The month map's Savings row actual. rate_90d is a trailing 90-day
    # average, not what THIS month saved, so the figure comes from the same
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
    return {
        "y": year, "m": month, "status": status,
        "as_of": as_of.isoformat(), "days_in_month": st["days_in_month"],
        "verdict": st["verdict"] if judged else None,
        "variance": _R(st["variance"]) if judged else 0.0,
        "tolerance": _R(st["tolerance"]),
        # false: no budget of the household's own stands behind this
        # month, so there is no verdict and no projection to show
        "budget_set": judged,
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
        "buckets": {k: bucket(k) for k in ("fixed", "food", "other")},
        "bills": {
            "posted": _R(b["fixed"]["actual"]),
            "planned": _R(b["fixed"]["month_budget"]),
            "awaiting": _R(b["fixed"].get("unpaid_due") or 0),
            # category: the transaction category on the matched charge
            # (what the bill stamps, or the feed's) and the feed's detailed
            # one, so a reader can see the bills by kind — mortgage,
            # utilities, subscriptions
            "paid": [{"date": r["date"].isoformat(),
                      "amount": _R(r["amount"]), "payee": r["payee"],
                      "category": r.get("category"),
                      "detailed": r.get("category_detailed")}
                     for r in paid],
            # [payee, amount, due, withdrawn_on] — see month_status
            "overdue": [[p, _R(a), due, gone]
                        for p, a, due, gone in budget.overdue_entries(st)],
            # the pool and the period are what "how close to the cap" means
            # for an envelope that accumulates over several months — `used`
            # alone is this month's swipes against a cap that is not monthly
            "envelopes": [{"payee": e["payee"], "used": _R(e["used"]),
                           "monthly": _R(e["monthly"]),
                           "overflow": _R(e["overflow"]),
                           "pool": _R(e.get("pool", e["monthly"])),
                           "period_months": int(e.get("period_months") or 1),
                           "period_used": _R(e.get("period_used", e["used"])),
                           "period_left": _R(e.get("period_left", 0)),
                           # "pin to Today": the household watches this cap
                           # like a budget of its own
                           "show_today": bool(e.get("show_today"))}
                          for e in b["fixed"].get("envelopes") or []],
        },
        "by_category": mtd, "amazon_subs": amazon_subs,
        "amazon_total": amazon_total,
        "by_category_variable": mtd_variable,
        "biggest": [_ser_txn(r, False) for r in biggest],
        # the same list with the bill payments and envelope swipes out —
        # a mortgage is always the biggest row, and says nothing
        "biggest_variable": _with_items(conn, [
            _variable_ser(r) for r in biggest_variable]),
        "income": ({"actual": _R(income_actual)
                    if income_actual is not None else None,
                    "budgeted": _R(st["income"])
                    if st.get("income") is not None else None}),
        "spend_total": _R(st["total_actual"]),
        "variable_scale": ({"factor": round(st["variable_scale"]["factor"], 4),
                            "static_total": _R(st["variable_scale"]["static_total"]),
                            "available": _R(st["variable_scale"]["available"])}
                           if st.get("variable_scale") else None),
        # the standing monthly plan surplus (the excess-cash figure,
        # same source as Today) — the month map's Excess cash row
        "plan_surplus": (_R(st["plan_surplus"])
                         if st.get("plan_surplus") is not None else None),
        "saved_month": saved_month,
        # 'snapshot' when a closed month was judged against its frozen
        # budget; 'live' = today's config (current/future months, or a
        # closed month that predates snapshots — the SPA says so)
        "budget_source": st["budget_source"],
        # same flag for the bill schedule — 'live' on a closed month means
        # its snapshot predates bill freezing, so today's schedule applies
        "bills_source": st["bills_source"],
        "first_year": _first_year(conn),
    }


# ---- year lens --------------------------------------------------------------


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
    cells = []
    # annual per-bucket aggregates for the year money map —
    # PlanBucket-shaped so the SPA's planRows() renders them unchanged.
    # Sums cover the elapsed BUDGETED months only (a July view shows seven
    # months of plan, not a phantom full-year budget — and a no-snapshot
    # month contributes nothing, since no plan existed for it).
    annual = {k: {"actual": 0.0, "expected": 0.0, "month_budget": 0.0}
              for k in ("food", "other", "fixed")}
    # standing monthly plan surplus, from the latest elapsed month's state
    # (None when nothing has elapsed — a future year has no excess to map)
    plan_surplus = None
    snap_months = budget.snapshot_months(conn, year)
    # bank coverage decides whether a cell's income is a number or unknown,
    # month by month (the Month lens hero's rule); the annual totals below
    # sum those months, so they are known only when every month is
    cov = _Coverage(conn)
    # one pass over the bank rows serves both the annual totals below and
    # the net cells' months
    income_y, income_m = reporting._income_year_month(conn)
    # the income above drops the accounts the household excluded from the
    # budget; every spend read below drops them too, or saved, the savings
    # rate and the net cells would set filtered income against unfiltered
    # spend and disagree with Cash Flow and with the month cells
    excl, ex_p = reporting.excluded_accounts_sql(conn)
    spend_m: dict | None = None     # lazy — only no-snapshot months pay
    memo: dict = {}                 # one engine cache across the 12 months
    for m in range(1, 13):
        if (year, m) > (today.year, today.month):
            cells.append({"m": m, "status": "future", "verdict": None,
                          "variance": None, "variable_actual": None,
                          "variable_budget": None})
            continue
        if (year, m) < (today.year, today.month) and m not in snap_months:
            # no budget was recorded for this closed month — show the
            # cash-flow fact (income vs spending), never a verdict against
            # a budget that didn't exist yet
            if spend_m is None:
                spend_m = {r["p"]: r["amt"] for r in conn.execute(
                    f"SELECT to_char(t.date,'YYYY-MM') p, "
                    f"SUM({reporting.NET_AMOUNT_JOINED}) amt "
                    f"FROM transactions t {reporting.REIMB_PARTIAL_JOIN} "
                    f"WHERE {reporting.SPEND_WHERE} "
                    f"AND t.date >= %s AND t.date < %s {excl} GROUP BY p",
                    (dt.date(year, 1, 1), dt.date(year + 1, 1, 1), *ex_p))}
            key = f"{year:04d}-{m:02d}"
            spend = _R(spend_m.get(key, 0) or 0)
            inc = (_R(income_m.get(key, 0) or 0)
                   if cov.month(key) else None)
            cells.append({"m": m, "status": "net", "verdict": None,
                          "variance": None, "variable_actual": None,
                          "variable_budget": None,
                          "income": inc, "spend": spend,
                          "net": (_R(inc - spend)
                                  if inc is not None else None)})
            continue
        st = month_state(conn, year, m, today, memo=memo)
        if not _judged(conn, st):
            # the month in progress with no budget the household saved:
            # the cash-flow fact a no-snapshot month shows, never a
            # verdict on the loader's unsaved guess
            key = f"{year:04d}-{m:02d}"
            inc = (_R(income_m.get(key, 0) or 0)
                   if cov.month(key) else None)
            spend = _R(st["total_actual"])
            cells.append({"m": m, "status": "net", "verdict": None,
                          "variance": None, "variable_actual": None,
                          "variable_budget": None,
                          "income": inc, "spend": spend,
                          "net": (_R(inc - spend)
                                  if inc is not None else None)})
            continue
        if st.get("plan_surplus") is not None:
            plan_surplus = st["plan_surplus"]
        for k in ("food", "other", "fixed"):
            b = st["buckets"][k]
            annual[k]["actual"] += b["actual"]
            annual[k]["expected"] += b["expected"]
            annual[k]["month_budget"] += b["month_budget"]
        var_budget = _R(st["variable_budget_paced"])
        # the cash-flow fact beside the verdict, the way the Month lens
        # hero reads it (income from the bank series, spend = the month's
        # total out), so a reader walking the year sees what each month
        # kept or lost, not only how it was judged
        key = f"{year:04d}-{m:02d}"
        inc = (_R(income_m.get(key, 0) or 0)
               if cov.month(key) else None)
        spend = _R(st["total_actual"])
        flow = {"income": inc, "spend": spend,
                "net": _R(inc - spend) if inc is not None else None}
        if (year, m) == (today.year, today.month):
            proj = _projection(st)
            cells.append({"m": m, "status": "projected",
                          "verdict": proj["verdict"],
                          "variance": proj["variance"],
                          "variable_actual": _R(st["variable_actual"]),
                          "variable_budget": var_budget, **flow})
        else:
            cells.append({"m": m, "status": "final",
                          "verdict": st["verdict"],
                          "variance": _R(st["variance"]),
                          "variable_actual": _R(st["variable_actual"]),
                          "variable_budget": var_budget, **flow})

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
    two_years = conn.execute(
        f"SELECT to_char(t.date,'YYYY') yr, {reporting.PART_CAT} cat, "
        f"MIN(COALESCE({_categories.PART_CAT}, '?')) cat_key, "
        f"SUM({reporting.PART_NET_JOINED}) amt, GROUPING({reporting.PART_CAT}) gc "
        f"FROM transactions t {reporting.REIMB_PARTIAL_JOIN} {reporting.SPLIT_JOIN} "
        f"WHERE {reporting.SPEND_WHERE} "
        f"AND t.date >= %s AND t.date < %s {excl} "
        f"GROUP BY GROUPING SETS ((yr), (yr, cat))",
        (dt.date(year - 1, 1, 1), dt.date(year + 1, 1, 1), *ex_p)).fetchall()
    spend_y = {r["yr"]: r["amt"] for r in two_years if r["gc"]}

    def totals(y: int) -> dict:
        ys = str(y)
        spend = _R(spend_y.get(ys, 0) or 0)
        if not cov.whole_year(y):
            return {"income": None, "spend": spend, "saved": None, "rate": None}
        inc = income_y.get(ys, 0) or 0
        return {"income": _R(inc), "spend": spend, "saved": _R(inc - spend),
                "rate": round(100 * (inc - spend) / inc, 1) if inc else None}

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
        # The goal's own measure over the budgeted months: the sibling-aware
        # destination net is ledger truth (a withdrawal subtracts) whenever
        # the destination has transfer rows in them. Only a silent
        # destination (unsynced, external) is read month by month from the
        # plan evidence the Month lens shows, or the map would read $0 for
        # a year whose months each show a figure.
        dest = sum(savings.matched_net(conn, _lens_cfg, goal,
                                       since=a, until=b)
                   for a, b in windows)
        dest_rows = 0
        if not dest and goal.get("account_id"):
            # the same owner scope the goal's own figures use, so a goal
            # on a business-entity account is not read as silent
            scope, scope_params = savings._owner_scope(conn, goal)
            for a, b in windows:
                dest_rows += conn.execute(
                    f"""SELECT COUNT(*) AS n FROM transactions t
                        WHERE t.removed = 0 AND t.account_id = %s
                          AND t.date >= %s AND t.date <= %s
                          AND COALESCE(t.category_override,
                                       t.category_primary, '')
                              IN ('TRANSFER_IN','TRANSFER_OUT')
                          {scope}""",
                    (savings._goal_account(conn, goal), a, b, *scope_params)
                ).fetchone()["n"]
        if dest or dest_rows:
            saved_ytd = _R(dest)
        else:
            saved_ytd = _R(sum(_R(savings.month_saved(
                conn, _lens_cfg, goal, a, b)) for a, b in windows))

    detail = None
    if with_detail:
        detail = _year_detail(conn, year, today, memo, snap_months)
        # net by year, back to the first year the ledger holds: the same
        # cash-flow definitions as `totals`, one grouped pass over every
        # year. A year with any month no bank account feeds has unknown
        # income, no net: the months before the feed starts would otherwise
        # add their whole spend as a loss, and the strip would disagree
        # with the year's own headline, which sums the month cells
        all_years = {r["yr"]: r["amt"] for r in conn.execute(
            f"SELECT to_char(t.date,'YYYY') yr, "
            f"SUM({reporting.NET_AMOUNT_JOINED}) amt "
            f"FROM transactions t {reporting.REIMB_PARTIAL_JOIN} "
            f"WHERE {reporting.SPEND_WHERE} AND t.date < %s {excl} "
            f"GROUP BY yr", (dt.date(year + 1, 1, 1), *ex_p))}
        first = _first_year(conn) or year
        years = []
        for yy in range(min(first, year), year + 1):
            ys = str(yy)
            spend = _R(all_years.get(ys, 0) or 0)
            inc = (_R(income_y.get(ys, 0) or 0) if cov.whole_year(yy)
                   else None)
            if not years and inc is None and not spend:
                # the ledger's first row may be an income record from
                # decades back: the strip starts at the first year with
                # money moving, not at the first year on file
                continue
            years.append({"y": yy, "income": inc, "spend": spend,
                          "net": _R(inc - spend) if inc is not None else None})
        detail["years"] = years

    return {"y": year, "cells": cells, "detail": detail,
            "totals": totals(year), "prev_totals": totals(year - 1),
            "categories": categories, "saved_ytd": saved_ytd,
            "buckets_annual": {k: {f: _R(v) for f, v in d.items()}
                               for k, d in annual.items()},
            "plan_surplus": (_R(plan_surplus)
                             if plan_surplus is not None else None),
            "first_year": _first_year(conn)}
