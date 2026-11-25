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

import calendar
import datetime as dt
import logging
import os
import re
import smtplib
from email.message import EmailMessage

from .. import envnum
from ..envnum import env_flag
from ..engine import alerts, anomalies, bills, budget, forecast, merchant_sql, savings
from ..engine.categories import display_label
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


def _still_due_this_month(conn, cfg: dict, day: dt.date, *,
                          memo: dict | None = None) -> list[dict]:
    """The walk's bill occurrences that are still this month's money — the
    bills side of the "after cards & bills" tile (the paychecks side is the
    month plan's own still-due half, budget._month_income_parts).
    The walk re-dates an unpaid occurrence due today (a bill also when
    overdue) to at least tomorrow, so on the month's last day a horizon of
    month end would lose them all and the tile would read $0 with rent
    unpaid. Looking one day further also expands next month, so only a
    RE-DATED occurrence may cross: a bill or a pension genuinely due on the
    1st is next month's. The original date rides on occ["due"]; the walk
    re-dates a copy."""
    month_end = day.replace(day=calendar.monthrange(day.year, day.month)[1])
    occs = budget.upcoming_bill_occurrences(
        conn, cfg, day, max(month_end, day + dt.timedelta(days=1)),
        memo=memo)
    return [o for o in occs
            if o["due"] <= month_end or o["occ"]["due"] <= day]


def bills_remaining_month(conn, cfg: dict, day: dt.date,
                          env_states: list[dict] | None = None,
                          memo: dict | None = None) -> float:
    """Unpaid bill occurrences through month end — the Today plan card's
    "After cards & bills" tile, summed with the same occurrence helper the
    runway and forecast reserve with, cut at month end by
    _still_due_this_month.

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
    return month_end_parts(conn, cfg, day, env_states,
                           memo=memo)["total"]


def month_end_parts(conn, cfg: dict, day: dt.date,
                    env_states: list[dict] | None = None, *,
                    memo: dict | None = None) -> dict:
    """What the month still owes through its end, read from the walk ONCE
    (_still_due_this_month and envelope_reserves) and split for printing
    (specs/today-figures.md, "Envelopes are their own row"):

    - `total`: bills_remaining_month, the figure the cash tiles subtract,
      summed exactly as it always was (the occurrences, then each
      envelope's reserve in turn), to the cent;
    - `bills_cents` / `envelopes_cents`: its two halves in dollars to the
      cent — the bill occurrences (this month's and any late one the
      walk carries) and the envelopes' reserves;
    - `bills` / `envelopes`: those two in whole dollars, apportioned ONCE
      to `whole_dollars(total)`, so "bills $X + envelopes $Y" adds up to
      the total as printed — the Bills row's still-due figure and the
      Envelopes row's "still expected this month" print these two;
    - `occs`: the walk's occurrences (server-only: the Bills row's
      breakdown reads them rather than walking again) and `reserves`,
      each envelope's, in `env_states` order.

    `env_states` is month_status's envelope accounting for `day`; without
    it the month is read here."""
    occs = _still_due_this_month(conn, cfg, day, memo=memo)
    if env_states is None:
        env_states = budget.month_status(
            conn, day, memo=memo)["buckets"]["fixed"]["envelopes"]
    reserves = envelope_reserves(env_states, day)
    return dict(split_month_end([o["planned"] for o in occs], reserves),
                occs=occs, reserves=reserves)


def month_end_text(parts: dict) -> str | None:
    """The line beneath "left at month end" on Today (web and phone) and
    in the daily email: what the month still owes, in its two parts —
    "bills $X + envelopes $Y" — only when both are above $0."""
    if parts["bills"] > 0 and parts["envelopes"] > 0:
        return (f"bills {_money(parts['bills'])} + envelopes "
                f"{_money(parts['envelopes'])}")
    return None


def split_month_end(planned: list[float], reserves: list[float]) -> dict:
    """month_end_parts' arithmetic: the total summed as
    bills_remaining_month always summed it (the occurrences, then each
    reserve in turn, rounded to the cent once) and its two halves, in
    cents and in whole dollars apportioned once to the total's."""
    from ..engine.reporting import shown_dollars
    bills = sum(planned)
    total = bills
    for r in reserves:
        total += r
    total = round(total, 2)
    b_c, e_c = round(bills, 2), round(sum(reserves), 2)
    b_d, e_d = shown_dollars([b_c, e_c], total)
    return {"total": total, "bills_cents": b_c, "envelopes_cents": e_c,
            "bills": b_d, "envelopes": e_d}


