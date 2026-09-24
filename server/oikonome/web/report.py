"""Daily budget email: verdict-first (UNDER / ON / OVER budget), driven by
the schedule-aware budget engine.

The HTML is NOT built here: it is the shared Today template rendered in
email mode by todayview.render_email — single source with the Today page.
This module builds the gathered status, the subject line, and the
plain-text mirror.

Nothing here is household-specific: checking comes from the per-tenant
config (forecast._checking_balance), and delivery is SMTP resolved per
tenant.
"""

import datetime as dt
import logging
import os
import re
import smtplib
from email.message import EmailMessage

from .. import envnum
from ..envnum import env_flag
from ..engine import alerts, anomalies, bills, budget, forecast, savings
from ..engine.compat import as_date, as_dict
from .data import _as_array

log = logging.getLogger("oikonome.email")


def mailable_base() -> str:
    """OIKONOME_BASE_URL if it is safe to hyperlink in recurring mail,
    else "".

    A bare-IP plain-HTTP link is a classic phishing signature that the big
    mail providers filter silently, and every LAN self-host has exactly that
    shape in OIKONOME_BASE_URL, so the one email the product exists to send
    is the one that gets filtered.

    "Mailable" = https AND a dotted hostname that is not an IP literal and
    not a LAN-only suffix. Everything else gets linkless household mail —
    which costs nothing, because the verdict email is self-contained by
    design (it mirrors the Today page rather than teasing it).

    Household mail only. One-time transactional links (password reset,
    invites, verification) are NOT gated on this: without their link those
    emails are useless, and being rare and user-triggered they don't carry
    the daily-cadence spam profile.
    """
    import ipaddress
    from urllib.parse import urlsplit
    base = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
    if not base:
        return ""
    try:
        u = urlsplit(base)
    except ValueError:
        return ""
    host = (u.hostname or "").lower()
    if u.scheme != "https" or "." not in host:
        return ""
    try:
        ipaddress.ip_address(host)
        return ""                                  # IP literal, even public
    except ValueError:
        pass
    if host.split(".")[-1] in ("local", "lan", "home", "internal", "localdomain"):
        return ""
    return base

# one budget language across every surface (the SPA's
# PlanBars + the shared Today/email template use these names)
BUCKET_LABELS = {"fixed": "Bills", "food": "Food",
                 "other": "Everything else"}


def _d(x) -> str:
    """All dates render as MM/DD/YY."""
    if isinstance(x, str):
        x = dt.date.fromisoformat(x[:10])
    return f"{x:%m/%d/%y}"


def _money(v: float) -> str:
    r = round(v)
    if r == 0:
        r = 0  # normalize -0
    # minus BEFORE the $ — "$-1,116" read garbled
    return f"-${-r:,.0f}" if r < 0 else f"${r:,.0f}"


def bills_remaining_month(conn, cfg: dict, day: dt.date,
                          env_states: list[dict] | None = None,
                          memo: dict | None = None) -> float:
    """Unpaid bill occurrences through month end — the Today plan card's
    "After cards & bills" tile, summed with the same occurrence helper the
    runway and forecast reserve with. That helper re-dates unpaid
    current-month occurrences to at least TOMORROW, so a month-end-only
    horizon would exclude every re-dated bill on the last calendar day and
    the tile would read $0 with rent still unpaid. The horizon is therefore
    never before tomorrow — on EOM this can include a bill genuinely due on
    the 1st, the acceptable direction for a still-owed figure.

    Envelope bills are bills too, but they have no occurrences, so summing
    occurrences alone reads low against the forecast walk. Each envelope
    adds exactly what the walk's drip would charge tomorrow→EOM: a monthly
    pool's max(0, pool − used) in full, an annual pool's rest-of-month share
    of its remaining YEAR pool. Savings-plan residual stays out on purpose —
    the label scopes to bills, and the runway already carries savings
    separately. `env_states` is month_status's envelope accounting for
    `day`; callers holding a status pass it, otherwise it is computed here.
    `memo` is the request-scoped cache gather() threads through every engine
    pass."""
    nxt = (dt.date(day.year + 1, 1, 1) if day.month == 12
           else dt.date(day.year, day.month + 1, 1))
    end = max(nxt - dt.timedelta(days=1), day + dt.timedelta(days=1))
    total = sum(o["planned"] for o in budget.upcoming_bill_occurrences(
        conn, cfg, day, end, memo=memo))
    if env_states is None:
        env_states = budget.month_status(
            conn, day, memo=memo)["buckets"]["fixed"]["envelopes"]
    days_left_month = (nxt - dt.timedelta(days=1) - day).days  # tomorrow..EOM
    days_left_year = (dt.date(day.year, 12, 31) - day).days
    if days_left_month:
        for e in env_states:
            if e["period_months"] >= 12:            # annual pool
                total += (e["period_left"] * days_left_month / days_left_year
                          if days_left_year else 0.0)
            else:
                total += max(0.0, e["pool"] - e["used"])
    return round(total, 2)


