"""Today, lenses, budget snapshots, activity, alerts and calendar.
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

import datetime as dt
import re
from fastapi import Depends, HTTPException, Query, Request

from . import data, demoguard
from ..db import tenancy
from ..engine import activity as _activity
from .api_common import (_budgets_set, _category_why, _ck_year, _ck_ym,
                         _conn, _ser, _stamp_recurring, _today, _user,
                         router)
from .fields import Fields, iso_date as _iso_date, json_fields
from .schemas import documented
from .schemas.today import (ActivityPage, AlertHistory, AlertKey,
                            AlertsDismissed, AlertsRestored, BudgetSnapshots,
                            BudgetSuggestions, CalendarData, MonthLensData,
                            ForecastMiss, ForecastScorePanel,
                            SnapshotsBackfilled, TodayFull, TodayGlance,
                            YearLensData)
from .security import limit


def _paced_from(pace: dict | None) -> str | None:
    """The pace start when it is later than the 1st, as an ISO date."""
    if not pace or pace["pace_start"] <= pace["month_start"]:
        return None
    return pace["pace_start"].isoformat()


@router.get("/today/full",
            dependencies=[Depends(limit("engine", 60, 60))])
@documented(response=TodayFull)
def today_full(user: dict = Depends(_user()), date: str = ""):
    """Everything the SPA Today page renders — the same gather()+context
    the Jinja page and email use, serialized. Single source preserved.

    `date=YYYY-MM-DD` (optional) renders a PAST day: same verdict/plan/
    bucket math as-of that day (gather's historical notion, not a fork),
    with the now-facts — cash forecast, runway, alerts — suppressed
    (live=False). Out-of-range dates clamp to [first transaction, today]."""
    from ..engine import budget
    from . import report, todayview
    if date:
        day = _iso_date(date, "date must be an ISO date (YYYY-MM-DD)")
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        real_today = _today(conn)
        if not date:
            day = real_today
        first = conn.execute("SELECT MIN(date) AS d FROM transactions "
                             "WHERE removed = 0").fetchone()["d"]
        # clamp: no earlier than the ledger, no later than the real today
        day = min(max(day, first or real_today), real_today)
        live = day == real_today
        st = report.gather(conn, day, live=live)
        # goal progress rides the Today payload (tile + pace) —
        # paced as of the viewed day. Computed in gather() so the email
        # renders the same numbers.
        goals = st["savings_goals"]
        ctx = todayview.build_context(st)
        # the recent pane renders the SAME table the Transactions page
        # does — full ledger-row shape, same ids the variable-spend
        # window picked
        recent = [_ser(r) for r in data.transactions_by_ids(
            conn, [r["txn_id"] for r in ctx["day"]["recent_var"]])]
        for o in recent:
            o["category_why"] = _category_why(o)
        _stamp_recurring(conn, recent)
        # "After paying cards and remaining bills" tile:
        # unpaid bill occurrences from now through month end plus the
        # envelope drip the walk would charge — computed in gather()
        # (report.bills_remaining_month) so the SPA tile and the email
        # can never disagree. A now-fact, so live-only (None on a
        # past-day view, like the runway itself).
        bills_remaining = st["bills_remaining_month"]
    finally:
        conn.close()
    day, fc = ctx["day"], ctx["fc"]
    b = st["buckets"]

    def bucket(k):
        v = b[k]
        out = {"actual": round(v["actual"], 2),
               "expected": round(v["expected"], 2),
               "month_budget": round(v["month_budget"], 2)}
        if "children" in v:     # food/other: custom-bucket carve-outs
            # `paced_budget`: the share of the budget the month may spend
            # (the whole of it unless the ledger began inside the month) —
            # what a money-map row measures its "$X left" against, the
            # tiles' own money. month_budget stays for the plan line.
            out["paced_budget"] = round(
                todayview.paced_budget(st, v["month_budget"]), 2)
            out["children"] = [{"name": c["name"],
                                "actual": round(c["actual"], 2),
                                "expected": round(c["expected"], 2),
                                "month_budget": round(c["month_budget"], 2),
                                "paced_budget": round(todayview.paced_budget(
                                    st, c["month_budget"]), 2)}
                               for c in v["children"]]
            out["display_actual"] = round(v["display_actual"], 2)
            out["display_expected"] = round(v["display_expected"], 2)
            out["remaining_budget"] = round(v["remaining_budget"], 2)
            out["paced_remaining_budget"] = round(
                todayview.paced_budget(st, v["remaining_budget"]), 2)
        return out

    scale = st.get("variable_scale")

    rows, total = todayview.today_map(st)
    return {
        "date": st["today"].isoformat(),
        # back-nav state: is this the live today, where "now" actually is,
        # and how far back the ledger goes (the ‹ button's floor)
        "is_today": live,
        "real_today": real_today.isoformat(),
        "min_date": first.isoformat() if first else None,
        # start of the "recent transactions" window — the header renders
        # "({{ st.yesterday|d }} – {{ st.today|d }} · …)"
        "yesterday": st["yesterday"].isoformat(),
        "day_of_month": st["today"].day,
        "days_in_month": st["days_in_month"],
        "verdict": st["verdict"],
        # which hero face this account shows (simple = the one number;
        # advanced = the full tile/goal anatomy) — the page's toggle
        # writes it back through /api/settings. Page-only: the daily
        # email's face is the separate email_schedule.daily.summary
        # setting, NOT this toggle.
        "today_view": st["today_view"],
        # the simple face's composed pieces (one number + chips), shared
        # with the email renders so the surfaces cannot drift
        "simple": ctx["day"]["simple"],
        "variance": round(st["variance"], 2),
        "tolerance": round(st["tolerance"], 2),
        "variable_actual": round(st["variable_actual"], 2),
        "variable_budget": round(b["food"]["month_budget"]
                                 + b["other"]["month_budget"], 2),
        # the hero's "$X of $Y left": the month's spendable share (the
        # whole month unless the ledger began inside it) and what is left
        # of it — composed once in month_status, read by the web, the app
        # and both email renders, equal to the tiles times the days left
        "month_share": round(st["month_share"], 6),
        "variable_budget_paced": round(st["variable_budget_paced"], 2),
        "month_remaining_paced": round(st["month_remaining_paced"], 2),
        # the day the month's pace runs from when the ledger began inside
        # the month (null otherwise): the progress line's "paced from"
        "paced_from": _paced_from(st.get("pace")),
        # "This month's plan" card (today_body.html) — all straight
        # from budget.month_status
        "income": (round(st["income"], 2)
                   if st.get("income") is not None else None),
        # this month's income in its two halves: landed, and still due
        # (absent halves = the budgeted figure, not the month's)
        "income_received": st.get("income_received"),
        "income_expected": st.get("income_expected"),
        # where that income comes from: the planner shows this figure and
        # lets it be typed only when it is the household's own ("typed")
        "income_source": st.get("income_source"),
        "plan_surplus": (round(st["plan_surplus"], 2)
                         if st.get("plan_surplus") is not None else None),
        # what the planner offers to save a month: an ordinary month's
        # surplus with the plan's own goal put back, the server's figure
        # (forecast.plan_savings_suggestion); null with no income
        "savings_suggested": st.get("savings_suggested"),
        "avg_bills": round(st.get("avg_bills") or 0, 2),
        # the plan line's "to spend": the configured budgets the excess
        # is computed from
        "plan_variable": round(st.get("plan_variable") or 0, 2),
        # this month's own bill schedule (what is due this month, not the
        # evened average) — the daily email's "bills due" figure
        "scheduled_month": round(st.get("scheduled_month") or 0, 2),
        "savings_plan": round(st.get("savings_plan") or 0, 2),
        # the goals savings_plan is made of, each with its plan — the
        # planners list them above the excess
        "plan_goals": st.get("plan_goals") or [],
        # the plan line in the whole dollars it prints, adding up to the
        # printed excess (budget.plan_line): both clients and the email
        # print these terms and round none of their own
        "plan_line": st.get("plan_line"),
        # the Budget page's summary line, for a plan with no income too
        # (budget.plan_legs): the client prints these legs and no others
        "plan_legs": st.get("plan_legs"),
        # the summary line's words after the excess figure and their
        # tone: a plan figure, and none of it left once the month's
        # spending is past its budget (todayview.excess_words)
        "plan_excess_words": todayview.plan_excess_words(st),
        # beside the plan line's bills (the evened-out average): that it
        # is the average, and this month's own bills when they sit well
        # away from it, or null
        "bills_weight": day["bills_weight"],
        # how many bills avg_bills evens out (the planner's "N tracked")
        "avg_bills_count": st.get("avg_bills_count") or 0,
        # the plan's rows (todayview._plan_rows: Food, carve-outs,
        # Everything else, Bills, Savings, Excess) and the variable bar
        # over them, built once: the web map, the phone map and the email
        # draw these
        "plan_rows": todayview.sent_plan_rows(rows),
        "plan_total": total,
        # sweep goals: the plan cap, month-to-date availability, and the
        # server-composed sentence (identical in the email HTML and plain)
        "sweep_plan": round(st.get("sweep_plan") or 0, 2),
        "sweep_available": (round(st["sweep_available"], 2)
                            if st.get("sweep_available") is not None
                            else None),
        "sweep_line": day.get("sweep_line"),
        "variable_scale": ({"factor": round(scale["factor"], 4),
                            "static_total": round(scale["static_total"], 2),
                            "available": round(scale["available"], 2)}
                           if scale else None),
        "irregular_bills": [dict(i, amount=round(i["amount"], 2))
                            for i in st.get("irregular_bills") or []],
        # "Scheduled but not yet posted" line: [payee, amount, due,
        # withdrawn_on] rows — withdrawn_on is the date the bank feed took
        # the charge back (null when it never posted)
        "overdue_unpaid": [[p, round(a, 2), due, gone]
                           for p, a, due, gone in budget.overdue_entries(st)],
        # month-to-date by category (Amazon subcategories clustered)
        "mtd": [[cat, round(amt, 2)] for cat, amt in day["mtd"]],
        "amazon_subs": [[s, round(a, 2)] for s, a in day["amazon_subs"]],
        "amazon_total": round(day["amazon_total"], 2),
        "allow": {k: {"rate": round(v["rate"], 2),
                      "plan": round(v["plan"], 2),
                      "spent_today": v["spent_today"],
                      "today_allowance": v["today_allowance"],
                      "left_today": v["left_today"],
                      "month_budget": v["month_budget"],
                      "month_spent": v["month_spent"],
                      # carved out to nothing: the clients leave its
                      # tile off
                      "empty": bool(v.get("empty")),
                      # nothing to hand out, nothing spent: one line
                      "zero_day": bool(v.get("zero_day")),
                      "daily_note": v["daily_note"],
                      "recover_days": v["recover_days"],
                      "recover_note": v["recover_note"],
                      "day_line": v["day_line"],
                      "rate_dollars": v["rate_dollars"],
                      "today_allowance_dollars": v["today_allowance_dollars"],
                      "left_dollars": v["left_dollars"]}
                  for k, v in day["allow"].items()},
        # custom buckets' today-ledger tiles, in config order
        "allow_kids": day["allow_kids"],
        # bills pinned to Today (bill setting) — server-composed cards,
        # identical wording in the email HTML and plain text
        "bill_cards": day["bill_cards"],
        "days_left": day["days_left"],
        # hero sentences + why chips: composed once in build_context so
        # this page, the email HTML and the plain text cannot drift
        "pace_line": day["pace_line"],
        # the words after the hero's month figure, and the label over the
        # left-to-spend number (todayview.hero_face)
        "month_words": day["month_words"],
        "left_label": day["left_label"],
        "why_chips": day["why_chips"],
        "headroom": day["headroom"],
        "buckets": {k: bucket(k) for k in ("fixed", "food", "other")},
        "fixed_unpaid_due": round(b["fixed"].get("unpaid_due") or 0, 2),
        "reasons": st["reasons"],
        # the Why narrative: server-composed once, rendered identically by
        # this page, the email HTML and the plain text
        "why": st.get("why"),
        "alerts": st.get("alerts") or [],
        "runway": ({"checking": st["runway"]["checking"],
                    "card_debt": round(st["runway"]["card_debt"], 2),
                    "due_total": round(st["runway"]["due_total"], 2),
                    "balance_unreliable":
                        bool(st["runway"].get("balance_unreliable")),
                    # no checking account to start cash from: headroom and
                    # forecast are null, and the client asks for the account
                    "no_checking": bool(st["runway"].get("no_checking"))}
                   if st.get("runway") else None),
        # unpaid bill occurrences through month end — the
        # "after paying cards and remaining bills" tile; null on past days
        "bills_remaining_month": bills_remaining,
        # ...split for printing (report.month_end_parts): the bills half
        # is the Bills row's still-due figure, the envelopes half the
        # Envelopes row's set-aside, and the line under the cash tile
        "month_end_parts": st.get("month_end_parts"),
        # the plan line's bills leg in words (budget.month_status)
        "plan_bills_label": st.get("plan_bills_label"),
        # the paychecks still due this month (dates; the sum is
        # income_expected) — the cash tile counts them next to the bills
        "income_due_dates": st.get("income_due_dates"),
        "income_due_total": st.get("income_due_total"),
        # why there is no paycheck figure ("none" | "budgeted" | "off";
        # null when there is one) — the tile's wording, decided server-side
        "income_due_note": st.get("income_due_note"),
        # the month-end tiles, "left at month end after cards & bills"
        # (and "…, bills & paychecks"); null on a past day or with no
        # checking balance
        "after_cards_bills": st.get("after_cards_bills"),
        "after_cards_bills_paychecks": st.get("after_cards_bills_paychecks"),
        # the cash notice, decided in todayview: {state critical|tight|
        # null, kind, amount, slow_to_per_day, until, ask}
        "cash_notice": ctx["cash_notice"],
        # the heavier-month warning, separate from the notice so an older
        # phone that words the notice itself stays quiet (todayview.cash_band)
        "cash_band": ctx["cash_band"],
        # the simple face's day bar (todayview.simple_day_meter)
        "day_meter": {"spent": round(day["day_spent"], 2),
                      "allow": round(day["day_allow"], 2)},
        # the recent window's variable spending, the email's figure
        "recent_variable_spending": day["recent_var_total"],
        "next_paycheck": st.get("next_paycheck"),
        "recent": recent,
        # two user-facing card scenarios — pay all cards now and
        # autopay statement balance. The engine still computes the full-
        # balance-at-due-dates series internally; the product doesn't show it.
        "forecast": ({"days": fc["days"],
                      "checking": fc["checking"],
                      "card_debt": fc["card_debt"],
                      # the two walks an older phone draws: pay all cards
                      # now, and the usual walk (the headline)
                      "pace_now": _series(fc["pace_now"]),
                      "pace_stmt": _series(fc["pace_stmt"]),
                      # the three card scenarios, each with its walk, low
                      # point in words, chart lines and timeline rows
                      # (todayview.CASH_SCENARIOS): "usual" first
                      "scenarios": ctx["scenarios"],
                      # when card spending reaches checking, in words
                      "card_timing_note": ctx["card_timing_note"],
                      # own-account transfers the forecast leaves out, in
                      # words (todayview.transfers_note)
                      "transfers_note": ctx["transfers_note"],
                      # a paycheck left out because its money stopped
                      # (todayview.stopped_paychecks_note)
                      "stopped_paychecks_note": ctx["stopped_paychecks_note"],
                      # how close the forecast has landed, in words
                      # (todayview.score_sentences)
                      "score": ctx["forecast_score"],
                      # the heading of each scenario row's after_cards
                      "after_cards_label": todayview.AFTER_CARDS_LABEL,
                      # the dated card autopays an older client draws as
                      # lines: [date, amount, card name], amount negative =
                      # leaving — only a card really paid by autopay
                      "card_autopay": todayview.autopay_marks(
                          fc, fc["pace_stmt"]),
                      # the chart's mark and the one-offs the pace leaves
                      # out, in the words every surface prints
                      "runs_out_label": ctx["runs_out_label"],
                      "one_offs": fc.get("one_offs") or [],
                      "pace_note": ctx["pace_note"],
                      # the forecast's own daily pace and its window
                      # (forecast.forecast_pace); the page prints it in
                      # pace_note's words
                      "pace_basis": fc.get("pace_basis")}
                     if fc else None),
        # the month's last day rides as a row of its own (kind
        # month_end, amount 0): the balance the month-end tile prints
        "forecast_rows": [{"date": d0, "amount": a if a is not None else 0.0,
                           "label": p2, "bal_now": bn, "bal_stmt": bs,
                           "kind": "event" if a is not None else "month_end",
                           # the card a bill is charged to (it moves no
                           # checking balance that day), else null
                           "card": card,
                           # the amount is the forecast's estimate (a
                           # seasonal bill, a paycheck's yearly pattern)
                           "estimated": p2.split(" — on ")[0] in (
                               (fc or {}).get("estimated") or [])}
                          for d0, a, p2, bn, bs, card in ctx["fc_rows"]],
        # "left at month end": the balance the cash timeline ends the
        # month on (todayview.month_end_left) and the note under it;
        # null with no forecast
        "month_end_left": (ctx["month_end"]["balance"]
                           if ctx["month_end"] else None),
        # the tile's label and the cards' line under it
        # (todayview.month_end_cards_note)
        "month_end_label": ctx["month_end_label"],
        "month_end_cards_note": ctx["month_end_cards_note"],
        "month_end_note": ctx["month_end_note"],
        "savings_goals": goals,
    }


# ---- timeframe lenses ----------------------------------------
# Week/Month/Year summaries composed from the frozen engine by web/lenses.py
# — the same functions the monthly/yearly emails render, so API and email
# cannot diverge.

@router.get("/today/forecast-score",
            dependencies=[Depends(limit("engine", 60, 60))])
@documented(response=ForecastScorePanel)
def forecast_score_api(user: dict = Depends(_user())):
    """"How has the forecast done?": the last 90 days of 30-day
    predictions against the real balance, the band's coverage, how the
    household's card payments compared with the scenarios, and why days
    were not graded (specs/forecast-grading.md). Every word is the
    server's."""
    from ..engine import forecast_score
    conn = _conn(user)
    try:
        return forecast_score.panel_kept(conn, _today(conn))
    finally:
        conn.close()


@router.get("/today/forecast-score/miss",
            dependencies=[Depends(limit("engine", 60, 60))])
@documented(response=ForecastMiss)
def forecast_miss_api(user: dict = Depends(_user()),
                      day: str = Query(..., max_length=10),
                      horizon: int = Query(30, ge=1, le=90)):
    """"Why the forecast missed" for the day a forecast pointed at: the
    miss split into parts that add up to it, and its five largest causes.
    404 when that forecast is past its 120-night keep or was not graded."""
    from ..engine import forecast_score
    d = _iso_date(day, "day must be an ISO date (YYYY-MM-DD)")
    # Grading keeps only these horizons; any other would come back as a
    # 404 that blames the forecast's age, which is not why.
    if horizon not in forecast_score.HORIZONS:
        raise HTTPException(422, "horizon must be one of "
                            + ", ".join(map(str, forecast_score.HORIZONS))
                            + " days.")
    conn = _conn(user)
    try:
        out = forecast_score.miss_kept(conn, d, horizon, _today(conn))
    finally:
        conn.close()
    if out is None:
        raise HTTPException(404, "That forecast is too old to explain, or "
                                 "it was not graded.")
    return out


@router.get("/today/glance",
            dependencies=[Depends(limit("glance", 120, 60))])
@documented(response=TodayGlance)
def today_glance(request: Request, user: dict = Depends(_user())):
    """The home-screen widget's payload: the Today hero's simple face, and
    nothing past it. One number (what is still spendable today across
    every variable bucket), the verdict word, the day bar, the bucket
    chips and the pace phrase — all lifted from the SAME context the page
    and the daily email render, so the widget can never show a number the
    app would not. No balances, no transactions, no account names: this
    is what a lock screen may show.

    The one door a widget token opens; a device token or a browser
    session reads it too (the app refreshes its own widget from here).
    Whole dollars, like every aggregate on the hero. Weak ETag so a
    widget polling on the OS's clock costs nothing when nothing moved."""
    import hashlib
    import json
    from fastapi.responses import Response
    from ..engine import budget
    from . import todayview
    conn = tenancy.tenant_connect(user["tenant_id"])
    try:
        today = _today(conn)
        budgets_set = _budgets_set(conn)
        if budgets_set:
            # the month status and the hero built from it, and nothing
            # else: the full Today read also walks the 60-day forecast,
            # the runway, goal progress and the alert log, none of which
            # the widget shows (todayview.hero_face is the page's own)
            st = budget.month_status(conn, today)
            st["today"] = today
            day = todayview.hero_face(st)
        else:
            st = day = None
    finally:
        conn.close()
    if st is None:
        # the app shows no hero before a budget exists (a verdict on a
        # plan the household has not made is arithmetic, not news); the
        # widget says so instead of a $0 that would read as "nothing left"
        body = {"date": today.isoformat(), "budgets_set": False}
    else:
        body = {
            "date": st["today"].isoformat(),
            "budgets_set": True,
            "verdict": st["verdict"],
            "left_today": round(day["simple"]["left_today"]),
            "day_spent": round(day["day_spent"]),
            "day_allow": round(day["day_allow"]),
            "days_left": day["days_left"],
            "pace_line": day["pace_line"] or "",
            "chips": day["simple"]["chips"],
        }
    body["as_of"] = dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="seconds")
    payload = json.dumps({k: v for k, v in body.items() if k != "as_of"},
                         sort_keys=True)
    etag = 'W/"' + hashlib.sha256(payload.encode()).hexdigest()[:32] + '"'
    headers = {"ETag": etag, "Cache-Control": "private, no-cache"}
    if request.headers.get("if-none-match") == etag:
        # the 304 restates the cache policy: a bare one would pick up the
        # API-wide no-store and tell the client to drop the copy it holds
        return Response(status_code=304, headers=headers)
    return Response(json.dumps(body), media_type="application/json",
                    headers=headers)