def envelope_reserves(env_states: list[dict], day: dt.date) -> list[float]:
    """What the walk's drip would charge each envelope tomorrow→EOM, in
    `env_states` order: a monthly pool's max(0, pool − used) in full, an
    annual pool's rest-of-month share of its remaining YEAR pool."""
    nxt = (dt.date(day.year + 1, 1, 1) if day.month == 12
           else dt.date(day.year, day.month + 1, 1))
    days_left_month = (nxt - dt.timedelta(days=1) - day).days  # tomorrow..EOM
    days_left_year = (dt.date(day.year, 12, 31) - day).days
    out = []
    for e in env_states:
        if not days_left_month:
            out.append(0.0)
        elif e["period_months"] >= 12:              # annual pool
            out.append(e["period_left"] * days_left_month / days_left_year
                       if days_left_year else 0.0)
        else:
            out.append(max(0.0, e["pool"] - e["used"]))
    return out


def gather(conn, today: dt.date, *, live: bool = True,
           memo: dict | None = None) -> dict:
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
    # when this gather began reading the world: the alert snapshot it
    # writes at the end is only as new as its oldest read
    view_at = dt.datetime.now(dt.timezone.utc)
    # One gather is one snapshot: the month status, the forecast walk and
    # goal progress each derive the same sweep-goal "posted this month"
    # evidence, and each derivation is a pair of unindexable LIKE scans
    # over the whole ledger. They share this dict so the work happens
    # once; it dies with the call, so it can never serve a stale number.
    # A caller that reads the same books again in the same request (the
    # integrations summary asks for the week's bills straight after) may
    # pass its own request-scoped dict to share the loads.
    memo = {} if memo is None else memo
    # One config load per gather: month_status accepts the dict, and the
    # today_view read below reuses it — this runs on every Today request
    # and once per tenant per email sweep, so a second identical
    # tenant_settings round trip here would be pure waste.
    cfg = budget.load_config(conn, memo=memo)
    # A past day's month has closed only when it has closed WHERE THE
    # HOUSEHOLD LIVES. The process clock is UTC on a hosted instance, so
    # near a month boundary it can sit in the household's next month (an
    # earlier day of the current month would lose its bill schedule) or
    # its previous one (a closed month would get today's live schedule).
    historical = False
    if not live:
        from .. import localtime
        real_today = localtime.now_local(cfg).date()
        historical = ((today.year, today.month)
                      != (real_today.year, real_today.month))
    status = budget.month_status(conn, today, historical=historical,
                                 cfg=cfg, memo=memo)
    status["today"] = today
    # what the planner offers to save a month on the saved plan: an
    # ordinary month's, not this one's (forecast.plan_savings_suggestion).
    # A now-fact, so none on a past day.
    status["savings_suggested"] = (
        forecast.plan_savings_suggestion(conn, status, today, memo=memo,
                                         cfg=cfg) if live else None)
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
        {**dict(r), "payee": merchant_sql.shown_payee(r)}
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
    # how close the forecast has landed (forecast_score; failure-tolerant)
    try:
        from ..engine import forecast_score
        if live and status["forecast"]:
            # the day's night, kept here when the hourly step has not run
            # yet and the household's clock has passed the hour
            forecast_score.record_if_due(conn, today)
            status["forecast_score"] = forecast_score.current(conn, today)
        else:
            status["forecast_score"] = None
    except Exception:
        status["forecast_score"] = None
    # the walked checking account's own-account transfers lately, which
    # the forecast does not walk (todayview.transfers_note words them)
    try:
        acct = (forecast.checking_pick(conn, cfg)[0]
                if live and status["forecast"] else None)
        status["own_transfers"] = (
            dict(forecast_score.kept_own_transfers(conn, cfg, acct, today))
            if acct else None)
    except Exception:
        status["own_transfers"] = None
    # anomaly watch (a now-signal — quiet on a past-day view)
    try:
        status["anomalies"] = anomalies.detect(conn, today) if live else []
    except Exception:
        status["anomalies"] = []
    # recurring detection: pending-proposal count + auto-applied drifts
    try:
        status["recurring_pending"] = len(bills.pending_proposals(conn, today))
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
        checking_bal, _avail, checking_unset = forecast._checking_start(
            conn, cfg)
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
        # the card debt the cash card prints: posted balances and pending
        # charges, decided once (forecast.card_debt_total) — the forecast
        # built above already holds it; read again only without one
        card_debt = (status["forecast"]["card_debt"]
                     if status.get("forecast") else
                     forecast.card_debt_total(
                         conn, budget.excluded_account_ids(cfg)))
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
        # a business's goal is paid from its own checking, which this
        # household headroom does not start from (savings.household_goals)
        for g in _savings.household_goals(conn, cfg):
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
        # a checking row nobody has given a balance (every file import
        # creates one) is an unknown, not $0 — read as $0 every cash signal
        # screams "out of money". The start pick says so from the row
        # itself, whatever else the household has linked: a linked savings
        # account, or a linked checking it excluded, does not make the
        # imported row's balance real. Surfaces show "set your balance"
        # instead of the alarm. A manual-only install still at the $0 a
        # manual account is created with reads the same way while
        # transactions exist and nothing aggregator-fed backs it.
        balance_unreliable = bool(checking_unset)
        # no checking account at all (a card-only or savings-only
        # household) is not a $0 balance: checking stays None, so headroom
        # and every cash alarm go quiet, and the surfaces ask for the
        # account instead (`no_checking`)
        no_checking = checking_bal is None
        if checking_bal is not None and not checking_bal \
                and not balance_unreliable:
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
            "no_checking": no_checking,
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
        # What each goal received THIS month (savings.month_saved: the
        # plan evidence the sweep line reads too — the Month lens' own
        # figure). The money map's Savings row reads "transferred vs
        # plan" on a this-month card, so it shows this; rate_90d is a
        # trailing three-month average that can read "on plan" in a month
        # nothing moved, and stays for the goal's pace. None for a
        # plan-only goal, which has no ledger to measure.
        by_name = {str(g["name"]): g for g in savings.goals(cfg)}
        for g in status["savings_goals"]:
            src = by_name.get(str(g["name"]))
            g["saved_this_month"] = (
                None if src is None or savings.is_plan_only(src)
                else savings.month_saved(
                    conn, cfg, src, today.replace(day=1), today,
                    memo=memo))
    except Exception:
        status["savings_goals"] = []
    # The running month's bills in parts that add up to its plan
    # (bills_breakdown.running): one walk, one try, so the still-due
    # figure the cash tiles subtract and the parts the Bills row prints
    # cannot come from two reads — a failure leaves both None, never one
    # of them. A past day has neither (still due is a now-fact).
    # The month-end figure the cash tiles subtract and its two printed
    # halves (month_end_parts): the walk read once, a now-fact (None on a
    # past day, or when it cannot be read — then the tiles say nothing)
    try:
        parts = (month_end_parts(
            conn, cfg, today,
            env_states=status["buckets"]["fixed"]["envelopes"], memo=memo)
            if live else None)
    except Exception:
        parts = None
    status["bills_remaining_month"] = (parts["total"] if parts is not None
                                       else None)
    status["month_end_parts"] = (
        dict({k: parts[k] for k in ("total", "bills", "envelopes")},
             text=month_end_text(parts))
        if parts is not None else None)
    try:
        from . import bills_breakdown
        status["bills_breakdown"] = (
            bills_breakdown.running(conn, cfg, status, today, memo=memo,
                                    parts=parts)
            if parts is not None else None)
    except Exception:
        logging.getLogger("oikonome.report").exception(
            "bills breakdown failed")
        status["bills_breakdown"] = None
    # ...and the month's pools, the Envelopes row (envelope_breakdown):
    # what is set aside to month end is the envelopes half of the same
    # walk, so the row and the cash tile's "envelopes $Y" are one figure
    try:
        from . import envelope_breakdown
        status["envelopes_breakdown"] = envelope_breakdown.month(
            status["buckets"]["fixed"], kind="running", y=today.year,
            m=today.month,
            reserve=parts["envelopes"] if parts is not None else 0)
    except Exception:
        logging.getLogger("oikonome.report").exception(
            "envelopes breakdown failed")
        status["envelopes_breakdown"] = None
    # The paychecks still due this month — their dates and their sum —
    # are the month plan's own "still due" half
    # (budget._month_income_parts → expected): one figure for Budget's
    # "still due", Today's "paychecks still due" and the email. A check
    # posted pending counts as landed there, so it is not still due here.
    # None when the household tracks no income series: the tile then says
    # that no paycheck is set up — the month-end figure counts bills and
    # no income, and a reader who sees only the bills side thinks the
    # product forgot their pay. None too when not live (a past day has no
    # "still due").
    # `income_due_note` says WHY there is no paycheck figure, so the tile's
    # wording does not tell a household to set up what it already has:
    # "off" — every income series is switched off; "budgeted" — income is a
    # budgeted figure (a scenario or the monthly amount), not a schedule;
    # "none" — nothing at all. The figure itself stays cash either way: a
    # budgeted amount has no dates, so it is not a paycheck still due.
    # None whenever the figure exists (or not live). One predicate, here;
    # web, mobile and the email read the key and never re-derive it.
    status["income_due_dates"] = None
    status["income_due_total"] = None
    status["income_due_note"] = None
    try:
        if live:
            parts = budget._month_income_parts(conn, cfg, today,
                                               closed=False, memo=memo)
            if parts is None:
                scheduled = [b for b in budget._recurring_bills(
                                 conn, caps=cfg.get("occurrence_caps"),
                                 income=True, memo=memo)
                             if (b["recurrence"] or {}).get("frequency")]
                status["income_due_note"] = (
                    "off" if scheduled
                    else "budgeted" if budget.active_monthly_income(cfg)
                    else "none")
            else:
                status["income_due_dates"] = parts["expected_dates"]
                status["income_due_total"] = parts["expected"]
    except Exception:
        status["income_due_dates"] = None
        status["income_due_total"] = None
        status["income_due_note"] = None
    # The Cash timeline's month-end tiles, one figure each: checking less
    # card debt less the bills still due this month, and the same plus the
    # paychecks still due. A now-fact (None on a past day, or with no
    # checking balance to start from).
    status["after_cards_bills"] = None
    status["after_cards_bills_paychecks"] = None
    rw_ = status.get("runway") or {}
    if (rw_.get("checking") is not None
            and status.get("bills_remaining_month") is not None):
        after = rw_["checking"] - rw_["card_debt"] - status["bills_remaining_month"]
        status["after_cards_bills"] = round(after, 2)
        if status["income_due_total"] is not None:
            status["after_cards_bills_paychecks"] = round(
                after + status["income_due_total"], 2)
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
            # (only a live gather reaches here, so `today` is that day)
            alerts.log(conn, built, today, as_of=view_at)
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
    spent_yday = round(sum(r["amount"] for r in y_rest), 2)
    yday_label = "Recent transactions"

    if verdict == "ON BUDGET":
        v_detail = "variable spending on pace"
    elif verdict == "UNDER BUDGET":
        v_detail = f"{_money(-variance)} less than planned on variable spending"
    else:
        v_detail = f"{_money(variance)} more than planned on variable spending"

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
    # the hero's "$X of the month's $Y left": what is left of the month's
    # paced share (month_status); the words after it are hero_face's
    remaining = d.get("month_remaining_paced",
                      var_budget_total - d["variable_actual"])

    # HTML = the shared Today template, email mode. The context is built
    # ONCE and shared with the plain-text mirror below — it aggregates
    # the whole hero (allowances, bill cards, why chips, MTD rollups),
    # which is too much work to redo for a second surface of the same
    # send.
    from .todayview import (_moneyc, build_context,
                            render_email)
    ctx = build_context(d, email=True)
    html = render_email(d, summary=summary, ctx=ctx)

    # cash: quiet until it matters — the plain-text words for the notice
    # todayview decided (cash_notice); no threshold is judged here
    rw = d.get("runway") or {}
    fc = d.get("forecast")
    if rw.get("balance_unreliable") or rw.get("no_checking"):
        fc = None
    pc = fc["pace_usual"] if fc else None
    note = ctx["cash_notice"]
    cash_line = None
    # the two "we can't see your cash yet" nudges — the same sentences the
    # HTML card carries in place of a cash alarm
    if rw.get("balance_unreliable"):
        cash_line = ("Cash forecast is waiting on your balance — set "
                     "today's checking balance on the Accounts page.")
    elif rw.get("no_checking"):
        cash_line = ("No checking account linked — link your checking "
                     "account on the Accounts page to see cash ahead.")
    elif note["text"]:
        # the notice in the words every surface prints
        # (todayview.cash_sentence); a critical one keeps its alarm mark
        cash_line = (("!! CASH: " if note["state"] == "critical"
                      and not note["ask"] else "") + note["text"])
    elif ctx.get("cash_band"):
        # the quiet heavier-month line (todayview.cash_band)
        cash_line = ctx["cash_band"]["text"]

    summaries = d.get("amazon_summaries") or {}

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
             # the folded row's words (MIRRORS today_body.html)
             else f"{label} $0 today" if a.get("zero_day")
             else f"{label} ${a['left_dollars']:,} "
                  f"of ${a['today_allowance_dollars']:,}")
            + (f" ({a['daily_note']}"
               + (f"; {a['recover_note']}" if a.get("recover_note") else "")
               + ")" if a.get("daily_note") else "")
            for label, a in day_ctx["allow_tiles"])
    plain += [f"{state} — {day_ctx['pace_line']} · "
              f"{_money(abs(remaining))} {day_ctx['month_words']}",
              f">> {day_ctx['left_label'].upper()}: {tile_line} <<"]
    # pinned bills (bill setting "pin to Today") — the same server-composed
    # card sentences the page and the HTML render
    if day_ctx["bill_cards"]:
        card_line = " · ".join(
            f"{c['label']} {_money(c['left'])} "
            f"{'left' if c['kind'] == 'envelope' else 'to go'} "
            f"({c['sub']}{'; ' + c['status'] if c['status'] else ''})"
            for c in day_ctx["bill_cards"])
        plain += [f"Pinned: {card_line}"]
    # the HTML's auto-scale sentence, where the HTML puts it
    if d.get("variable_scale"):
        plain += ["Variable budgets auto-scaled from "
                  f"{_money(d['variable_scale']['static_total'])} to fit "
                  "this month's bills."]
    if cash_line:
        plain += [cash_line]
    # the WHY, over-budget only — a good day is quiet: merchant chips as
    # one line, then the per-category
    # sentences, all server-composed alongside the other two surfaces.
    w = d.get("why") or {}
    if day_ctx["why_chips"]:
        chip_txt = ", ".join(
            f"{c['payee'][:26]} {_money(c['amount'])}"
            + (f" ({c['count']}×)" if c["count"] > 1 else "")
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
    # the terms print in the whole dollars budget.plan_line rounded to the
    # printed excess, so the chain adds up as printed; without an excess
    # (no income) there is nothing to add up to and each prints its own
    # the plan line in the terms budget.plan_line rounded to the printed
    # excess, as the HTML prints it — and, like the HTML, only when there
    # is a plan line (no income yet: no line)
    pl = d.get("plan_line")
    if pl and view == "detail":
        # this month's schedule against the average, in the words the
        # page and the HTML print beside the bills (todayview.bills_weight)
        weight_txt = (f" ({day_ctx['bills_weight']})"
                      if day_ctx.get("bills_weight") else "")
        plan_flow = (f"Plan: {_money(pl['income'])} in -> "
                     f"{_money(pl['bills'])} "
                     f"{day_ctx.get('plan_bills_label') or 'bills'}"
                     f"{weight_txt} -> "
                     f"{_money(pl['to_spend'])} to spend")
        if pl["saved"] is not None:
            plan_flow += f" -> {_money(pl['saved'])} saved"
        # the excess in the Budget summary's words (plan_excess_words)
        ex_words = (day_ctx.get("plan_excess_words") or {}).get("text")
        plan_flow += f" -> {_money(pl['excess'])} {ex_words or 'excess'}"
        # the sweep goals' line — the SAME server-composed sentence the
        # page and the HTML render, beside the chain (a sweep takes from
        # the excess only when the month provides it, so it is not a link)
        if day_ctx.get("sweep_line"):
            plan_flow += f" · {day_ctx['sweep_line']}"
        plain += ["", plan_flow]
    # the summary email ends with the verdict pane — everything the HTML's
    # hero card carries is now said; the map/timeline/goals sections below
    # belong to the extensive version only
    if summary:
        plain += ["", "(summary email — open Oikonome for the full report)"]
        return subject, "\n".join(plain), html
    # savings goals — trackable ones, and the ones that need an account to
    # become trackable (their line is the prompt to pick it; any other
    # plan-only row has nothing to report), and the advanced face only: the
    # simple face tracks goals on the Budget page, matching the HTML and the
    # Today page
    goals = ([] if view != "detail" else
             [g for g in d.get("savings_goals") or []
              if not g.get("plan_only") or g.get("needs_account")])
    if goals:
        plain += ["", "Savings goals:"]
        for g in goals:
            if g.get("plan_only"):
                plain += [f"  {g['name']}: match words need an account — "
                          "pick one on the Budget page"]
                continue
            # withdrawals outweighing deposits is not the same news as
            # nothing arriving (mirrors Today.tsx)
            nothing = ("no net saving in the last 3 months"
                       if g.get("contributed_90d")
                       else "no contributions in 3 months")
            # open-ended goal (no target): "$X saved" + what the last 3
            # months saved — never "of $0" (mirrors Today.tsx)
            if not g.get("target"):
                pace = (f"{_money(g['saved_90d'])} in the last 3 months"
                        if (g.get("saved_90d") or 0) > 0 else nothing)
                plain += [f"  {g['name']}: {_money(g.get('saved') or 0)} "
                          f"saved — {pace}"]
                continue
            pace = ("funded" if (g.get("pct") or 0) >= 100
                    else "off pace" if g.get("on_pace") is False
                    else "on pace" if g.get("on_pace")
                    else (f"~{_d(g['eta'])} at the current rate"
                          if g.get("eta") else nothing))
            plain += [f"  {g['name']}: {_money(g.get('saved') or 0)} of "
                      f"{_money(g.get('target') or 0)}"
                      + (f" ({g['pct']}%)" if g.get("pct") is not None else "")
                      + f" — {pace}"]
    # the money map — the HTML's Monthly budget card, row for row: the
    # same rows (todayview.money_map), the same words beside each
    # (row_status, on Today's pace basis), the same captions and footer
    # lines, so the plain text says what the page and the HTML say
    mp = ctx["map"]
    plain += ["", "Monthly budget:"]
    for r in mp["rows"]:
        pad = "    " if r["indent"] else "  "
        figs = (f"{r.get('actual_label') or _money(r['actual'])} / "
                f"{'—' if r['unbudgeted'] else _money(r['budget'])}")
        plain.append(f"{pad}{r['label']} {figs}"
                     + (f" — {r['delta']['text']}" if r["delta"] else ""))
        if r["unbudgeted"]:
            plain.append(f"{pad}  no budget set — set one on the Budget "
                         "page so this counts toward the verdict")
        elif r.get("caption"):
            plain.append(f"{pad}  {r['caption']}")
    if mp["folded"]:
        plain += ["  Also this month: " + " · ".join(mp["folded"])]
    if d.get("irregular_bills"):
        plain += ["  Non-monthly bills this month: " + " · ".join(
            f"{i['payee']} {_money(i['amount'])} ({i['label']})"
            for i in d["irregular_bills"][:4])]
    # a charge the bank feed withdrew is not a bill that never posted —
    # it gets its own line, as on the Today page
    late = [o for o in budget.overdue_entries(d) if not o[3]]
    gone = [o for o in budget.overdue_entries(d) if o[3]]
    if late:
        plain += ["  Scheduled but not yet posted: "
                  + ", ".join(f"{p} {_money(a)} (due {_d(due)})"
                              for p, a, due, _ in late[:4])]
    if gone:
        plain += ["  withdrawn by the bank feed: "
                  + ", ".join(f"{p} {_money(a)} ({_d(on)})"
                              for p, a, _, on in gone[:4])]
    # cash timeline header — cash's one home (checking, cards, after
    # everything, and the forecast low), one line
    if rw.get("checking") is not None:
        # the card debt the HTML's tile prints: the forecast's (pending
        # charges included), the runway's posted figure only without one
        debt = fc["card_debt"] if fc else rw["card_debt"]
        cash_now = (f"Cash timeline: checking {_money(rw['checking'])} · "
                    f"card debt {_money(debt)}")
        if d.get("income_due_total") is not None:
            dates = d.get("income_due_dates") or []
            when = (" (" + ", ".join(_d(x) for x in dates[:3]) + ")"
                    if dates else "")
            cash_now += (f" · paychecks still due "
                         f"+{_money(d['income_due_total'])}{when}")
        # the balance the timeline ends the month on, and why
        # (todayview.month_end_left), as the HTML's tile says it
        if ctx["month_end"]:
            cash_now += (f" · {ctx['month_end_label']} "
                         f"{_money(ctx['month_end']['balance'])} "
                         f"({ctx['month_end_note']})")
            # what the cards still hold then, as the HTML
            if ctx["month_end_cards_note"]:
                cash_now += f" · {ctx['month_end_cards_note']}"
        # each card scenario's low point, in the HTML's words
        if pc and ctx["fc_lows"]:
            cash_now += " · low: " + "; ".join(
                f"{lo['label'].lower()} "
                # a low that ends its own sentence gives up its period to
                # the cards' clause
                + (f"{lo['words'].rstrip('.')}, {lo['cards_note']}"
                   if lo.get("cards_note") else lo["words"])
                for lo in ctx["fc_lows"])
        plain += ["", cash_now]
        if fc and ctx["pace_note"]:
            plain += [f"  {ctx['pace_note']}"]
        # the forecast's track record, as the HTML prints it
        sc = ctx.get("forecast_score") or {}
        for line in (sc.get("text"), sc.get("bias_text")):
            if fc and line:
                plain += [f"  {line}"]
        if fc and ctx["card_timing_note"]:
            plain += [f"  {ctx['card_timing_note']}"]
        # a paycheck left out because it stopped arriving, as the HTML
        if fc and ctx.get("stopped_paychecks_note"):
            plain += [f"  {ctx['stopped_paychecks_note']}"]
        # own-account transfers the forecast leaves out, as the HTML
        if fc and ctx.get("transfers_note"):
            plain += [f"  {ctx['transfers_note']}"]
        if fc and ctx["scenarios"] and ctx["scenarios"][0]["cards_note"]:
            plain += [f"  {ctx['scenarios'][0]['cards_note']}"]
    # the recent rows in the page's order (build_context sorts them)
    recent = day_ctx["recent_var"]
    plain += ["", f"{yday_label} ({_money(day_ctx['recent_var_total'])} "
                  "variable):"]
    if recent:
        # cents on transaction rows, like the site's tables
        plain += [f"    {_moneyc(r['amount']):>10}  "
                  f"{r['payee'] + ' — ' + summaries[r['txn_id']] if r['txn_id'] in summaries else r['payee']}"
                  + (" (pend)" if r.get("pending") else "")
                  + f"  [{display_label(r['category'])}]" for r in recent]
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


def relay_user(conn, cfg: dict) -> str:
    """The relay login as stored settings hold it, decrypted. It is stored
    encrypted like the password beside it: Postmark takes the server API
    token as the login. An untagged value (an older build's, or a box
    without a master key) passes through; one that will not decrypt (the
    key it was sealed under is gone) reads as no login, so the relay
    refuses the send where the mail health probe sees it, and no settings
    save stops on it."""
    from ..db import crypto
    try:
        return crypto.decrypt(conn, cfg.get("smtp_user") or None) or ""
    except Exception:                                    # noqa: BLE001
        import logging
        logging.getLogger("oikonome.email").warning(
            "the stored mail relay login does not decrypt; read as none")
        return ""


def seal_relay_user(conn, stored, posted) -> str:
    """What to store for a posted relay login: blank stays blank, the
    stored ciphertext when it already holds this login (a re-save of the
    whole settings document is no change), otherwise the login encrypted
    under the household's key."""
    from ..db import crypto
    posted = str(posted or "")
    if not posted:
        return ""
    try:
        if stored and crypto.decrypt(conn, stored) == posted:
            return stored
    except Exception:                                    # noqa: BLE001
        pass                     # unreadable: replaced by what was posted
    return crypto.encrypt(conn, posted)


def _user_as_sender(user) -> str | None:
    from .mailguard import valid_address
    return user if valid_address(user) else None


def _env_smtp() -> dict:
    host = os.environ.get("OIKONOME_SMTP_HOST", "")
    return {"host": host or "localhost",
            # a hand-edited port that is not a number ("587/tcp") would
            # raise on every mail path, the mail-health probe included;
            # env_int names it in the log and keeps the default
            "port": envnum.env_int("OIKONOME_SMTP_PORT", 587,
                                   lo=1, hi=65535),
            "user": os.environ.get("OIKONOME_SMTP_USER"),
            "password": os.environ.get("OIKONOME_SMTP_PASSWORD"),
            "sender": os.environ.get("OIKONOME_SMTP_FROM",
                                     "oikonome@localhost"),
            "starttls": _flag_on_unless_disabled("OIKONOME_SMTP_STARTTLS"),
            "no_verify": envnum.env_flag("OIKONOME_SMTP_NO_VERIFY"),
            "configured": bool(host)}


def tls_unverified(s: dict) -> bool:
    """Should STARTTLS to this relay skip certificate verification?

    A relay dict says for itself (`no_verify`); resolve_smtp decides it.
    The env relay reads OIKONOME_SMTP_NO_VERIFY. A relay saved in Settings
    reads it too on a self-hosted instance, but only when that relay sits
    on the operator's own network (`_relay_on_lan`): a self-signed LAN
    relay (a mail bridge) configured through the wizard has no other way
    to send short of turning STARTTLS off and sending the password in the
    clear. A Settings relay on a public host keeps its certificate check
    whatever the flag says — the flag was set for the operator's bridge,
    not for whatever relay another household, or a later edit, names.
    Hosted never lets it reach a household's relay. A dict that does not
    say (a caller building one by hand) keeps reading the env flag, which
    is what it always meant."""
    flag = s.get("no_verify")
    if flag is None:
        return envnum.env_flag("OIKONOME_SMTP_NO_VERIFY")
    return bool(flag)


def _lan_nets():
    import ipaddress
    return tuple(ipaddress.ip_network(n) for n in (
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7"))


_LAN_NETS = _lan_nets()


def _relay_on_lan(host: str) -> bool:
    """Is this relay host one only the operator's own network can answer
    for — loopback, a private/link-local address, or a bare hostname with
    no dot (resolved by the LAN's own DNS or hosts file)? A literal is
    judged as written and a dotted name is never resolved to decide: DNS
    for a public name is not the operator's to vouch for."""
    import ipaddress
    h = str(host or "").strip().rstrip(".").strip("[]")
    if not h:
        return False
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return "." not in h and all(c.isalnum() or c == "-" for c in h)
    if getattr(ip, "ipv4_mapped", None):
        ip = ip.ipv4_mapped
    # named ranges, not `is_private`: that also covers the documentation
    # and benchmark blocks, which are nobody's LAN
    return (ip.is_loopback or ip.is_link_local
            or any(ip in n for n in _LAN_NETS if n.version == ip.version))


def _stored_port(raw) -> int:
    """The saved relay port, or 587 when the stored value is not a usable
    one. Settings checks the port, but a restored archive or an imported
    bundle can carry any string, and every send resolves the relay here:
    a value that raised would fail every mail, every cadence, every hour."""
    try:
        port = int(raw or 587)
    except (TypeError, ValueError, OverflowError):
        port = 0
    if not 1 <= port <= 65535:
        import logging
        logging.getLogger("oikonome.email").warning(
            "stored SMTP port is not a port number; using 587")
        return 587
    return port


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
    from ..notify import outbound_pause
    _p = outbound_pause.paused(cfg)
    if cfg.get("smtp_host") and _p and _p["smtp"]:
        # paused by the owner's credential rotation until they confirm
        # the relay is theirs — the operator's relay (or a logged link)
        # carries the mail meanwhile, never a relay a hijacker may have
        # pointed
        return _env_smtp()
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
                "port": _stored_port(cfg.get("smtp_port")),
                "user": relay_user(conn, cfg) or None,
                "password": crypto.decrypt(conn, cfg.get("smtp_password"))
                            if cfg.get("smtp_password") else None,
                # a relay saved before the settings door filled an empty
                # From still sends as its own account, as the door now saves
                "sender": cfg.get("smtp_from") or _user_as_sender(
                    relay_user(conn, cfg)) or "oikonome@localhost",
                "starttls": bool(cfg.get("smtp_starttls", True)),
                # self-host: the operator's env escape hatch reaches a
                # relay saved here only when it is on their own network
                # (see tls_unverified). Hosted returned above, and is
                # excluded again here so the flag can never reach a
                # household's relay whatever reorders this function.
                "no_verify": (envnum.env_flag("OIKONOME_SMTP_NO_VERIFY")
                              and not env_flag("OIKONOME_HOSTED")
                              and _relay_on_lan(cfg["smtp_host"])),
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
    # smtplib takes the envelope from the parsed To/Bcc headers, so one
    # "address" carrying a comma is delivered to two mailboxes. Every door
    # that stores an address checks this too; the send path is the last
    # place it can be refused, whatever wrote the value.
    # A display form (`Name <a@x>`, stored before the rule) is one mailbox
    # and is unwrapped. An entry that is several mailboxes, or none, is
    # dropped here and logged; the rest of the message still goes. It is
    # NOT split: a login email that reads as two addresses is exactly the
    # one that would reach a mailbox nobody verified. Lists that really are
    # lists (the operator variable, the household recipients) are split at
    # the door that reads them. Nothing left → refuse before connecting.
    from .mailguard import split_mailboxes
    kept, dropped = [], []
    for r in recipients or []:
        good, bad = split_mailboxes([r])
        if len(good) == 1 and not bad:
            if good[0].lower() not in {k.lower() for k in kept}:
                kept.append(good[0])
        else:
            dropped.append(r)
    if dropped:
        log.warning("email: %d of %d recipient%s not one email address, "
                    "skipped: %s", len(dropped), len(recipients),
                    "" if len(recipients) == 1 else "s",
                    ", ".join(str(d)[:80] for d in dropped),
                    extra={"no_capture": True})
    if not kept:
        raise ValueError("not a single email address: "
                         f"{len(dropped)} of {len(recipients)} recipients")
    recipients = kept
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


# The submission port that speaks TLS from the first byte (RFC 8314). A
# relay there waits for a ClientHello and never sends the plaintext greeting
# smtplib.SMTP waits for, so every send would time out.
IMPLICIT_TLS_PORT = 465


class _PinnedSMTPSSL(smtplib.SMTP_SSL):
    """SMTP over implicit TLS to a pinned address, verifying the
    certificate (and sending SNI) for the relay's real name rather than the
    address connected to."""

    def __init__(self, server_name: str, *a, **kw):
        self._server_name = server_name
        super().__init__(*a, **kw)

    def _get_socket(self, host, port, timeout):
        import socket
        sock = socket.create_connection((host, port), timeout,
                                        self.source_address)
        return self.context.wrap_socket(sock,
                                        server_hostname=self._server_name)


def _smtp_tls_context(s: dict):
    # smtplib's default context doesn't verify the relay's certificate — a
    # MITM could read the verdict and the SMTP password.
    # create_default_context verifies chain + hostname. Self-signed relays
    # the docs promise (Proton Bridge on localhost, a home postfix) keep
    # working with OIKONOME_SMTP_NO_VERIFY=1: still encrypted, not verified.
    import ssl
    ctx = ssl.create_default_context()
    # only where the flag is allowed to reach — see tls_unverified
    if tls_unverified(s):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


# Refusals smtplib raises from send_message before the relay took the
# message: nothing was delivered, so a caller may safely try another mail.
# Any other failure inside send_message (a timeout or a dropped line during
# DATA) leaves it unknown whether the relay already accepted it.
_REFUSED_BEFORE_DELIVERY = (smtplib.SMTPSenderRefused,
                            smtplib.SMTPRecipientsRefused,
                            smtplib.SMTPDataError)


def _smtp_deliver(msg, s: dict, host: str, connect_host: str, port: int,
                  user, password) -> None:
    """Hand `msg` to the relay. Raises only when it was not delivered.

    An exception that may have followed delivery carries
    `maybe_delivered = True`, so a caller deciding whether to send a second
    mail in its place does not send the same news twice."""
    implicit = int(port) == IMPLICIT_TLS_PORT
    opener = (_PinnedSMTPSSL(host, connect_host, port, timeout=30,
                             context=_smtp_tls_context(s)) if implicit
              else smtplib.SMTP(connect_host, port, timeout=30))
    delivered = False
    try:
        with opener as smtp_conn:
            # smtplib verifies the STARTTLS certificate against the host it
            # connected to — restore the real name so pinning doesn't turn
            # verification into an IP mismatch
            smtp_conn._host = host
            if s["starttls"] and not implicit:
                smtp_conn.starttls(context=_smtp_tls_context(s))
            if user and password:
                smtp_conn.login(user, password)
            try:
                smtp_conn.send_message(msg)
            except _REFUSED_BEFORE_DELIVERY:
                raise
            except Exception as e:                         # noqa: BLE001
                e.maybe_delivered = True
                raise
            delivered = True
    except Exception:
        # Once send_message has returned the relay owns the message; a bad
        # reply to the QUIT that closing the session sends (smtplib raises
        # it from __exit__) does not undo that, and reporting it as a
        # failure would make callers retry or send a fallback mail.
        if not delivered:
            raise
        log.debug("SMTP session to %s ended badly after the message was "
                  "accepted", host, exc_info=True)