def gather(conn, today: dt.date, *, live: bool = True) -> dict:
    """live=False: `today` is a PAST day the user navigated to (Today page
    back-nav). The verdict/plan/bucket math is untouched — same month_status,
    same as-of date — but the NOW-facts are suppressed: cash forecast,
    runway/headroom, and alerts describe the present, not that day, so they
    come back None/empty and nothing is written to the alert log. Callers
    pass the flag explicitly (never inferred from dt.date.today()) because
    the email job legitimately gathers on a LOCAL date that can differ from
    the server's UTC date."""
    # A past month must not have TODAY's live bill schedule projected onto
    # it. But only a live=False caller is ever showing a past day: live=True
    # asserts `today` IS the caller's present, in the CALLER's zone — the
    # email sweep passes the household's local date, which near a month
    # boundary can legitimately sit in a different month than this
    # process's own clock. Re-deciding liveness against dt.date.today()
    # here would mark those sends "historical" and drop the whole
    # fixed-bill schedule from a live daily email.
    real_today = dt.date.today()
    historical = ((not live)
                  and (today.year, today.month)
                  != (real_today.year, real_today.month))
    # One gather is one snapshot: the month status, the forecast walk and
    # goal progress each derive the same sweep-goal "posted this month"
    # evidence, and each derivation is a pair of unindexable LIKE scans
    # over the whole ledger. They share this dict so the work happens
    # once; it dies with the call, so it can never serve a stale number.
    memo: dict = {}
    # One config load per gather: month_status accepts the dict, and the
    # today_view read below reuses it — this runs on every Today request
    # and once per tenant per email sweep, so a second identical
    # tenant_settings round trip here would be pure waste.
    cfg = budget.load_config(conn, memo=memo)
    status = budget.month_status(conn, today, historical=historical,
                                 cfg=cfg, memo=memo)
    status["today"] = today
    # which hero face the PAGE shows (today_view setting; absent =
    # simple). The daily email's face is the separate
    # email_schedule.daily.summary setting — the email sweep passes its
    # own face and does not follow this toggle.
    _tv = cfg.get("today_view") or "summary"
    status["today_view"] = ("detail" if _tv == "advanced"
                            else "summary" if _tv == "simple" else _tv)
    # merchant logos on/off (Settings → "merchant logos"; default on). The
    # pages read it from /api/me; the EMAIL never shows a logo — an off-site
    # <img> is an open-tracking pixel and breaks behind a LAN image proxy
    # (the standing no-remote-images rule, test_email_safety_invariants)
    status["merchant_logos"] = cfg.get("merchant_logos", True) is not False
    # the "recent" window: yesterday + today, clamped to the 1st (month_status
    # rows only cover the current month, so on the 1st this is today alone)
    win_start = max(today - dt.timedelta(days=1), today.replace(day=1))
    status["yesterday"] = win_start   # legacy name: start of the recent window
    span = {win_start + dt.timedelta(days=i)
            for i in range((today - win_start).days + 1)}
    # the recent list is a DISPLAY of the ledger, so it shows the ledger's
    # name (merchant row / rename) — the raw `payee` the verdict matched on
    # stays on status["rows"] for the matching that needs it
    status["yesterday_rows"] = [
        {**dict(r), "payee": r.get("display_payee") or r["payee"]}
        for r in status["rows"] if as_date(r["date"]) in span]
    # What each Amazon order in the recent window actually contained, keyed
    # by txn id to match the recent rows — the template and the plain-text
    # mirror both render it.
    _recent_ids = [r["txn_id"] for r in status["yesterday_rows"] if r.get("txn_id")]
    # Costco receipts share the slot: their breakdown by what was bought
    # is the same kind of sentence, rendered the same way.
    status["amazon_summaries"] = {
        r["transaction_id"]: r["summary"]
        for r in conn.execute(
            """SELECT am.transaction_id, s.summary
                 FROM amazon_matches am
                 JOIN amazon_summaries s ON s.dedup_key = am.dedup_key
                WHERE am.transaction_id = ANY(%s)
               UNION ALL
               SELECT cm.transaction_id, cr.summary
                 FROM costco_matches cm
                 JOIN costco_receipts cr ON cr.dedup_key = cm.dedup_key
                WHERE cm.transaction_id = ANY(%s) AND cr.summary <> ''""",
            [_recent_ids, _recent_ids])
    } if _recent_ids else {}
    # "due soon" window = today → the next expected paycheck; falls back to
    # 7 days if no income series is set
    try:
        paycheck = forecast.next_paycheck(conn, today, memo=memo)
    except Exception:
        paycheck = None
    due_end = paycheck or (today + dt.timedelta(days=7))
    status["next_paycheck"] = paycheck.isoformat() if paycheck else None
    status["due_soon_end"] = due_end.isoformat()
    # Companion fees are read through the shape guard, not a COALESCE:
    # substituting for a MISSING list says nothing about a wrong-shaped
    # one, and jsonb_array_elements over an object RAISES, which aborts
    # the gather. That would take the household's nightly mail off the air
    # over one bill — a failure nobody can see, unlike a blank page. A
    # bill whose companions are the wrong shape simply carries no fee.
    status["due_soon"] = conn.execute(
        """SELECT payee, amount, due_on, category,
                  COALESCE((SELECT SUM((c->>'amount')::numeric)
                              FROM jsonb_array_elements(""" +
        _as_array("raw->'companions'") + """) c), 0)
                    AS fee
             FROM bills
            WHERE amount < 0 AND active = 1 AND due_on BETWEEN %s AND %s
            ORDER BY due_on""", (today, due_end)).fetchall()
    # 60-day cash forecast (failure-tolerant; email must send regardless).
    # A past-day view gets None: the forecast projects from CURRENT account
    # balances, so running it "as of" a past date fabricates a trajectory
    # that never existed — the SPA hides the pane, we skip the work.
    try:
        status["forecast"] = (forecast.build(conn, today=today, memo=memo)
                              if live else None)
    except Exception:
        status["forecast"] = None
    # anomaly watch (a now-signal — quiet on a past-day view)
    try:
        status["anomalies"] = anomalies.detect(conn, today) if live else []
    except Exception:
        status["anomalies"] = []
    # recurring detection: pending-proposal count + auto-applied drifts
    try:
        status["recurring_pending"] = len(bills.pending_proposals(conn))
        status["recurring_drifts"] = [
            {"payee": r["payee"], **as_dict(r["evidence"])}
            for r in bills.recent_auto_changes(conn, days=1)]
    except Exception:
        status["recurring_pending"] = 0
        status["recurring_drifts"] = []
    # merchant spellings offered as one business — a queue on the
    # Merchants page, surfaced the same quiet way as recurring proposals
    try:
        from ..engine import merchant_merge
        status["merge_offers"] = merchant_merge.notice(conn)
    except Exception:
        status["merge_offers"] = None

    # cash runway = SOLVENCY check: can checking pay off ALL outstanding card
    # debt AND every bill due before the next paycheck right now? "Right
    # now" is the point: balances are CURRENT, so on a past-day view the
    # whole runway is a now-fact — None, and the SPA hides headroom, the
    # cash notice, and the balance-unreliable nudge along with it.
    cfg = status["config"]
    if not live:
        status["runway"] = None
    else:
        checking_bal = forecast._checking_balance(conn, cfg)
        # forecast.build's own card-scope predicate, spliced rather than
        # restated: its card_debt and this tile's render side by side on
        # Today and in the nightly email, and a second copy of the rule is
        # a second thing to forget to update
        cards = conn.execute(
            f"""SELECT i.institution_name AS bank, a.mask, a.balance_current AS bal
               FROM accounts a JOIN items i ON i.id = a.item_id
               WHERE a.type = 'credit' {forecast.CARD_SCOPE_SQL}
               ORDER BY a.balance_current DESC""",
            (budget.excluded_account_ids(cfg),)).fetchall()
        card_debt = sum(max(0.0, c["bal"] or 0.0) for c in cards)
        horizon = paycheck or (today + dt.timedelta(days=14))
        # The same rule the forecast uses — occurrence-matched against MTD
        # spend (a bill paid early is already out of checking; reserving it again
        # would make headroom too tight), overdue-unpaid re-dated rather than
        # dropped (dropping would make it too rosy), and every month in the window expanded,
        # not just the endpoints. True occurrences, so a weekly bill lands
        # twice in 15 days.
        due14 = [{"payee": b["payee"], "amount": b["planned"],
                  "due_on": b["due"].isoformat()}
                 for b in budget.upcoming_bill_occurrences(conn, cfg, today,
                                                           horizon, memo=memo)]
        due_total = sum(r["amount"] for r in due14)
        # The forecast walk schedules each savings goal's
        # unposted plan inside this same window — money as committed as a
        # bill — so due_total carries it too, or headroom would read too
        # rosy by exactly that residual. Same posted math as the walk;
        # a horizon crossing the 1st owes next month's plan too.
        from ..engine import savings as _savings
        first = today.replace(day=1)
        nxt = (dt.date(today.year + 1, 1, 1) if today.month == 12
               else dt.date(today.year, today.month + 1, 1))
        for g in _savings.goals(cfg):
            plan = float(g.get("monthly_plan") or 0)
            # a sweep-mode plan is not a commitment — it moves only if the
            # month's surplus exists, so reserving it against headroom
            # would reintroduce exactly the pessimism sweep mode removes
            if plan <= 0 or _savings.goal_mode(g) == "sweep":
                continue
            posted = (0.0 if _savings.is_plan_only(g) else max(
                0.0, _savings.posted_for_plan(conn, cfg, g, since=first,
                                              memo=memo)))
            due_total += max(0.0, round(plan - posted, 2))
            if nxt <= horizon:
                due_total += plan
        due_total = round(due_total, 2)
        # a files-only install has manual accounts whose balance nobody
        # ever set — checking reads $0 and every cash signal screams "out of
        # money" at a brand-new user. When the $0 comes from manual accounts
        # only (no aggregator-fed depository) and transactions exist, flag the
        # balance as unreliable so the surfaces show a "set your balance" nudge
        # instead of the alarm.
        balance_unreliable = False
        if not checking_bal:
            dep = conn.execute(
                """SELECT COUNT(*) FILTER (WHERE item_id != 'manual') AS live,
                          COUNT(*) AS total
                   FROM accounts WHERE type = 'depository'""").fetchone()
            has_rows = conn.execute("SELECT 1 FROM transactions LIMIT 1").fetchone()
            balance_unreliable = bool(dep and dep["total"] and not dep["live"]
                                      and has_rows)
        status["runway"] = {
            "checking": checking_bal,
            "balance_unreliable": balance_unreliable,
            "card_debt": card_debt,
            "cards": [(c["bank"], c["mask"], max(0.0, c["bal"] or 0.0))
                      for c in cards if (c["bal"] or 0) > 0.5],
            "due_total": due_total,
            "due_count": len(due14),
            "required": card_debt + due_total,
            "horizon": horizon,
        }
    # goals and the plan card's "After cards & bills" tile ride the
    # gathered status so the SPA payload and the email render the same
    # numbers — the email mirrors the Today page from one source.
    try:
        status["savings_goals"] = savings.progress(conn, cfg, today,
                                                  memo=memo)
    except Exception:
        status["savings_goals"] = []
    try:
        status["bills_remaining_month"] = (
            bills_remaining_month(
                conn, cfg, today,
                env_states=status["buckets"]["fixed"]["envelopes"],
                memo=memo)
            if live else None)
    except Exception:
        status["bills_remaining_month"] = None
    # charges flagged "reimbursement expected" — surfaced as an alert.
    # The engine helper nets partial receipts off each charge, so the
    # total is what's still OWED, not the charges' face value.
    try:
        from ..engine import alerts as alerts_engine
        status["reimb_pending"] = alerts_engine.reimb_pending(conn)
    except Exception:
        status["reimb_pending"] = None
    # onboarding nudge: data exists but no bills tracked and nothing pending
    # → point at the recurring-bills finder (dismissable like any alert)
    try:
        # only "at least 20 rows" is ever asked — a full ledger count scans
        # the whole table on every Today request to answer it
        n_txn = conn.execute(
            "SELECT COUNT(*) AS n FROM (SELECT 1 FROM transactions "
            "WHERE removed=0 LIMIT 20) s").fetchone()["n"]
        n_bills = conn.execute("SELECT COUNT(*) AS n FROM bills "
                               "WHERE active=1").fetchone()["n"]
        status["needs_recurring_setup"] = (
            n_txn >= 20 and n_bills == 0
            and not status.get("recurring_pending"))
    except Exception:
        status["needs_recurring_setup"] = False
    # unified alert strip (worst first) + persistent log; dismissed alerts
    # stay out of the strip but keep their history row. Alerts are
    # now-facts: a past-day view gets none and must never write the log
    # (it would stamp stale signals with today's date).
    if not live:
        status["alerts"] = []
        return status
    try:
        status["stale_pulls"] = alerts.freshness(conn)
        # auto-removed (reaped) connections — the "reconnect"
        # one-liner in the email + Today strip
        status["reaped_items"] = alerts.reaped_connections(conn)
        # the nightly LLM run's own verdict — a dead model backend is
        # otherwise silent for days
        status["llm_failing"] = alerts.llm_failing(conn)
        built = alerts.build(status)
        if not historical:
            # the household's day, like the worker's writer: two writers a
            # day apart would let the UTC-evening one outrank the other
            alerts.log(conn, built, today if live else real_today)
        status["alerts"] = alerts.visible(conn, built)
    except Exception:
        status["alerts"] = []
    return status