@router.get("/lens/month",
            dependencies=[Depends(limit("engine", 60, 60))])
@documented(response=MonthLensData)
def lens_month(user: dict = Depends(_user()), y: int = 0, m: int = 0):
    """Month report card — final for past months, projected for the
    current one (default: this month)."""
    from . import lenses
    conn = _conn(user)
    try:
        # the household's day decides BOTH the default month and whether
        # the month is still open — on the server clock the summary calls
        # a live month final for the hours after UTC midnight
        today = _today(conn)
        y, m = y or today.year, m or today.month
        _ck_ym(y, m)
        return lenses.month_summary(conn, y, m, today=today)
    finally:
        conn.close()


@router.get("/lens/year",
            dependencies=[Depends(limit("engine", 60, 60))])
@documented(response=YearLensData)
def lens_year(user: dict = Depends(_user()), y: int = 0):
    """12-cell verdict grid + annual totals + category YoY (default: this
    year)."""
    from . import lenses
    conn = _conn(user)
    try:
        today = _today(conn)
        y = y or today.year
        _ck_year(y)
        return lenses.year_summary(conn, y, today=today)
    finally:
        conn.close()


@router.get("/budget/snapshots")
@documented(response=BudgetSnapshots)
def budget_snapshots(user: dict = Depends(_user())):
    """Budget history for the Budget page's card: the frozen months
    (newest first, each month's variable budgets + how it was captured)
    and how many closed months since the ledger began have no snapshot —
    the months the backfill button would freeze."""
    from ..engine import budget
    conn = _conn(user)
    try:
        rows = conn.execute(
            "SELECT year, month, config, bills, captured_at, source "
            "FROM budget_snapshots ORDER BY year DESC, month DESC").fetchall()
        first = conn.execute("SELECT MIN(date) AS d FROM transactions "
                             "WHERE removed = 0").fetchone()
        cfg = budget.load_config(conn)
        # a saved budget, never the loader's unsaved first-month guess —
        # the backfill refuses to freeze that guess, so it is not offered
        has_budget = budget.budgets_set(conn)
    finally:
        conn.close()
    from .. import localtime
    today = localtime.now_local(cfg).date()
    have = {(r["year"], r["month"]) for r in rows}
    missing = 0
    if first and first["d"]:
        # the months the backfill would freeze — no further back than it
        # reaches (budget.backfill_start)
        y, m = budget.backfill_start(first["d"], today)
        while (y, m) < (today.year, today.month):
            if (y, m) not in have:
                missing += 1
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    months = []
    for r in rows:
        c = dict(r["config"])
        # a malformed row (pre-validation restore) lists as absent
        # rather than 500ing the whole history card
        if not budget._snapshot_budgets_numeric(c):
            continue
        food = float(c.get("food_monthly") or 0)
        other = float(c.get("other_monthly") or 0)
        entry = {"y": r["year"], "m": r["month"],
                 "food_monthly": round(food, 2),
                 "other_monthly": round(other, 2),
                 "variable_budget": round(food + other, 2),
                 "source": r["source"],
                 "captured_at": r["captured_at"].isoformat(),
                 # the month still running is re-frozen nightly until it
                 # closes: it is "so far", not frozen
                 "status_text": ("so far" if (r["year"], r["month"])
                                 == (today.year, today.month) else None)}
        # the schedule frozen with the budget — absent on snapshots that
        # predate bill freezing, and the card says "—" rather than back-
        # deriving a number the month never ran under
        sched = r["bills"] if isinstance(r["bills"], dict) else {}
        for k in ("bills_monthly", "income_monthly"):
            v = sched.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                entry[k] = round(float(v), 2)
        months.append(entry)
    return {"months": months, "missing": missing,
            "budget_set": bool(has_budget
                               and ((cfg.get("food_monthly") or 0) > 0
                                    or (cfg.get("other_monthly") or 0) > 0))}


