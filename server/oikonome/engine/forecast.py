"""60-day cash-position forecast: when does headroom bottom out?

The primary checking account is per-tenant config `checking_account_id`,
falling back to the largest-balance depository checking account — never a
hardcoded institution.

Projects HEADROOM (checking − all card debt) forward day by day:
  − every bill occurrence on its due date
  − envelope bills spread evenly (what's LEFT of each pool ÷ days left
    in its period — the walk starts from live checking, which this
    period's envelope spend has already left)
  − variable spending at the PLAN pace and the CURRENT month pace
  − monthly-mode savings plans as scheduled outflows; sweep-mode plans
    only at each month end and only up to that month's own realized
    surplus (capped so the trough never moves — a sweep goal can never
    make the forecast read worse than having no goal)
  + paycheck occurrences (median of last 3 real deposits, capped by the
    active income-scenario biweekly)

Card-payoff scenarios:
  plan/pace   — full balance paid on each card's statement due date
                (conservative; no/stale due date ⇒ tomorrow)
  pace_now    — pay every card today (comparison line)
  pace_nocards— never pay (feeds the excluding-cards notice)
  pace_stmt   — autopay reality: last statement balance on the due date,
                remainder rolling to the next cycle ~1 month later

The PRODUCT (SPA + email) shows two card scenarios — pace_now
("pay all cards now") and pace_stmt ("autopay statement balance", the
PRIMARY/realistic case). plan/pace stay computed here (internal
consumers + tests) but are not user-facing.
"""

import calendar
import datetime as dt
import statistics

from .. import localtime
from . import bills, budget
from .compat import as_date

HORIZON_DAYS = 60


def _checking_balance(conn, cfg: dict) -> float:
    """Per-tenant primary checking: explicit config wins, else the
    largest-balance depository checking account."""
    acct_id = cfg.get("checking_account_id")
    # a pinned account that has since become a linked group's shadow (its
    # source went stale or errored and the healthy twin took over) would
    # feed a frozen balance into every runway number — honour the live
    # failover the rest of the money math already follows
    from . import links
    if acct_id and acct_id in set(links.shadow_ids(conn)):
        acct_id = None
    if acct_id:
        # a pinned account that was since assigned to a business entity
        # is business money — the personal runway must not start from it
        # unless the household combines entities (the auto-pick below
        # applies the same rule)
        row = conn.execute(
            """SELECT COALESCE(balance_available, balance_current) AS bal
               FROM accounts WHERE id = %s
                 AND (NULLIF((SELECT current_setting('app.combine_entities', true)),'')='true'
                      OR entity_id IS NULL)""", (acct_id,)).fetchone()
        if row and row["bal"] is not None:
            return row["bal"]
    row = conn.execute(
        """SELECT COALESCE(balance_available, balance_current) AS bal
           FROM accounts WHERE type = 'depository' AND subtype = 'checking'
             AND (NULLIF((SELECT current_setting('app.combine_entities', true)),'')='true' OR entity_id IS NULL)  -- don't auto-pick a business account
             -- never auto-pick a shadow (non-primary linked) source
             AND (NULLIF((SELECT current_setting('app.shadow_ids', true)), '') IS NULL
                  OR NOT (id = ANY(string_to_array(
                          (SELECT current_setting('app.shadow_ids', true)), ','))))
           ORDER BY COALESCE(balance_current, 0) DESC LIMIT 1""").fetchone()
    return row["bal"] if row and row["bal"] is not None else 0.0


def next_paycheck(conn, today: dt.date | None = None, *,
                  memo: dict | None = None) -> dt.date | None:
    """Earliest projected paycheck (income-series occurrence) strictly after
    `today`. None if no income series is set. Looks ~6 weeks out. `memo`:
    the request-scoped cache — the config, the income schedule and the
    inflow rows are the same ones `build` reads a moment later."""
    cfg = budget.load_config(conn, memo=memo)
    # "strictly after today" is a promise about the household's calendar:
    # on the container's UTC day a west-coast evening already sits on
    # tomorrow, and the paycheck landing tomorrow stops being the next one
    today = today or localtime.now_local(cfg).date()
    end = today + dt.timedelta(days=45)
    # same deposit-matched rule as the forecast walk: a paycheck that has
    # already posted is not the NEXT one
    up = budget.upcoming_bill_occurrences(
        conn, cfg, today, end, income=True, memo=memo)
    return up[0]["due"] if up else None