def build(d: dict, *, summary: bool = False) -> tuple:
    """subject + plain-text mirror + HTML (the shared Today template in
    email mode — single source with the Today page).

    summary=True (email_schedule.daily.summary) sends only the verdict
    pane — the hero card with the left-to-spend tiles and pinned bills —
    for households that want the answer without the report. Both renders
    cut at the same boundary so HTML and plain stay mirrors."""
    verdict, variance = d["verdict"], d["variance"]
    # the recent list shows VARIABLE spend only — the subject $ matches it
    fixed_ids = {r["txn_id"] for r in d["buckets"]["fixed"]["rows"]}
    y_rest = [r for r in d["yesterday_rows"] if r["txn_id"] not in fixed_ids]
    spent_yday = sum(r["amount"] for r in y_rest)
    yday_label = "Recent transactions"

    if verdict == "ON BUDGET":
        v_detail = "variable spending on pace"
    elif verdict == "UNDER BUDGET":
        v_detail = f"{_money(-variance)} ahead on variable spending"
    else:
        v_detail = f"{_money(variance)} over on variable spending"

    # on plan is green in the subject too — it is the same signal as the
    # verdict pill, and amber there reads as a warning
    emoji = {"UNDER BUDGET": "🟢", "ON BUDGET": "🟢", "OVER BUDGET": "🔴"}[verdict]
    win = (d["today"] - d["yesterday"]).days
    win_label = ("today" if win == 0 else "since yesterday" if win == 1
                 else f"last {win} days")
    subject = (f"{emoji} {verdict.capitalize()} — {_d(d['today'])}: "
               f"{_money(spent_yday)} {win_label}"
               + (f", {v_detail}" if verdict != "ON BUDGET" else ""))

    var_budget_total = (d["buckets"]["food"]["month_budget"]
                        + d["buckets"]["other"]["month_budget"])
    remaining = var_budget_total - d["variable_actual"]

    # cash: quiet until it matters — plain-text mirror of the
    # HTML block's three states. autopay-statement is the
    # realistic with-cards trajectory.
    rw = d.get("runway") or {}
    spare = None
    fc = d.get("forecast")
    # When the balance is known to be stale, todayview drops the whole
    # forecast — SPA card, email card, cash notice — rather than present a
    # number seeded from a lie. The plain-text mirror cannot show a caveat,
    # so it suppresses the same way; the set-your-balance
    # nudge in the verdict still tells them why it is quiet.
    if rw.get("balance_unreliable"):
        fc = None
    pc = fc["pace_stmt"] if fc else None
    if rw.get("checking") is not None and not rw.get("balance_unreliable"):
        spare = rw["checking"] - rw["required"]
    income = d.get("income")
    paycheckish = (income / 2) if income else 1000
    neg_in = None
    if pc and pc.get("negative_date"):
        neg_in = (dt.date.fromisoformat(pc["negative_date"])
                  - d["today"]).days
    cash_critical = ((spare is not None and spare < 0)
                     or (neg_in is not None and neg_in <= 14))
    cash_tight = not cash_critical and (
        (spare is not None and spare < paycheckish)
        or bool(pc and (pc["min"] < 0 or neg_in is not None)))
    # the shortfall quoted beside negative_date is the balance ON that day
    # (`first_neg_amount`), never the possibly-deeper later `min` — same
    # pairing as todayview.build_context, which the HTML renders
    first_neg = pc.get("first_neg_amount") if pc else None
    slow_to = None
    if first_neg is not None and first_neg < 0 and neg_in and neg_in > 0:
        slow_to = max(0.0, (pc.get("rate") or 0) + first_neg / neg_in)
    slow_txt = (f", or slow spending to {_money(slow_to)}/day"
                if slow_to is not None else "")
    cash_line = None
    if cash_critical:
        if spare is not None and spare < 0:
            cash_line = (f"!! CASH: short {_money(-spare)} today — checking "
                         f"doesn't cover cards & bills before the next "
                         f"paycheck. Cover it from savings{slow_txt}.")
        else:
            by = _d(pc["negative_date"]) if pc.get("negative_date") else "now"
            short = first_neg if first_neg is not None else pc["min"]
            cash_line = (f"!! CASH: short {_money(-short)} by {by} — "
                         f"cover it from savings{slow_txt}.")
    elif cash_tight:
        if pc and (pc["min"] < 0 or neg_in is not None):
            cash_line = (f"Cash gets tight around {_d(pc['min_date'])} "
                         f"(low {_money(pc['min'])}).")
        else:
            cash_line = (f"Cash is tight — {_money(spare)} left after cards "
                         f"& bills before the next paycheck.")

    month_bills = d["buckets"]["fixed"]["month_budget"]
    weight = month_bills - d["avg_bills"]
    weight_txt = (f"heavy month, +{_money(weight)} vs average" if weight > 100
                  else f"light month, −{_money(-weight)} vs average" if weight < -100
                  else "typical month")

    summaries = d.get("amazon_summaries") or {}

    # HTML = the shared Today template, email mode. The context is built
    # ONCE and shared with the plain-text mirror below — it aggregates
    # the whole hero (allowances, bill cards, why chips, MTD rollups),
    # which is too much work to redo for a second surface of the same
    # send.
    from .todayview import _moneyc, build_context, render_email
    ctx = build_context(d)
    html = render_email(d, summary=summary, ctx=ctx)

    # plain-text mirror — same section order as the html / Today page.
    # Brand line mirrors the HTML header's link; only for a MAILABLE base
    # (a LAN URL in the body gets the whole message filtered).
    base = mailable_base()
    plain = ([f"Oikonome — {base}/app/", ""] if base else []) + \
        [f"Daily budget — {_d(d['today'])}", ""]
    # alerts are not here: they close the mail, per recipient, inside the
    # "Needs you" section (notify/mailact) — and only for an owner or member
    # hero mirror: verdict sentence, then the
    # left-to-spend-today tiles as one line, each tile carrying its own
    # plan-vs-now daily note — same strings as the Today page and the
    # HTML, from the ONE build_context above, so nothing drifts
    day_ctx = ctx["day"]
    state = ("Over budget" if verdict == "OVER BUDGET"
             else "Under budget" if verdict == "UNDER BUDGET"
             else "On budget")
    # the email face is the email_schedule.daily.summary choice, NOT the
    # page's today_view toggle: summary email = the
    # verdict pane with the simple face, detail email = the full report
    # with the advanced face, whatever the page shows
    view = "summary" if summary else "detail"
    if view != "detail":
        tile_line = (f"{_money(day_ctx['simple']['left_today'])}"
                     + (" — " + ", ".join(
                         c["text"] for c in day_ctx["simple"]["chips"])
                        if day_ctx["simple"]["chips"] else ""))
    else:
        tile_line = " · ".join(
            (f"{label} $0 (over by {_money(-a['left_today'])})"
             if a["left_today"] < -0.5
             else f"{label} {_money(max(0.0, a['left_today']))} "
                  f"of {_money(a['today_allowance'])}")
            + (f" ({a['daily_note']}"
               + (f"; {a['recover_note']}" if a.get("recover_note") else "")
               + ")" if a.get("daily_note") else "")
            for label, a in day_ctx["allow_tiles"])
    plain += [f"{state} — {day_ctx['pace_line']} · "
              f"{_money(abs(remaining))} "
              f"{'over the' if remaining < 0 else 'of'} "
              f"{_money(var_budget_total)}"
              f"{'' if remaining < 0 else ' left'} · "
              f"{day_ctx['days_left']} "
              f"day{'' if day_ctx['days_left'] == 1 else 's'}",
              f">> LEFT TO SPEND TODAY: {tile_line} <<"]
    # pinned bills (bill setting "pin to Today") — the same server-composed
    # card sentences the page and the HTML render
    if day_ctx["bill_cards"]:
        card_line = " · ".join(
            f"{c['label']} {_money(c['left'])} "
            f"{'left' if c['kind'] == 'envelope' else 'to go'} "
            f"({c['sub']}{'; ' + c['status'] if c['status'] else ''})"
            for c in day_ctx["bill_cards"])
        plain += [f"Pinned bills: {card_line}"]
    if cash_line:
        plain += [cash_line]
    # the WHY, over-budget only — a good day is quiet: merchant chips as
    # one line, then the per-category
    # sentences, all server-composed alongside the other two surfaces.
    w = d.get("why") or {}
    if day_ctx["why_chips"]:
        chip_txt = ", ".join(
            f"{c['payee'][:26]} {_money(c['amount'])}"
            + (f" ({c['count']}x)" if c["count"] > 1 else "")
            for c in day_ctx["why_chips"])
        plain += [f"Driving it: {chip_txt}"]
        for e in w.get("entries") or []:
            plain += [f"  - {e['name']} {e['text']}"]
        # the over-budget way back — same server-composed sentence the
        # Today page, the app and the HTML above render
        if w.get("recovery"):
            plain += [f"  {w['recovery']}"]
    # the plan as ONE flow line — monthly facts, not
    # daily news; the Budget page owns the detail
    if view != "detail":
        # the simple face drops the plan chain (the Budget page owns it);
        # the sweep sentence still lands, alone, when there is one
        if day_ctx.get("sweep_line"):
            # verbatim — the same composed sentence every surface quotes;
            # re-casing it here is exactly the drift the shared string
            # exists to prevent
            plain += ["", day_ctx["sweep_line"]]
    plan_flow = (f"Plan: "
                 f"{_money(d['income']) if d.get('income') is not None else '·'} in -> "
                 f"{_money(month_bills)} bills "
                 f"(avg {_money(d['avg_bills'])}; {weight_txt}) -> "
                 f"{_money(var_budget_total)} to spend")
    # the surplus subtracts the savings plan, so without this leg the chain
    # lands on a number the terms shown do not produce
    if (d.get("savings_plan") or 0) > 0.005:
        plan_flow += f" -> {_money(d['savings_plan'])} saved"
    if d.get("plan_surplus") is not None:
        plan_flow += f" -> {_money(d['plan_surplus'])} excess"
    # the sweep goals' line — the SAME server-composed sentence the page
    # and the HTML render, beside the chain (a sweep takes from the
    # excess only when the month provides it, so it is not a chain link)
    if day_ctx.get("sweep_line"):
        plan_flow += f" · {day_ctx['sweep_line']}"
    if view == "detail":
        plain += ["", plan_flow]
    if d.get("variable_scale"):
        sc = d["variable_scale"]
        plain += [f"  Heavy bill month: variable budgets scaled to {_money(sc['available'])} "
                  f"(from {_money(sc['static_total'])})"]
    # the summary email ends with the verdict pane — everything the HTML's
    # hero card carries is now said; the map/timeline/goals sections below
    # belong to the extensive version only
    if summary:
        plain += ["", "(summary email — open Oikonome for the full report)"]
        return subject, "\n".join(plain), html
    if d.get("irregular_bills"):
        plain += ["  Non-monthly bills this month: " + ", ".join(
            f"{i['payee']} {_money(i['amount'])} ({i['label']})"
            for i in d["irregular_bills"][:4])]
    # cash timeline header — cash's one home (checking, cards, after
    # everything, and the forecast low), one line
    if rw.get("checking") is not None:
        after_cards = rw["checking"] - rw["card_debt"]
        cash_now = (f"Cash timeline: checking {_money(rw['checking'])} · "
                    f"card debt {_money(rw['card_debt'])}")
        if d.get("bills_remaining_month") is not None:
            cash_now += (" · after cards & bills "
                         + _money(after_cards - d["bills_remaining_month"]))
        if pc:
            cash_now += (f" · low {_money(pc['min'])} {_d(pc['min_date'])}")
        plain += ["", cash_now]
    # savings goals — trackable only (plan-only rows have nothing to
    # report), and the advanced face only: the simple face tracks goals on
    # the Budget page, matching the HTML and the Today page
    goals = ([] if view != "detail" else
             [g for g in d.get("savings_goals") or []
              if not g.get("plan_only")])
    if goals:
        plain += ["", "Savings goals:"]
        for g in goals:
            # open-ended goal (no target): "$X saved" + the measured rate —
            # never "of $0" (mirrors Today.tsx)
            if not g.get("target"):
                pace = (f"saving {_money(g['rate_90d'])}/mo"
                        if (g.get("rate_90d") or 0) > 0
                        else "no contributions in 90 days")
                plain += [f"  {g['name']}: {_money(g.get('saved') or 0)} "
                          f"saved — {pace}"]
                continue
            pace = ("funded" if (g.get("pct") or 0) >= 100
                    else "off pace" if g.get("on_pace") is False
                    else "on pace" if g.get("on_pace")
                    else (f"~{_d(g['eta'])} at the current rate"
                          if g.get("eta") else "no contributions in 90 days"))
            plain += [f"  {g['name']}: {_money(g.get('saved') or 0)} of "
                      f"{_money(g.get('target') or 0)}"
                      + (f" ({g['pct']}%)" if g.get("pct") is not None else "")
                      + f" — {pace}"]
    plain += [""]
    vsign = "+" if d["variance"] > 0 else "-"
    plain.append(f"{'Variable spending':<22} spent {_money(d['variable_actual']):>11}  "
                 f"expected {_money(d['variable_expected']):>11}  "
                 f"({vsign}{_money(abs(d['variance']))})")
    for key in ("food", "other"):
        b = d["buckets"][key]
        kids = b.get("children") or []
        # with custom buckets the parent line shows the remainder
        # after the carve-outs; each child gets its own indented line
        actual = b["display_actual"] if kids else b["actual"]
        expected = b["display_expected"] if kids else b["expected"]
        sign = "+" if actual - expected > 0 else "-"
        plain.append(f"  {BUCKET_LABELS[key]:<20} spent {_money(actual):>11}  "
                     f"expected {_money(expected):>11}  "
                     f"({sign}{_money(abs(actual - expected))})")
        for c in kids:
            csign = "+" if c["variance"] > 0 else "-"
            plain.append(f"    {c['name'][:18]:<18} spent {_money(c['actual']):>11}  "
                         f"expected {_money(c['expected']):>11}  "
                         f"({csign}{_money(abs(c['variance']))})")
    fx = d["buckets"]["fixed"]
    plain.append(f"{'Fixed bills (info)':<20} posted {_money(fx['actual']):>10}  "
                 f"awaiting {_money(fx.get('unpaid_due', 0)):>10}  "
                 f"month {_money(fx['month_budget'])}")
    if d.get("overdue_unpaid"):
        plain += ["Scheduled but not yet posted: "
                  + ", ".join(f"{p} {_money(a)}" for p, a, _ in d["overdue_unpaid"][:4])]
    plain += ["", f"{yday_label} ({_money(spent_yday)} variable):"]
    if y_rest:
        # cents on transaction rows, like the site's tables
        plain += [f"    {_moneyc(r['amount']):>10}  "
                  f"{r['payee'] + ' — ' + summaries[r['txn_id']] if r['txn_id'] in summaries else r['payee']}"
                  f"  [{r['category']}]" for r in y_rest]
    else:
        plain += ["  (none)"]
    return subject, "\n".join(plain), html