@router.post("/budget/snapshots/backfill")
@documented(response=SnapshotsBackfilled)
def budget_snapshots_backfill(user: dict = Depends(_user())):
    """Freeze the CURRENT budget for every closed no-snapshot month since
    the ledger began — the owner's explicit opt-in to the retroactive
    judgment the lenses otherwise refuse to fabricate. Marked
    source='backfill'; existing snapshots and the current month are never
    touched. (Viewers are stopped by the session write gate.)"""
    demoguard.deny(user)
    from ..engine import budget
    conn = _conn(user)
    try:
        n = budget.backfill_snapshots(conn, _today(conn))
    finally:
        conn.close()
    return {"backfilled": n}


@router.get("/budgets/suggest")
@documented(response=BudgetSuggestions)
def budgets_suggest(user: dict = Depends(_user()),
                    income: str = Query("", max_length=40)):
    """'Suggest from history': trailing 6-full-month median per
    bucket (current month excluded, months >2× the median excluded), for
    food/other AND every custom bucket. Read-only — the user approves each
    number in the Settings UI before anything is saved.

    `income`: the monthly take-home the planner's box holds, as typed
    (either separator convention). With no income in history it is what
    `suggestions.savings_monthly` is measured on; with income in history it
    is not read. Blank, unreadable, non-finite or not above $0 is no typed
    income: no suggestion, never an error."""
    from ..engine import budget
    import math
    from .fields import MONEY_MAX, typed_money
    try:
        typed = typed_money(income) if income.strip() else None
    except (TypeError, ValueError):
        typed = None
    if typed is not None and (not math.isfinite(typed)
                              or not 0 < typed <= MONEY_MAX):
        typed = None
    conn = _conn(user)
    try:
        return budget.suggest_budgets(conn, _today(conn),
                                      typed_income=typed)
    finally:
        conn.close()