def _paycheck_amount(conn, payee: str, row_amount: float, today: dt.date,
                     cap: float | None = None) -> float:
    """Median of the last 3 real deposits for this income series (falls back
    to the configured amount). Wide 0.5–2× band.

    `cap` = the active income-scenario biweekly take-home: when the
    retirement-saving toggle flips, real deposits lag the new reality by 2-3
    paychecks, so the estimate is capped at min(median, scenario)."""
    key = budget._key_token(payee or "")
    if not key:
        return row_amount if cap is None else min(row_amount, cap)
    events = bills._events(
        [r for r in bills._income_rows(conn, today) if key in r["tokens"]])
    core = [e["amount"] for e in events
            if 0.5 * row_amount <= e["amount"] <= 2 * row_amount]
    est = statistics.median(core[-3:]) if len(core) >= 2 else row_amount
    return est if cap is None else min(est, cap)


def build(conn, today: dt.date | None = None, days: int = HORIZON_DAYS,
          *, memo: dict | None = None) -> dict:
    # `memo`: the optional request-scoped cache described on
    # savings.posted_for_plan — the caller that also renders Today passes
    # the same dict here so one snapshot's sweep evidence is derived once.
    cfg = budget.load_config(conn, memo=memo)
    # day zero of the walk is the household's day, not the container's
    today = today or localtime.now_local(cfg).date()
    end = today + dt.timedelta(days=days)

    checking_bal = _checking_balance(conn, cfg)
    card_debt = conn.execute(
        """SELECT COALESCE(SUM(GREATEST(a.balance_current, 0)), 0) AS d
           FROM accounts a WHERE a.type = 'credit'
             AND (NULLIF((SELECT current_setting('app.combine_entities', true)),'')='true' OR a.entity_id IS NULL)  -- business cards out of personal runway (unless combined)
             AND (NULLIF((SELECT current_setting('app.shadow_ids', true)), '') IS NULL
                  OR NOT (a.id = ANY(string_to_array(
                          (SELECT current_setting('app.shadow_ids', true)), ','))))
        """).fetchone()["d"]
    start = checking_bal - card_debt

    # per-card payoff events, TWO scenarios:
    #   full — full current balance on the liabilities due date (conservative
    #          primary); no/stale due date ⇒ tomorrow, never optimistic
    #   stmt — autopay reality: the LAST STATEMENT balance on the due date,
    #          remainder rolling to the next cycle; missing data ⇒ full
    card_events: list[tuple[dt.date, float, str]] = []
    card_events_stmt: list[tuple[dt.date, float, str]] = []
    # the subset the clients draw as dated lines on the cash chart: the
    # statement-scenario autopays whose due date the issuer actually gave us
    card_autopay: list[tuple[dt.date, float, str]] = []
    for c in conn.execute(
            """SELECT COALESCE(a.display_name, a.name) AS name,
                      a.balance_current AS bal,
                      l.raw ->> 'next_payment_due_date' AS due,
                      -- cast only what is a number: a restored ZIP can
                      -- carry "N/A" here, and a bare ::double precision
                      -- on it aborts this query, and with it every Today
                      -- load and nightly email for the household
                      CASE WHEN l.raw ->> 'last_statement_balance'
                                ~ '^-?[0-9]+(\\.[0-9]+)?$'
                           THEN (l.raw ->> 'last_statement_balance')::double precision
                      END AS stmt
               FROM accounts a LEFT JOIN liabilities l ON l.account_id = a.id
               WHERE a.type = 'credit' AND COALESCE(a.balance_current, 0) > 0.5
                 AND (NULLIF((SELECT current_setting('app.combine_entities', true)),'')='true' OR a.entity_id IS NULL)
                 -- Same shadow filter as the card_debt sum above, so a
                 -- multi-source card (Plaid + SimpleFIN) schedules ONE payment
                 -- event for its one real balance, not one per linked account
                 AND (NULLIF((SELECT current_setting('app.shadow_ids', true)), '') IS NULL
                      OR NOT (a.id = ANY(string_to_array(
                              (SELECT current_setting('app.shadow_ids', true)), ','))))
            """).fetchall():
        try:
            due = as_date(c["due"])
        except ValueError:
            # an unparsable or out-of-range due date is the "never told
            # us" case below, not a reason to 500 the whole forecast
            due = None
        # A card whose issuer never told us a due date still has to be paid,
        # so the walk schedules it tomorrow rather than pretending the money
        # is safe. That date is an assumption, not a calendar fact, which is
        # why only the real ones become dated markers below: drawing a line
        # labelled "autopay" on a day nothing is known to happen would be a
        # guess wearing a fact's clothes.
        dated = due is not None and due > today
        if not dated:
            due = today + dt.timedelta(days=1)
        if due <= end:
            card_events.append((due, -c["bal"], f'{c["name"]} payment'))
        stmt = c["stmt"]
        stmt = min(float(stmt), c["bal"]) if stmt is not None else c["bal"]
        stmt = max(stmt, 0.0)
        if due <= end and stmt > 0.5:
            card_events_stmt.append((due, -stmt, f'{c["name"]} autopay'))
            if dated:
                card_autopay.append((due, -stmt, c["name"]))
        rest = c["bal"] - stmt
        if rest > 0.5:
            ny, nm = (due.year, due.month + 1) if due.month < 12 else (due.year + 1, 1)
            next_due = dt.date(ny, nm, min(due.day, calendar.monthrange(ny, nm)[1]))
            if next_due <= end:
                card_events_stmt.append(
                    (next_due, -rest, f'{c["name"]} autopay (next statement)'))
                if dated:
                    card_autopay.append((next_due, -rest, c["name"]))

    # --- dated events: bills out, paychecks in -----------------------------
    # Paid/overdue/floored rules live in budget.upcoming_bill_occurrences —
    # the runway and Today headroom read the same helper.
    events: list[tuple[dt.date, float, str]] = [
        (b["due"], -b["planned"], b["payee"])
        for b in budget.upcoming_bill_occurrences(conn, cfg, today, end,
                                                  memo=memo)]

    # planned savings-goal contributions are scheduled outflows
    # (1st of each month) — fixed-bill semantics, never verdict spend.
    # Don't ALSO track the actual transfer as a recurring bill, or the
    # forecast counts the same money twice. Sweep-mode goals are NOT
    # fixed outflows and are handled after month_status below — their
    # contribution exists only if the month's own surplus does.
    from . import savings as _savings
    first = today.replace(day=1)
    for g in _savings.goals(cfg):
        plan = float(g.get("monthly_plan") or 0)
        if plan <= 0 or _savings.goal_mode(g) == "sweep":
            continue
        d = first
        while d <= end:
            if d == first:
                # `today < d` never holds for the CURRENT month's 1st
                # (today is on/after the 1st by definition), so a
                # future-only test would never schedule this month's
                # transfer and the forecast would run optimistic by the
                # whole plan until it posted.
                # Bill-like instead, mirroring unpaid bills: whatever the
                # ledger hasn't seen posted to the goal this month is
                # still owed and lands at max(1st, tomorrow). Posted
                # money is already out of the live starting balance —
                # scheduling it again would double-count. A plan-only
                # goal (no account, no tokens) has no ledger to
                # check; the conservative read is still-owed.
                # posted_for_plan also accepts the CHECKING
                # leg as evidence — a transfer whose destination account
                # carries no rows yet has already left the live balance, so
                # scheduling the plan again would walk the money out twice.
                posted = (0.0 if _savings.is_plan_only(g) else max(
                    0.0, _savings.posted_for_plan(conn, cfg, g,
                                                  since=first, memo=memo)))
                owed = round(plan - posted, 2)
                due = max(d, today + dt.timedelta(days=1))
                if owed > 0.005 and due <= end:
                    events.append((due, -owed, f"{g['name']} (savings plan)"))
            elif today < d <= end:
                events.append((d, -plan, f"{g['name']} (savings plan)"))
            d = (dt.date(d.year + 1, 1, 1) if d.month == 12
                 else dt.date(d.year, d.month + 1, 1))

    # active income-scenario take-home caps the per-deposit estimate so a
    # lower configured paycheck applies immediately
    _scen = budget.active_income_scenario(cfg)
    pay_cap = (_scen.get("take_home") if _scen else None) or None
    # Paychecks go through the same occurrence rule as bills, deposit side:
    # one that already posted is in the live starting balance, so walking
    # it in again on payday would count the same money twice for the days
    # between the deposit and the due date.
    amounts: dict[str, float] = {}
    for p in budget.upcoming_bill_occurrences(conn, cfg, today, end,
                                              income=True, memo=memo):
        payee = p["payee"]
        if payee not in amounts:
            amounts[payee] = _paycheck_amount(
                conn, payee, p["occ"]["bill"]["amount"], today, cap=pay_cap)
        events.append((p["due"], amounts[payee], payee))

    # --- daily burn rates ---------------------------------------------------
    st = budget.month_status(conn, today, memo=memo)
    b = st["buckets"]

    # --- sweep-mode savings goals -------------------------------------------
    # A sweep contribution is NOT a fixed outflow: at each month end the
    # goal takes min(plan, that month's realized surplus), floored at
    # zero. The current month's surplus projection is the budget's own
    # month-to-date read (plan surplus accrued to today, corrected by how
    # far variable spending sits from its pace); future months project
    # the standing plan surplus. A month with no measurable surplus
    # schedules nothing — the trough must never show money leaving that
    # the month's numbers say cannot leave. The current month is further
    # reduced by posted evidence (the transfer already left the live
    # balance; scheduling it again would count the money twice). With
    # several sweep goals the surplus is a single pool claimed in config
    # order — priority ordering is deliberately not a feature yet.
    sweep_months: list[tuple[dt.date, list[tuple[str, float]]]] = []
    _sweep_goals = [g for g in _savings.goals(cfg)
                    if _savings.goal_mode(g) == "sweep"
                    and float(g.get("monthly_plan") or 0) > 0]
    if _sweep_goals and st.get("plan_surplus") is not None:
        # the sweep lands at month END, so the current month's cap is the
        # surplus PROJECTED to that point (the standing surplus corrected
        # by today's pace variance — spending on-plan from here realizes
        # exactly it), not the smaller month-to-date accrual the Today
        # line quotes as "available so far"
        cur_surplus = max(0.0, st["plan_surplus"] - (st.get("variance") or 0))
        std_surplus = max(0.0, st["plan_surplus"])
        for y, m in budget.months_spanned(today, end):
            eom = dt.date(y, m, calendar.monthrange(y, m)[1])
            # today IS a month end: the sweep hasn't happened yet, and the
            # walk starts tomorrow — land it on the first walked day
            due = max(eom, today + dt.timedelta(days=1))
            if due > end:
                continue
            current = (y, m) == (today.year, today.month)
            room = cur_surplus if current else std_surplus
            wants: list[tuple[str, float]] = []
            for g in _sweep_goals:
                plan = float(g.get("monthly_plan") or 0)
                if current and not _savings.is_plan_only(g):
                    plan -= max(0.0, _savings.posted_for_plan(
                        conn, cfg, g, since=first, memo=memo))
                take = round(min(max(0.0, plan), room), 2)
                if take > 0.005:
                    room -= take
                    wants.append((str(g["name"]), take))
            if wants:
                sweep_months.append((due, wants))
    plan_daily = ((b["food"]["month_budget"] + b["other"]["month_budget"])
                  / st["days_in_month"])
    # over elapsed days, like the verdict's pace: today is not over yet
    pace_daily = (st["variable_actual"] / (today.day - 1)) if today.day >= 5 else plan_daily
    pace_daily = max(pace_daily, plan_daily * 0.25)   # a quiet week isn't a trend

    # Envelope drip: charge what's LEFT of each pool over the days left
    # in its period, never the full plan rate — the starting balance is LIVE
    # checking, so this period's envelope spend is already out of it; dripping
    # the full monthly again double-counts. When the walk crosses into a NEW
    # period the pool is untouched again (next month/year has no spend yet),
    # so the per-day rate steps at each period roll rather than pretending
    # one blended rate holds across the boundary. month_status already did
    # the pool accounting (used / period_left, -aware) — reuse it.
    env_states = st["buckets"]["fixed"]["envelopes"]
    days_left_month = st["days_in_month"] - today.day          # tomorrow..EOM
    days_left_year = (dt.date(today.year, 12, 31) - today).days
    env_by_day: dict[dt.date, float] = {}
    _d = today
    for _ in range(days):
        _d += dt.timedelta(days=1)
        r = 0.0
        for e in env_states:
            if e["period_months"] >= 12:               # annual pool
                if _d.year == today.year:
                    r += (e["period_left"] / days_left_year
                          if days_left_year else 0.0)
                else:                                   # pool resets Jan 1
                    r += e["pool"] / (366 if calendar.isleap(_d.year) else 365)
            elif (_d.year, _d.month) == (today.year, today.month):
                r += (max(0.0, e["pool"] - e["used"]) / days_left_month
                      if days_left_month else 0.0)
            else:                                       # pool resets at the 1st
                r += e["pool"] / calendar.monthrange(_d.year, _d.month)[1]
        env_by_day[_d] = r
    # today's effective envelope rate — feeds the reported burn `rate` only
    env_daily = env_by_day.get(today + dt.timedelta(days=1), 0.0)

    by_day: dict[dt.date, float] = {}
    for d, amt, _ in events:
        by_day[d] = by_day.get(d, 0.0) + amt

    card_by_day: dict[dt.date, float] = {}
    for d, amt, _ in card_events:
        card_by_day[d] = card_by_day.get(d, 0.0) + amt

    card_stmt_by_day: dict[dt.date, float] = {}
    for d, amt, _ in card_events_stmt:
        card_stmt_by_day[d] = card_stmt_by_day.get(d, 0.0) + amt

    def walk(rate: float, start_val: float | None = None,
             extra: dict[dt.date, float] | None = None):
        bal = start if start_val is None else start_val
        # the chart anchors at TODAY's position — the
        # walk itself still starts tomorrow (today's posted spend is
        # already inside the starting balance)
        dates = [today]
        vals = [bal]
        d = today
        for _ in range(days):
            d += dt.timedelta(days=1)
            bal += by_day.get(d, 0.0) - rate - env_by_day.get(d, 0.0)
            if extra:
                bal += extra.get(d, 0.0)
            dates.append(d)
            vals.append(bal)
        # sweep pass, after the walk knows its own shape: each month-end
        # sweep is additionally capped so the walk's trough never moves —
        # a sweep goal only ever converts surplus the path can spare, so
        # the forecast with a sweep goal never reads worse (lower trough,
        # new shortfall) than the same forecast with no goal at all. The
        # cap re-checks per scenario because each walk troughs differently.
        swept: list[tuple[dt.date, float, str]] = []
        if sweep_months:
            floor_lo = min(vals)
            for due, wants in sweep_months:
                idx = next((i for i, dd in enumerate(dates) if dd >= due),
                           None)
                if idx is None:
                    continue
                spare = max(0.0, min(vals[idx:]) - floor_lo)
                for name, want in wants:
                    take = round(min(want, spare), 2)
                    if take <= 0.005:
                        continue
                    spare -= take
                    for i in range(idx, len(vals)):
                        vals[i] -= take
                    swept.append((due, -take, f"{name} (sweep savings)"))
        lo_i = min(range(len(vals)), key=lambda i: vals[i])
        # the balance ON the first-negative day — the walk can dip a little
        # early and trough far deeper weeks later, and a message quoting
        # min beside negative_date welds the wrong dollar to the wrong day
        neg_i = next((i for i, v in enumerate(vals) if v < 0), None)
        return {"series": [(dd.isoformat(), round(v, 2))
                           for dd, v in zip(dates, vals)],
                "min": round(vals[lo_i], 2),
                "min_date": dates[lo_i].isoformat(),
                "negative_date": (dates[neg_i].isoformat()
                                  if neg_i is not None else None),
                "first_neg_amount": (round(vals[neg_i], 2)
                                     if neg_i is not None else None),
                "end": round(vals[-1], 2),
                "rate": round(rate + env_daily, 2),
                # what this scenario actually swept, for the event list
                "sweep_events": [(dd.isoformat(), round(a, 2), p)
                                 for dd, a, p in swept]}

    plan = walk(plan_daily, start_val=checking_bal, extra=card_by_day)
    pace = walk(pace_daily, start_val=checking_bal, extra=card_by_day)
    pace_now = walk(pace_daily)
    pace_nocards = walk(pace_daily, start_val=checking_bal)
    pace_stmt = walk(pace_daily, start_val=checking_bal, extra=card_stmt_by_day)
    # the event list carries the PRIMARY scenario's realized sweeps so the
    # cash timeline names the month-end dip — sweeps are per-scenario
    # (each walk troughs differently), and pace_stmt is what the product
    # shows as the realistic case
    events_out = ([(d.isoformat(), round(a, 2), p)
                   for d, a, p in sorted(events, key=lambda e: e[0])]
                  + pace_stmt["sweep_events"])
    events_out.sort(key=lambda e: e[0])
    return {
        "start": round(start, 2), "days": days, "card_debt": round(card_debt, 2),
        "checking": round(checking_bal, 2),
        "plan": plan, "pace": pace, "pace_now": pace_now,
        "pace_nocards": pace_nocards, "pace_stmt": pace_stmt,
        "events": events_out,
        "card_events": [(d.isoformat(), round(a, 2), p)
                        for d, a, p in sorted(card_events, key=lambda e: e[0])],
        "card_events_stmt": [(d.isoformat(), round(a, 2), p)
                             for d, a, p in sorted(card_events_stmt,
                                                   key=lambda e: e[0])],
        "card_autopay": [(d.isoformat(), round(a, 2), p)
                         for d, a, p in sorted(card_autopay, key=lambda e: e[0])],
    }