def _flag_on_unless_disabled(name: str) -> bool:
    """An env boolean whose safe state is ON: unset (or forwarded empty by
    compose) stays on, and only a real value is parsed.

    `envnum.env_flag` owns the one list of truthy spellings but reads unset
    as False, which is the wrong default for a knob that protects
    something. Reading it as `== "1"` instead would mean an operator who
    writes `true` silently gets STARTTLS disabled: mail in the clear while
    the configuration says the opposite."""
    return (envnum.env_flag(name)
            if (os.environ.get(name) or "").strip() else True)


def _env_smtp() -> dict:
    host = os.environ.get("OIKONOME_SMTP_HOST", "")
    return {"host": host or "localhost",
            "port": int(envnum.env_num("OIKONOME_SMTP_PORT", "587")),
            "user": os.environ.get("OIKONOME_SMTP_USER"),
            "password": os.environ.get("OIKONOME_SMTP_PASSWORD"),
            "sender": os.environ.get("OIKONOME_SMTP_FROM",
                                     "oikonome@localhost"),
            "starttls": _flag_on_unless_disabled("OIKONOME_SMTP_STARTTLS"),
            "configured": bool(host)}


def resolve_smtp(conn) -> dict:
    """Effective SMTP settings for this tenant.

    Hosted: always the operator's env relay — a tenant row is ignored even
    if one exists, though the API refuses to save it. Self-hosted: the
    Settings-card config (password encrypted at rest) wins, with the
    operator's env vars as the fallback, and a host the netguard blocks is
    treated as not-configured rather than probed.

    `configured` says whether email can send at all."""
    from ..db import crypto
    from ..engine import budget
    # Hosted: the operator's relay, always. The API refuses to save a
    # tenant smtp_host there, but a row that got in some other way (a
    # restore, direct SQL) would otherwise keep winning here — and a stale relay that
    # still answers is the failure that looks like nothing is wrong.
    if env_flag("OIKONOME_HOSTED"):
        return _env_smtp()
    cfg = budget.load_config(conn)
    if cfg.get("smtp_host"):
        # belt: a stored config (restored, or smuggled past a
        # future door) is re-checked at the moment of use — a blocked host
        # is treated as not-configured (env fallback, which is the
        # operator's own and stays trusted) instead of probing an internal
        # host every night. Raising here would 500 invites and /doctor.
        from .netguard import BlockedURL, check_host
        try:
            check_host(str(cfg["smtp_host"]), what="the configured SMTP host")
        except BlockedURL as e:
            import logging
            logging.getLogger("oikonome.email").warning(
                "tenant SMTP host blocked by policy, using env fallback: %s",
                e)
            return _env_smtp()
        return {"host": cfg["smtp_host"],
                "port": int(cfg.get("smtp_port") or 587),
                "user": cfg.get("smtp_user") or None,
                "password": crypto.decrypt(conn, cfg.get("smtp_password"))
                            if cfg.get("smtp_password") else None,
                "sender": cfg.get("smtp_from") or "oikonome@localhost",
                "starttls": bool(cfg.get("smtp_starttls", True)),
                "configured": True}
    return _env_smtp()