# An address anywhere inside free text. Bounded classes and no nested
# quantifiers, so a long run of dots cannot make it backtrack.
_ADDRESS_IN_TEXT = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


@router.get("/activity", dependencies=[Depends(limit("activity", 120, 60))])
@documented(response=ActivityPage)
def activity_api(user: dict = Depends(_user()),
                 limit_: int = Query(50, alias="limit", ge=1, le=200),
                 before: str = "", kind: str = "", who: str = "",
                 target: str = ""):
    """The household's activity log, newest first: who changed which
    category, bill, note, rule… and when. Everyone in the household reads
    it (everyone sees everything); `before` pages by the last row's
    timestamp, `kind` / `who` / `target` filter."""
    if kind and kind not in _activity.KINDS:
        raise HTTPException(400, "unknown kind")
    hidden = demoguard.people_hidden(user)
    if hidden and who and not demoguard.own_addresses_only(user, [who]):
        # filtering by another member's address would confirm who acts here,
        # and the anonymised name matches no stored row
        return {"rows": [], "more": False, "next": None,
                "actors": [], "kinds": list(_activity.KINDS)}
    conn = _conn(user)
    try:
        out = _activity.list_activity(
            conn, limit=limit_, before=before or None, kind=kind or None,
            actor=who or None, target=target or None)
    finally:
        conn.close()
    if hidden:
        # the actor column is the acting member's address; the label (and
        # the sentence built from it) can be one too — the continuity
        # packet's row names who it was mailed to
        def _mask(value):
            if isinstance(value, str):
                return _ADDRESS_IN_TEXT.sub(
                    lambda m: demoguard.own_address_only(user, m.group(0)),
                    value)
            if isinstance(value, dict):
                return {k: _mask(v) for k, v in value.items()}
            if isinstance(value, list):
                return [_mask(v) for v in value]
            return value
        for r in out["rows"]:
            r["actor"] = demoguard.own_address_only(user, r.get("actor"))
            # another member's id is a handle on that member; the shared
            # login is told only its own
            if r.get("actor_user_id") not in (None, str(user.get("user_id"))):
                r["actor_user_id"] = None
            label = r.get("label")
            masked = _mask(label)
            if masked != label:
                r["label"] = masked
                r["summary"] = _activity.sentence(
                    r.get("kind") or "", r.get("action") or "", masked,
                    r.get("detail"))
            # a note's text or a merchant name can quote an address as well,
            # and the detail (before/after values) is returned beside it
            r["summary"] = _mask(r.get("summary"))
            r["detail"] = _mask(r.get("detail"))
        # the filter offers only the caller: a chip for "a household
        # member" could only ever return nothing
        out["actors"] = list(dict.fromkeys(
            demoguard.own_addresses_only(user, out["actors"])))
    return out


