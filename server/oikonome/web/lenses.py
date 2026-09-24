"""Timeframe lenses (Week / Month / Year) over the frozen engine.

Every lens is a VIEW composed from `budget.month_status` outputs — the
verdict semantics, spend definition and bucket math change ZERO here. The
compose functions return JSON-safe dicts and are the single source for BOTH
the /api/lens/* endpoints and the weekly/monthly email bodies: an email
renderer calls the same function the SPA fetches, so the two surfaces
cannot diverge (the todayview rule, applied to lenses).

Composition rules:

* WEEK — Mon–Sun calendar weeks. The weekly budget is the sum of each
  day's DAILY ALLOWANCE (that day's month_budget ÷ days-in-month), so a
  week straddling two months mixes both months' allowances per-day.
  Verdict = the engine's frozen formula (variance vs max($50, 5% of
  expected)) applied at week scope; expected pro-rates to elapsed days.

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

from ..engine import budget, reporting
from ..engine import categories as _categories

# ---- shared helpers ---------------------------------------------------------

_R = lambda v: round(v, 2)  # noqa: E731 — payload rounding, one name


def monday_of(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


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
    return st


def _projection(st: dict, as_of: dt.date) -> dict:
    """Month-end pace projection for an in-progress month — the Today
    card's math (todayview.build_context proj), judged against the FULL
    variable budget with the frozen tolerance rule (at month end,
    expected = the full budget, so tolerance = max($50, 5% of budget)).

    The pace runs over ELAPSED days, the same fraction the verdict uses:
    today has not finished, so counting it as a completed $0 day would
    project an on-pace household with nothing spent yet this morning as
    under budget while the verdict says on budget. On day 1 nothing has
    elapsed and the projection is the plan itself."""
    var_budget = (st["buckets"]["food"]["month_budget"]
                  + st["buckets"]["other"]["month_budget"])
    elapsed = as_of.day - 1
    pace_total = (st["variable_actual"] / elapsed * st["days_in_month"]
                  if elapsed >= 1 else var_budget)
    variance = pace_total - var_budget
    tolerance = max(50.0, var_budget * 0.05)
    return {"pace_total": _R(pace_total), "variance": _R(variance),
            "tolerance": _R(tolerance), "verdict": _verdict(variance, tolerance)}


def _ser_txn(r, fixed: bool) -> dict:
    return {"date": r["date"].isoformat(), "amount": _R(r["amount"]),
            "payee": r["payee"], "category": r["category"],
            "account": r.get("account"), "pending": bool(r["pending"]),
            "fixed": fixed}


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
    rows = conn.execute(
        f"SELECT t.id, {reporting.PART_NET_JOINED} amt "
        f"FROM transactions t {reporting.REIMB_PARTIAL_JOIN} {reporting.SPLIT_JOIN} "
        f"WHERE {reporting.SPEND_WHERE} "
        f"AND t.date >= %s AND t.date < %s "
        f"AND {reporting.PART_CAT} = REPLACE(%s, '_', ' ')",
        (dt.date(year, 1, 1), dt.date(year + 1, 1, 1), key)).fetchall()
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
    for m in range(1, 13):
        if (year, m) > (today.year, today.month):
            continue
        if (year, m) < (today.year, today.month) and m not in snap_months:
            continue
        info = budget.bucket_ledger(
            month_state(conn, year, m, today, memo=memo), bucket)
        if info is None:
            continue
        label = label or info["label"]
        amount += info["amount"]
        for t in info["txn_ids"]:
            if t not in seen:
                seen.add(t)
                ids.append(t)
    if label is None:
        return None
    return {"bucket": bucket, "label": label, "amount": _R(amount),
            "txn_ids": ids}


def _first_year(conn) -> int | None:
    row = conn.execute("SELECT MIN(date) AS d FROM transactions "
                       "WHERE removed = 0").fetchone()
    return row["d"].year if row and row["d"] else None


# ---- week lens --------------------------------------------------------------


def week_summary(conn, start: dt.date, today: dt.date | None = None) -> dict:
    """One Mon–Sun week. `start` snaps to its Monday. Budgets/expectations
    sum per-day allowances (month_budget ÷ days-in-month for each day's
    OWN month), so month-straddling weeks are exact by construction."""
    today = today or dt.date.today()
    start = monday_of(start)
    days = [start + dt.timedelta(days=i) for i in range(7)]
    end = days[-1]

    states: dict[tuple[int, int], dict] = {}
    memo: dict = {}
    for d in {(d.year, d.month) for d in days}:
        states[d] = month_state(conn, d[0], d[1], today, memo=memo)

    def st_of(d: dt.date) -> dict:
        return states[(d.year, d.month)]

    # per-day daily allowance, per bucket + custom-bucket children (each
    # month's own — dynamic scaling included, since month_budget is the
    # engine's already-scaled number)
    def allowance(d: dt.date, bucket: str, child: str | None = None) -> float:
        st = st_of(d)
        b = st["buckets"][bucket]
        if child is not None:
            c = next((c for c in b.get("children") or []
                      if c["name"] == child), None)
            monthly = c["month_budget"] if c else 0.0
        else:
            monthly = b["month_budget"]
        return monthly / st["days_in_month"]

    covered = [d for d in days if d <= today]
    in_week = lambda r: start <= r["date"] <= end  # noqa: E731

    buckets: dict[str, dict] = {}
    child_names: dict[str, list[str]] = {}
    for k in ("food", "other"):
        names: list[str] = []
        for st in states.values():
            for c in st["buckets"][k].get("children") or []:
                if c["name"] not in names:
                    names.append(c["name"])
        child_names[k] = names
        rows = [r for st in states.values()
                for r in st["buckets"][k]["rows"] if in_week(r)]
        kids = []
        carved = 0.0
        for name in names:
            crows = [r for st in states.values()
                     for c in st["buckets"][k].get("children") or []
                     if c["name"] == name for r in c["rows"] if in_week(r)]
            cactual = sum(r["amount"] for r in crows)
            carved += cactual
            kids.append({
                "name": name, "actual": _R(cactual),
                "week_budget": _R(sum(allowance(d, k, name) for d in days)),
                "expected": _R(sum(allowance(d, k, name) for d in covered))})
        wk_budget = sum(allowance(d, k) for d in days)
        buckets[k] = {
            "actual": _R(sum(r["amount"] for r in rows)),
            "week_budget": _R(wk_budget),
            "expected": _R(sum(allowance(d, k) for d in covered)),
            "children": kids,
            "display_actual": _R(sum(r["amount"] for r in rows) - carved),
            "remaining_budget": _R(max(
                0.0, wk_budget - sum(c["week_budget"] for c in kids))),
        }

    fixed_rows = [r for st in states.values()
                  for r in st["buckets"]["fixed"]["rows"] if in_week(r)]
    var_rows = [r for st in states.values() for k in ("food", "other")
                for r in st["buckets"][k]["rows"] if in_week(r)]

    # Bills PLANNED for the week (the money map's Bills row): occurrences
    # whose due date falls Mon–Sun, expanded and paid/unpaid-matched by the
    # ENGINE's own machinery (month_occurrences + match_occurrences — the
    # month_status pipeline, no new cadence rules), plus each day's share
    # of envelope pools. `awaiting` mirrors Today's unpaid_due rule:
    # due date arrived (≤ today) and still unmatched. A bill due this week
    # but paid in an adjacent week shows as planned here and posted there —
    # inherent week-boundary noise, same as an early payment on Today.
    cfg = next(iter(states.values()))["config"]
    bills = budget.drop_card_pay_bills(conn, budget._recurring_bills(
        conn, caps=cfg.get("occurrence_caps"),
        disabled=cfg.get("disabled_bills")))
    week_bills = awaiting = 0.0
    for (yy, mm), mst in states.items():
        # The same candidate set month_status matches over: rows from
        # PREPAY_MATCH_DAYS before the month, and next month's occurrences
        # as decoys — so a bill due the 1st that autopaid on the 28th reads
        # as settled here too, not as awaiting. Only THIS month's
        # occurrences are counted; a next-month one paid early is counted
        # by its own month's pass, whose lookback sees the same payment.
        occs = budget.month_occurrences(bills, yy, mm)
        n_this = len(occs)
        ny, nm = (yy, mm + 1) if mm < 12 else (yy + 1, 1)
        occs += budget.month_occurrences(bills, ny, nm)
        mrows = budget._spend_rows(
            conn, dt.date(yy, mm, 1) - dt.timedelta(days=budget.PREPAY_MATCH_DAYS),
            mst["as_of"] + dt.timedelta(days=1),
            excluded=budget.excluded_account_ids(cfg), memo=memo)
        _, occs = budget.match_occurrences(mrows, occs)
        for occ in occs[:n_this]:
            if not (start <= occ["due"] <= end):
                continue
            if occ["floored"] and occ["txn"] is None:
                continue        # engine rule: floored + unpaid ≠ expected
            week_bills += occ["planned"]
            if occ["txn"] is None and occ["due"] <= today:
                awaiting += occ["planned"]
    # Envelope share per day — the forecast's drip, not the plan rate:
    # what's LEFT of each pool over the days left in its period. A flat
    # monthly ÷ days-in-month share would keep charging an exhausted pool,
    # planning money the engine says is already spent. Days in the
    # current month drip max(0, pool − used) over tomorrow→EOM; other
    # months' pools are untouched (or final) and drip the plan rate, the
    # same rate step the forecast walks across a period roll. Annual pools
    # drip their remaining YEAR pool over the days left in the
    # year. month_status already did the pool accounting — reuse it.
    days_left_month = (calendar.monthrange(today.year, today.month)[1]
                       - today.day)
    days_left_year = (dt.date(today.year, 12, 31) - today).days
    for d1 in days:
        st = st_of(d1)
        for e in st["buckets"]["fixed"]["envelopes"]:
            if e["period_months"] >= 12:            # annual pool
                if d1.year == today.year:
                    week_bills += (e["period_left"] / days_left_year
                                   if days_left_year else 0.0)
                else:                               # pool resets Jan 1
                    week_bills += e["pool"] / (
                        366 if calendar.isleap(d1.year) else 365)
            elif (d1.year, d1.month) == (today.year, today.month):
                week_bills += (max(0.0, e["pool"] - e["used"])
                               / days_left_month if days_left_month else 0.0)
            else:                                   # pool resets at the 1st
                week_bills += e["pool"] / st["days_in_month"]

    actual = buckets["food"]["actual"] + buckets["other"]["actual"]
    expected = buckets["food"]["expected"] + buckets["other"]["expected"]
    week_budget = buckets["food"]["week_budget"] + buckets["other"]["week_budget"]
    variance = actual - expected
    tolerance = max(50.0, expected * 0.05)

    per_day = []
    for d in days:
        per_day.append({
            "date": d.isoformat(),
            "variable": _R(sum(r["amount"] for r in var_rows if r["date"] == d)),
            "fixed": _R(sum(r["amount"] for r in fixed_rows if r["date"] == d)),
        })

    txns = sorted((r for r in var_rows + fixed_rows),
                  key=lambda r: (r["date"], -r["amount"]), reverse=True)
    fixed_ids = {id(r) for r in fixed_rows}
    status = ("future" if start > today
              else "in_progress" if end >= today else "final")
    # at week scope: plan surplus is a STANDING monthly metric
    # (config-derived — the same number Today shows); the SPA scales it
    # to the timeframe like the savings row (planRows opts.scale)
    plan_surplus = next(iter(states.values())).get("plan_surplus")
    return {
        "start": start.isoformat(), "end": end.isoformat(),
        "status": status, "as_of": today.isoformat(),
        "verdict": _verdict(variance, tolerance),
        "variance": _R(variance), "tolerance": _R(tolerance),
        "actual": _R(actual), "expected": _R(expected),
        "week_budget": _R(week_budget),
        "remaining": _R(week_budget - actual),
        "days": per_day,
        "buckets": {**buckets,
                    "fixed": {"actual": _R(sum(r["amount"] for r in fixed_rows)),
                              "count": len(fixed_rows),
                              # bills planned for / awaiting in the week —
                              # the money map's Bills row (posted green,
                              # awaiting amber, remaining light)
                              "week_budget": _R(week_bills),
                              "awaiting": _R(awaiting)}},
        "txns": [_ser_txn(r, id(r) in fixed_ids) for r in txns],
        "plan_surplus": (_R(plan_surplus)
                         if plan_surplus is not None else None),
    }


# ---- month lens -------------------------------------------------------------


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
    biggest = sorted(st["rows"], key=lambda r: -r["amount"])[:10]
    income_actual = next(
        (r["amt"] for r in reporting._income_by(conn, "month")
         if r["p"] == f"{year:04d}-{month:02d}"), None)
    paid = sorted(b["fixed"].get("occ_rows") or [],
                  key=lambda r: r["date"])
    # The month map's Savings row actual. rate_90d is a trailing 90-day
    # average, not what THIS month saved, so the figure comes from the same
    # source as the Year lens' saved_ytd: the ledger's posted contribution
    # to the standing "Savings" goal within this month, checking-leg
    # evidence included (posted_for_plan).
    from ..engine import savings as _savings
    _cfg = budget.load_config(conn)
    saved_month = None
    _goal = next((g for g in _savings.goals(_cfg)
                  if str(g["name"]) == "Savings"), None)
    if (_goal is not None and not _savings.is_plan_only(_goal)
            and status != "future"):
        _first = dt.date(year, month, 1)
        _last = dt.date(year, month, calendar.monthrange(year, month)[1])
        saved_month = _R(_savings.posted_for_plan(
            conn, _cfg, _goal, since=_first, until=min(_last, today)))
    return {
        "y": year, "m": month, "status": status,
        "as_of": as_of.isoformat(), "days_in_month": st["days_in_month"],
        "verdict": st["verdict"], "variance": _R(st["variance"]),
        "tolerance": _R(st["tolerance"]),
        "projection": (_projection(st, as_of)
                       if status == "in_progress" else None),
        "variable_actual": _R(st["variable_actual"]),
        "variable_budget": _R(b["food"]["month_budget"]
                              + b["other"]["month_budget"]),
        "buckets": {k: bucket(k) for k in ("fixed", "food", "other")},
        "bills": {
            "posted": _R(b["fixed"]["actual"]),
            "planned": _R(b["fixed"]["month_budget"]),
            "awaiting": _R(b["fixed"].get("unpaid_due") or 0),
            "paid": [{"date": r["date"].isoformat(),
                      "amount": _R(r["amount"]), "payee": r["payee"]}
                     for r in paid],
            "overdue": [[p, _R(a), due]
                        for p, a, due in st.get("overdue_unpaid") or []],
            "envelopes": [{"payee": e["payee"], "used": _R(e["used"]),
                           "monthly": _R(e["monthly"]),
                           "overflow": _R(e["overflow"])}
                          for e in b["fixed"].get("envelopes") or []],
        },
        "by_category": mtd, "amazon_subs": amazon_subs,
        "amazon_total": amazon_total,
        "biggest": [_ser_txn(r, False) for r in biggest],
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


def year_summary(conn, year: int, today: dt.date | None = None) -> dict:
    """12 verdict cells + annual totals + category totals vs prior year.
    Verdict cells cover months with a budget (a frozen snapshot, or the
    live config for the current month); a closed month with no snapshot
    is a "net" cell — income vs spending, no verdict."""
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
    # bank coverage decides whether a net cell's income is a number or
    # unknown — same rule as the annual totals below
    covered = reporting._bank_coverage_years(conn)
    # one pass over the bank rows serves both the annual totals below and
    # the net cells' months
    income_y, income_m = reporting._income_year_month(conn)
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
                    f"AND t.date >= %s AND t.date < %s GROUP BY p",
                    (dt.date(year, 1, 1), dt.date(year + 1, 1, 1)))}
            key = f"{year:04d}-{m:02d}"
            spend = _R(spend_m.get(key, 0) or 0)
            inc = (_R(income_m.get(key, 0) or 0)
                   if str(year) in covered else None)
            cells.append({"m": m, "status": "net", "verdict": None,
                          "variance": None, "variable_actual": None,
                          "variable_budget": None,
                          "income": inc, "spend": spend,
                          "net": (_R(inc - spend)
                                  if inc is not None else None)})
            continue
        st = month_state(conn, year, m, today, memo=memo)
        if st.get("plan_surplus") is not None:
            plan_surplus = st["plan_surplus"]
        for k in ("food", "other", "fixed"):
            b = st["buckets"][k]
            annual[k]["actual"] += b["actual"]
            annual[k]["expected"] += b["expected"]
            annual[k]["month_budget"] += b["month_budget"]
        var_budget = _R(st["buckets"]["food"]["month_budget"]
                        + st["buckets"]["other"]["month_budget"])
        if (year, m) == (today.year, today.month):
            proj = _projection(st, st["as_of"])
            cells.append({"m": m, "status": "projected",
                          "verdict": proj["verdict"],
                          "variance": proj["variance"],
                          "variable_actual": _R(st["variable_actual"]),
                          "variable_budget": var_budget})
        else:
            cells.append({"m": m, "status": "final",
                          "verdict": st["verdict"],
                          "variance": _R(st["variance"]),
                          "variable_actual": _R(st["variable_actual"]),
                          "variable_budget": var_budget})

    # annual totals — the Cash Flow report's frozen definitions (income:
    # bank deposits net of own-money transfers; spend: SPEND_WHERE), scoped
    # to the two years the lens shows. Years without bank data have unknown
    # income, not $0 (`covered`, computed above for the net cells too).
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
        f"AND t.date >= %s AND t.date < %s GROUP BY GROUPING SETS ((yr), (yr, cat))",
        (dt.date(year - 1, 1, 1), dt.date(year + 1, 1, 1))).fetchall()
    spend_y = {r["yr"]: r["amt"] for r in two_years if r["gc"]}

    def totals(y: int) -> dict:
        ys = str(y)
        spend = _R(spend_y.get(ys, 0) or 0)
        if ys not in covered:
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
    # elapsed span. None when there is nothing to measure
    # (no goal / plan-only), and the SPA falls back to its extrapolation.
    from ..engine import savings
    saved_ytd = None
    _lens_cfg = budget.load_config(conn)
    goal = next((g for g in savings.goals(_lens_cfg)
                 if str(g["name"]) == "Savings"), None)
    if goal is not None and not savings.is_plan_only(goal):
        span_end = min(today, dt.date(year, 12, 31))
        span_start = dt.date(year, 1, 1)
        if span_end >= span_start:
            # sibling-aware dest net is ledger truth when the dest
            # account actually has transfer rows. When dest is silent
            # (unsynced / external), the month lens already counts
            # checking-leg posted_for_plan — year must be the sum of
            # those same monthly windows or the map shows $0 for a year
            # whose months each show a figure.
            dest = savings.matched_net(
                conn, _lens_cfg, goal, since=span_start, until=span_end)
            dest_rows = 0
            if not dest and goal.get("account_id"):
                dest_rows = conn.execute(
                    f"""SELECT COUNT(*) AS n FROM transactions t
                        WHERE t.removed = 0 AND t.account_id = %s
                          AND t.date >= %s AND t.date <= %s
                          AND COALESCE(t.category_override,
                                       t.category_primary, '')
                              IN ('TRANSFER_IN','TRANSFER_OUT')
                          {budget.PERSONAL_ONLY_SQL}""",
                    (goal["account_id"], span_start, span_end)
                ).fetchone()["n"]
            if dest or dest_rows:
                saved_ytd = _R(dest)
            else:
                total = 0.0
                m = span_start
                while m <= span_end:
                    last = dt.date(
                        m.year, m.month,
                        calendar.monthrange(m.year, m.month)[1])
                    total += savings.posted_for_plan(
                        conn, _lens_cfg, goal, since=m,
                        until=min(last, span_end))
                    if m.month == 12:
                        break
                    m = dt.date(m.year, m.month + 1, 1)
                saved_ytd = _R(total)

    return {"y": year, "cells": cells,
            "totals": totals(year), "prev_totals": totals(year - 1),
            "categories": categories, "saved_ytd": saved_ytd,
            "buckets_annual": {k: {f: _R(v) for f, v in d.items()}
                               for k, d in annual.items()},
            "plan_surplus": (_R(plan_surplus)
                             if plan_surplus is not None else None),
            "first_year": _first_year(conn)}