def send_each(subject: str, plain: str, html: str, recipients: list[str],
              sender: str | None = None,
              attachments: list[tuple[str, bytes, str]] | None = None,
              smtp: dict | None = None,
              unsubscribe: str | None = None,
              personal=None) -> list[dict]:
    """Send household mail as ONE MESSAGE PER RECIPIENT, each addressed to
    that person. Returns [{email, ok, error}] — one row per recipient.

    `personal(address) -> (plain, html)` adds a section that belongs to ONE
    copy — the daily verdict's "Needs you" block, whose buttons are signed
    for the address they were mailed to and which a viewer's copy must not
    carry at all. It goes in before the unsubscribe footer, so the footer
    stays last.

    `unsubscribe=<tenant_id>` closes each copy with a per-address
    unsubscribe footer (and the List-Unsubscribe headers mail clients turn
    into their own button). One link, one effect: the address that
    received the mail is muted for every household email. Household mail
    passes it; transactional mail (invites, resets, the test send) does
    not, because those are not a subscription.

    List mail is not sent as To=sender + Bcc=everyone: under that shape a
    second address on the recipient list can silently never receive the
    daily verdict while the first does. Three reasons for the fan-out:

    * **Deliverability.** `smtplib.send_message` strips the Bcc header, so a
      Bcc blast arrives as `To: <sender>` with the actual
      recipient nowhere in the visible headers. "Not addressed to you" is one
      of the strongest spam signals there is, and it is applied per-recipient:
      one address gets it, another does not, from the same send.
    * **Visibility.** One blast has ONE outcome, so a per-recipient failure
      is invisible: the sweep would record success as long as the relay
      accepted the envelope. Here every address has its own result.
    * **Privacy.** Bcc already hid recipients from each other; a per-recipient
      send hides them at least as well, and drops the copy addressed to the
      sender that arrives twice on a mail-alias fan-in.

    One connection per recipient — deliberate. A household list is a handful
    of addresses, and an isolated failure must not cost the others their mail.
    """
    from ..notify import delivery
    results = []
    # ONE pooled read up front. `clear()` opens an unpooled ADMIN connection
    # to delete a row that, on the overwhelmingly common path, is not there,
    # so calling it per recipient every cadence would pay for nothing.
    # Only addresses actually carrying a
    # failure need healing.
    was_failing = delivery.failing(recipients)
    for r in recipients:
        try:
            p, h, unsub_url = plain, html, None
            if personal is not None:
                from ..notify import mailact
                p, h = mailact.attach(p, h, *personal(r))
            if unsubscribe:
                from ..notify import unsubscribe as _unsub
                p, h, unsub_url = _unsub.decorate(p, h, unsubscribe, r)
            send(subject, p, h, [r], sender=sender,
                 attachments=attachments, smtp=smtp, bcc=False,
                 list_unsubscribe=unsub_url or None)
            results.append({"email": r, "ok": True, "error": None})
            # a delivery that the relay accepted is the only
            # evidence that an address works — spend it. This is
            # what makes a fixed address heal on its own instead of
            # waiting for someone to notice the stale warning.
            if r.strip().lower() in was_failing:
                delivery.clear(r)
        except Exception as e:                    # noqa: BLE001 — per-address
            log.warning("email to %s failed: %s: %s", r, type(e).__name__, e)
            err = f"{type(e).__name__}: {e}"
            results.append({"email": r, "ok": False, "error": err})
            # once the provider suppresses an address there is no
            # bounce webhook any more — the refusal at submission is the
            # ONLY remaining signal, and this is where it is heard.
            delivery.record_send_failure(r, err)
    return results