def _alert_key(body: Fields) -> tuple[str, str]:
    """The kind + message that name an alert row. A lookup key, matched as
    stored, so unlike most text fields it is never trimmed: a message that
    happens to end in a space could then not be dismissed at all."""
    return (body.text("kind", strip=False),
            body.text("message", strip=False))


@router.post("/alerts/dismiss")
@documented(response=AlertsDismissed, body=AlertKey)
def alerts_dismiss_api(user: dict = Depends(_user()),
                       body: Fields = Depends(json_fields)):
    from ..engine import alerts as _alerts
    kind, message = _alert_key(body)
    conn = _conn(user)
    try:
        n = _alerts.dismiss(conn, kind, message)
        return {"ok": True, "dismissed": n}
    finally:
        conn.close()


@router.post("/alerts/restore")
@documented(response=AlertsRestored, body=AlertKey)
def alerts_restore_api(user: dict = Depends(_user()),
                       body: Fields = Depends(json_fields)):
    from ..engine import alerts as _alerts
    kind, message = _alert_key(body)
    conn = _conn(user)
    try:
        n = _alerts.restore(conn, kind, message)
        return {"ok": True, "restored": n}
    finally:
        conn.close()


@router.get("/alerts/history")
@documented(response=AlertHistory)
def alerts_history_api(user: dict = Depends(_user())):
    """The persistent alert log (active first, then newest) — the SPA
    Alerts page. One line per subject (alerts.history_lines): its latest
    wording, the first and last day it was seen, where it is acted on, its
    name and its chip."""
    from ..engine import alerts as _alerts
    conn = _conn(user)
    try:
        return {"alerts": [_ser(r) for r in _alerts.history_lines(conn)]}
    finally:
        conn.close()