def send(subject: str, plain: str, html: str, recipients: list[str],
         sender: str | None = None,
         attachments: list[tuple[str, bytes, str]] | None = None,
         smtp: dict | None = None, bcc: bool = True,
         list_unsubscribe: str | None = None):
    """SMTP delivery — settings from the `smtp` dict (resolve_smtp) when
    given, else env (self-hosted default).
    attachments: (filename, data, mime like "application/zip") tuples.

    bcc=True (the daily-verdict default): To=sender, recipients Bcc'd so a
    household's several recipients don't see each other. bcc=False for
    single-recipient TRANSACTIONAL mail (invite, verification, reset,
    operator notice) — To=the recipient directly: To=self+Bcc delivers
    twice when sender and recipient share a mailbox (an alias fan-in) and
    makes the To header read as the sender."""
    s = smtp or _env_smtp()
    host, port = s["host"], s["port"]
    user, password = s["user"], s["password"]
    sender = sender or s["sender"]
    msg = EmailMessage()
    msg["From"] = sender
    if bcc:
        msg["To"] = sender
        msg["Bcc"] = ", ".join(recipients)
    else:
        msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    if list_unsubscribe:
        # RFC 2369 + RFC 8058: the client's own Unsubscribe button POSTs
        # `List-Unsubscribe=One-Click` to this URL — the same door the
        # footer link opens, minus the confirm page (see web.app /unsubscribe).
        msg["List-Unsubscribe"] = f"<{list_unsubscribe}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    # A relay that rewrites links for tracking can spend one-time tokens
    # (password reset, welcome set-password) before the human clicks, so
    # tracking is disabled per message where the relay supports it — here,
    # Postmark's headers, gated on the host so other relays never see them.
    if "postmark" in (host or "").lower():
        msg["X-PM-TrackOpens"] = "false"
        msg["X-PM-TrackLinks"] = "None"
    html, inline = inline_data_images(html)
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    # the images the HTML embeds (the brand mark, the cash chart) become
    # multipart/related parts: Content-Disposition inline, no filename,
    # referenced by cid. Clients that list every non-text part (Proton,
    # Gmail) show them among the attachments too — accepted; the image is
    # still never fetched from anywhere.
    for cid, subtype, data in inline:
        msg.get_payload()[-1].add_related(
            data, maintype="image", subtype=subtype, cid=f"<{cid}>",
            disposition="inline")
    for name, data, mime in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype,
                           subtype=subtype or "octet-stream", filename=name)
    # With the hosted policy on, connect to the address that PASSED the policy check,
    # not whatever the name resolves to a second time (DNS rebind). No-op
    # on self-host (returns the hostname unchanged).
    from .netguard import pinned_smtp_host
    connect_host = pinned_smtp_host(host)
    try:
        _smtp_deliver(msg, s, host, connect_host, port, user, password)
    except Exception as e:                                 # noqa: BLE001
        _record_outcome(False, subject, recipients, e)
        raise
    _record_outcome(True, subject, recipients, None)