_SERIES_KEYS = ("series", "min", "min_date", "negative_date",
                "first_neg_amount", "end", "rate", "sweep_events")


def _series(walk: dict) -> dict:
    """A forecast walk as the reply's Series: the walk's own working keys
    (its card rows, outflows, charged bills) stay on the server."""
    return {k: walk[k] for k in _SERIES_KEYS}


@router.get("/calendar",
            dependencies=[Depends(limit("engine", 60, 60))])
@documented(response=CalendarData)
def calendar_api(user: dict = Depends(_user()), days: int = 35):
    """Dated money events for the next N days — bills out, paychecks in,
    card payments as the Today page's default view walks them (the usual
    walk's card rows) — grouped per day for a calendar strip.
    Same engine pass the cash forecast uses; nothing recomputed."""
    from ..engine import budget, forecast
    days = max(7, min(90, days))
    conn = _conn(user)
    try:
        # the household's day, not the instance clock: on the hosted (UTC)
        # box an evening request would otherwise label a bill due today
        # against tomorrow's date on this one screen
        today = _today(conn)
        # the forecast the Today page built (forecast.build keeps it for
        # the day): its horizon covers the calendar's, which reads its
        # own days of it
        fc = forecast.build(conn, today=today,
                            days=max(days, forecast.HORIZON_DAYS))
        # The cash walk starts tomorrow, so it dates what is owed today —
        # an unpaid bill due today (or already late), a paycheck due today,
        # a card payment the issuer dates today — tomorrow. A calendar is
        # not a walk: each belongs on the day it is owed, today's cell. A
        # card with no due date stays on tomorrow: that day is the walk's
        # assumption, not a date anyone gave.
        tomorrow = today + dt.timedelta(days=1)
        cfg = budget.load_config(conn)
        end = today + dt.timedelta(days=days)
        owed_now: list[tuple[str, float]] = [
            (o["payee"], round(-o["planned"], 2))
            for o in budget.upcoming_bill_occurrences(conn, cfg, today, end)
            if o["due"] == tomorrow and o["occ"]["due"] <= today]
        # a paycheck's walked amount is the forecast's own estimate, so
        # its payee alone names it
        paid_now = [o["payee"] for o in budget.upcoming_bill_occurrences(
                        conn, cfg, today, end, income=True)
                    if o["due"] == tomorrow and o["occ"]["due"] == today]
        # the cards whose issuer says a payment is due today
        cards_now = {r["name"] for r in conn.execute(
            """SELECT COALESCE(a.display_name, a.name) AS name
                 FROM accounts a JOIN liabilities l ON l.account_id = a.id
                WHERE a.type = 'credit' AND a.balance_current IS NOT NULL
                  AND l.raw ->> 'next_payment_due_date' = %s""",
            (today.isoformat(),)).fetchall()}
    finally:
        conn.close()
    by_day: dict[str, list[dict]] = {}
    last_day = end.isoformat()
    usual = fc["pace_usual"]
    # Checking money is counted once (specs/today-figures.md, "The
    # Calendar counts checking money once"): checking's own events (the
    # usual walk's checking_events: every dated event but the bills a
    # card pays), and the card payments as the Today page's default view
    # walks them (the usual walk's card rows: same dates and amounts). A
    # bill a card pays is listed on the day every timeline view lists it,
    # as on its card, and is left out of the day's in and out — the card
    # payment that pays it is the money leaving checking.
    for d, amount, label, card in usual.get("charged_bills", []):
        if d > last_day:
            continue
        # owed today or late: today's cell, as every owed bill below
        key = (label, round(amount, 2))
        if d == tomorrow.isoformat() and key in owed_now:
            owed_now.remove(key)
            d = today.isoformat()
        # the label an older phone prints says it is on the card too
        note = f"on {card}"
        by_day.setdefault(d, []).append(
            {"label": f"{label} — {note}", "name": label, "note": note,
             "amount": amount, "kind": "on_card"})
    for kind, evs in (("bill", usual.get("checking_events", [])),
                      ("card", usual.get("card_rows", []))):
        for d, amount, label in evs:
            if d > last_day:
                continue
            day = d
            key = (label, round(amount, 2))
            if d == tomorrow.isoformat():
                if kind == "bill" and amount > 0 and label in paid_now:
                    paid_now.remove(label)
                    day = today.isoformat()
                elif kind == "bill" and key in owed_now:
                    owed_now.remove(key)
                    day = today.isoformat()
                elif (kind == "card" and label.endswith(" — statement due")
                      and label.split(" — ", 1)[0] in cards_now):
                    # the walk pays a statement due today tomorrow at the
                    # earliest; the calendar shows that statement's own
                    # row on the day it is due (a round row stays put)
                    day = today.isoformat()
            # `name` and `note` are the two halves the clients place apart
            # — a narrow cell cut the joined label mid-word: a card row's
            # card, and how it is paid ("Harbor card", "paid after payday")
            name, note = ((label.split(" — ", 1) + [None])[:2]
                          if kind == "card" else (label, None))
            by_day.setdefault(day, []).append(
                {"label": label,
                 "name": name, "note": note, "amount": amount,
                 "kind": "income" if amount > 0 else kind})
    # a day's in and out as well as its net: the phone lists the days in
    # words at very large text ("Tue 12: +$1,250 in, $86 out") and prints
    # these, adding nothing up itself (specs/accessibility.md, requirement 12)
    def ck(evs):
        # what moves checking that day: not a bill on a card
        return [e["amount"] for e in evs if e["kind"] != "on_card"]
    return {"days": days,
            "start": today.isoformat(),
            "calendar": [{"date": d,
                          "total": round(sum(ck(evs)), 2),
                          "came_in": round(sum(a for a in ck(evs)
                                               if a > 0), 2),
                          "went_out": round(-sum(a for a in ck(evs)
                                                 if a < 0), 2),
                          "events": sorted(evs, key=lambda e: e["amount"])}
                         for d, evs in sorted(by_day.items())]}