def _record_outcome(ok: bool, subject: str, recipients: list[str],
                    err: Exception | None) -> None:
    # instance-wide "is mail working" bookkeeping — operator-facing, and
    # deliberately outside the send path's own error handling so a
    # bookkeeping hiccup can neither fail a send nor hide a failure
    try:
        from ..notify import mailhealth
        # the exception object, not its text: mailhealth keeps the class
        # name and scrubs the message (recipient dicts, relay replies)
        mailhealth.record(ok, kind=subject, to=recipients, error=err)
    except Exception:                                      # noqa: BLE001
        pass


_DATA_IMG = re.compile(
    r'src="data:image/(png|jpeg|gif);base64,([A-Za-z0-9+/=]+)"')


def inline_data_images(html: str) -> tuple[str, list[tuple[str, str, bytes]]]:
    """Move every embedded (data:) image out of the HTML into an inline part.

    Returns the HTML with each `src="data:…"` rewritten to `src="cid:…"`,
    and the (cid, subtype, bytes) parts to attach. Gmail and Outlook do not
    display data: images, so none may reach the wire; the cid is derived
    from the bytes, so the same image referenced twice travels once."""
    import base64
    import hashlib
    parts: dict[str, tuple[str, bytes]] = {}

    def _swap(m):
        data = base64.b64decode(m.group(2))
        cid = hashlib.sha256(data).hexdigest()[:24] + "@oikonome"
        parts.setdefault(cid, (m.group(1), data))
        return f'src="cid:{cid}"'

    out = _DATA_IMG.sub(_swap, html)
    return out, [(cid, sub, data) for cid, (sub, data) in parts.items()]


def _smtp_deliver(msg, s: dict, host: str, connect_host: str, port: int,
                  user, password) -> None:
    with smtplib.SMTP(connect_host, port, timeout=30) as smtp_conn:
        # smtplib verifies the STARTTLS certificate against the host it
        # connected to — restore the real name so pinning doesn't turn
        # verification into an IP mismatch
        smtp_conn._host = host
        if s["starttls"]:
            # smtplib's default context doesn't verify the
            # relay's certificate — a MITM could read the verdict and the
            # SMTP password. create_default_context verifies chain +
            # hostname. Self-signed relays the docs promise (Proton
            # Bridge on localhost, a home postfix) keep working with
            # OIKONOME_SMTP_NO_VERIFY=1: still encrypted, not verified.
            import ssl
            ctx = ssl.create_default_context()
            # env_flag, not `== "1"`: an operator who writes "true" means
            # the same thing
            if envnum.env_flag("OIKONOME_SMTP_NO_VERIFY"):
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            smtp_conn.starttls(context=ctx)
        if user and password:
            smtp_conn.login(user, password)
        smtp_conn.send_message(msg)
