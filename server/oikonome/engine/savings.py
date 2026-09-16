"""Savings goals: ledger-verified progress toward named funds
(trip fund, emergency fund) — spec `specs/income-scenarios-goals.md`.

A goal matches TRANSFER rows on its destination account (optional token
carve-outs so several goals share one savings account), plus an optional
starting balance. A 'monthly'-mode plan reduces plan surplus and lands in
the cash forecast as a scheduled outflow; a 'sweep'-mode plan contributes
only at month end and only what the month's realized surplus covers, so
it never reduces the plan surplus and never walks money out of the
forecast that the month's numbers say cannot leave. The daily verdict
never sees goals (it stays variable-spend-only). Milestone emails are
edge-triggered
(25/50/75/100% and falling off pace for a dated goal), state kept in
tenant config `savings_goal_state`."""

from __future__ import annotations

import datetime as dt

from .. import localtime

MILESTONES = (25, 50, 75, 100)


def goals(cfg: dict) -> list[dict]:
    out = []
    for g in cfg.get("savings_goals") or []:
        if isinstance(g, dict) and str(g.get("name") or "").strip():
            out.append(g)
    return out


def goal_mode(g: dict) -> str:
    """'monthly' (the default: the plan is a fixed commitment, walked out
    of the forecast like a bill) or 'sweep' (contribute at month end, but
    only what the month's own surplus covers — min(plan, realized
    surplus), nothing in a month that didn't work out). Any unknown or
    absent value reads as 'monthly' so old configs behave unchanged."""
    return ("sweep" if str(g.get("mode") or "").strip().lower() == "sweep"
            else "monthly")


def monthly_plan_total(cfg: dict, mode: str | None = None) -> float:
    """Sum of monthly plans, optionally restricted to one contribution
    mode — the plan surplus subtracts only the fixed ('monthly')
    commitments, while the sweep line reports the 'sweep' total."""
    return round(sum(float(g.get("monthly_plan") or 0) for g in goals(cfg)
                     if mode is None or goal_mode(g) == mode), 2)


def is_plan_only(g: dict) -> bool:
    """A goal with no destination account and no match tokens has
    nothing to measure progress against — `_matched_net` unscoped would sum
    EVERY transfer on the tenant (dashboard-wide phantom progress). The
    balance planner saves exactly this shape ({name, monthly_plan}), so it's
    common, not an edge: the plan reduces surplus and rides the forecast;
    progress stays null until the user scopes it."""
    return not g.get("account_id") and not any(
        str(t).strip() for t in (g.get("tokens") or []))


def _like_token(tok: str) -> str:
    """A user token as a LIKE pattern, with the wildcards neutralised.

    A goal token is a literal fragment of a merchant/description, but `%`
    and `_` are LIKE wildcards: an unescaped token like `100%` matches every
    transfer whose text merely starts `100`, and a single `_` matches
    anything. That silently inflates both the destination-side net and the
    checking-side inference the sweep math runs on. Every call site pairs
    this with an explicit `ESCAPE '\\'` clause so the pattern's meaning does
    not depend on a server setting."""
    esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def _matched_net(conn, g: dict, since: dt.date | None = None,
                 until: dt.date | None = None) -> float:
    """Net contributions for one goal: money INTO the account (amount<0 on
    the destination side, Plaid signs) minus money back out, TRANSFER rows
    only (interest/dividends aren't contributions), token-filtered.
    `since`/`until` bound the window inclusively — the Year lens
    measures one calendar year's contributions, not all-time.

    Linked shadow accounts are excluded like every other
    money aggregate — a token-matched goal over a dual-sourced savings
    account would otherwise count each transfer once per linked source."""
    from . import links
    where = ["t.removed = 0",
             """COALESCE(t.category_override, t.category_primary, '')
                IN ('TRANSFER_IN','TRANSFER_OUT')"""]
    params: list = []
    shadows = links.shadow_ids(conn)
    if shadows:
        where.append("NOT (t.account_id = ANY(%s))")
        params.append(shadows)
    if g.get("account_id"):
        where.append("t.account_id = %s")
        params.append(g["account_id"])
    for tok in (g.get("tokens") or []):
        tok = str(tok).strip().lower()
        if tok:
            where.append(
                "LOWER(COALESCE(t.merchant_name,'') || ' ' || "
                "COALESCE(t.name,'')) LIKE %s ESCAPE '\\'")
            params.append(_like_token(tok))
    if since is not None:
        where.append("t.date >= %s")
        params.append(since)
    if until is not None:
        where.append("t.date <= %s")
        params.append(until)
    from . import budget as _b   # late: avoid a circular import at load
    row = conn.execute(
        f"SELECT COALESCE(SUM(-t.amount), 0) AS net FROM transactions t "
        f"WHERE {' AND '.join(where)}" + _b.PERSONAL_ONLY_SQL,
        params).fetchone()
    return float(row["net"])


def matched_net(conn, cfg: dict, g: dict, since: dt.date | None = None,
                until: dt.date | None = None) -> float:
    """sibling-aware `_matched_net`. A token-scoped goal keeps
    its own carve-out. A token-LESS goal sharing its destination account
    with other goals would otherwise claim the account's ENTIRE transfer
    net — every sibling counting the same dollars, MTD savings N×. The
    account's un-tokened remainder (total minus tokened siblings' nets)
    goes to the FIRST token-less goal in config order; later token-less
    siblings read 0 (first-claim partition — deterministic, never
    double-counts; give later goals tokens to scope them properly)."""
    acc = g.get("account_id")
    has_tokens = any(str(t).strip() for t in (g.get("tokens") or []))
    if not acc or has_tokens:
        return _matched_net(conn, g, since=since, until=until)
    tokenless = [s for s in goals(cfg)
                 if s.get("account_id") == acc
                 and not any(str(t).strip() for t in (s.get("tokens") or []))]
    tokened = [s for s in goals(cfg)
               if s.get("account_id") == acc
               and any(str(t).strip() for t in (s.get("tokens") or []))]
    if len(tokenless) <= 1 and not tokened:
        return _matched_net(conn, g, since=since, until=until)
    if tokenless and str(tokenless[0].get("name")) != str(g.get("name")):
        return 0.0                       # a sibling already claimed it
    total = _matched_net(conn, {"account_id": acc}, since=since, until=until)
    carved = sum(_matched_net(conn, s, since=since, until=until)
                 for s in tokened)
    return round(total - carved, 2)


def _checking_side_rows(conn, g: dict, plan: float,
                        since: dt.date | None = None,
                        until: dt.date | None = None) -> list[tuple[str, float]]:
    """the checking legs of a goal contribution, row by row —
    TRANSFER_OUT on any non-credit account OTHER than the destination,
    token-matched when the goal has tokens, else amount-matched to the
    monthly plan to the cent (the only signal a token-less goal offers).

    Rows, not a sum, because siblings sharing a destination account have to
    be told apart by row identity: an amount-match and a token-match can
    select the SAME transfer, and netting sums instead would either
    double-count it or subtract a sibling's unrelated transfer."""
    from . import links
    where = ["t.removed = 0", "t.amount > 0",
             """COALESCE(t.category_override, t.category_primary, '')
                = 'TRANSFER_OUT'""",
             "COALESCE(a.type, '') != 'credit'"]
    params: list = []
    shadows = links.shadow_ids(conn)
    if shadows:
        where.append("NOT (t.account_id = ANY(%s))")
        params.append(shadows)
    if g.get("account_id"):
        where.append("t.account_id != %s")
        params.append(g["account_id"])
    tokens = [str(t).strip().lower() for t in (g.get("tokens") or [])
              if str(t).strip()]
    for tok in tokens:
        where.append(
            "LOWER(COALESCE(t.merchant_name,'') || ' ' || "
            "COALESCE(t.name,'')) LIKE %s ESCAPE '\\'")
        params.append(_like_token(tok))
    if not tokens:
        where.append("ABS(t.amount - %s) <= 0.01")
        params.append(plan)
    if since is not None:
        where.append("t.date >= %s")
        params.append(since)
    if until is not None:
        where.append("t.date <= %s")
        params.append(until)
    from . import budget as _b   # late: avoid a circular import at load
    rows = conn.execute(
        f"SELECT t.id AS id, t.amount AS amount FROM transactions t "
        f"LEFT JOIN accounts a ON a.id = t.account_id "
        f"WHERE {' AND '.join(where)}" + _b.PERSONAL_ONLY_SQL,
        params).fetchall()
    return [(str(r["id"]), float(r["amount"])) for r in rows]


def checking_side_out(conn, cfg: dict, g: dict, plan: float,
                      since: dt.date | None = None,
                      until: dt.date | None = None) -> float:
    """sibling-aware checking-leg evidence, summed.

    The checking leg is INFERENCE, not ledger truth, and a token-less goal
    offers nothing to infer from but "an outflow equal to my plan, to the
    cent" — a signal that cannot tell siblings apart. One $500
    checking→savings move matches EVERY token-less goal on the account, so
    each would count the same dollars: the Today line and its email mirror
    announce "swept $1,000 to savings this month" for a single $500
    transfer, and a monthly-mode sibling that received nothing reads as
    funded, dropping a real future outflow out of headroom and the forecast.

    So the inference follows the same first-claim partition the destination
    side uses in `matched_net`: the FIRST token-less goal on an account may
    infer from the checking leg, later token-less siblings read 0 until they
    are given tokens to scope them. Rows a TOKENED sibling already claims
    are removed by row id, so the tokened goal's own transfer cannot be
    inferred a second time by an amount-matching neighbour."""
    acc = g.get("account_id")
    has_tokens = any(str(t).strip() for t in (g.get("tokens") or []))
    if not acc or has_tokens:
        return round(sum(a for _, a in _checking_side_rows(
            conn, g, plan, since, until)), 2)
    siblings = [s for s in goals(cfg) if s.get("account_id") == acc]
    tokenless = [s for s in siblings
                 if not any(str(t).strip() for t in (s.get("tokens") or []))]
    tokened = [s for s in siblings
               if any(str(t).strip() for t in (s.get("tokens") or []))]
    if len(tokenless) > 1 and str(tokenless[0].get("name")) != str(g.get("name")):
        return 0.0                       # a sibling already claimed it
    rows = _checking_side_rows(conn, g, plan, since, until)
    claimed: set[str] = set()
    for s in tokened:
        claimed.update(i for i, _ in _checking_side_rows(
            conn, s, float(s.get("monthly_plan") or 0), since, until))
    return round(sum(a for i, a in rows if i not in claimed), 2)


def posted_for_plan(conn, cfg: dict, g: dict, since: dt.date,
                    until: dt.date | None = None,
                    memo: dict | None = None) -> float:
    """evidence this month's planned contribution happened.
    Destination-side net is the primary signal, but a savings account with
    no rows yet (external, unsynced, or the aggregator only carries the
    checking side) leaves it 0 while the checking TRANSFER_OUT already
    left the live balance — the forecast walk then schedules the plan
    AGAIN and the cash graph counts the money zero times. The checking leg
    counts as fallback evidence, capped at the plan (destination rows are
    ledger truth; the checking side is inference and may not exceed what
    the plan predicts). max — never both — so two posted legs of one
    transfer still count once.

    `memo`: an optional dict scoped to ONE request. Today (and the daily
    email it mirrors) derives this same number in three engine modules —
    the month status, the forecast walk and goal progress — over the same
    snapshot, and each derivation is a pair of unindexable LIKE scans over
    the whole ledger. The caller that owns the request creates the dict and
    passes it to all three; nothing caches across requests, so a memo can
    never serve a number the ledger has since moved past."""
    plan = float(g.get("monthly_plan") or 0)
    key = (str(g.get("name")), g.get("account_id"),
           tuple(str(t) for t in (g.get("tokens") or [])), plan, since, until)
    if memo is not None and key in memo:
        return memo[key]
    dest = matched_net(conn, cfg, g, since=since, until=until)
    if plan <= 0 or dest >= plan:
        out = dest
    else:
        evidence = min(plan, checking_side_out(conn, cfg, g, plan,
                                               since, until))
        out = max(dest, evidence)
    if memo is not None:
        memo[key] = out
    return out


def progress(conn, cfg: dict, today: dt.date | None = None,
             memo: dict | None = None) -> list[dict]:
    """Per-goal progress + pace. Pace projects from the trailing 3 months
    of actual contributions; with a target_date it reports on/off pace,
    without one it estimates the arrival month. `memo` is the optional
    request-scoped cache described on `posted_for_plan`."""
    # "this month" and the pace window are calendar facts about where the
    # household lives, and cfg carries its zone — the container's own day
    # is often UTC on a server and flips a month early for half the world
    today = today or localtime.now_local(cfg).date()
    month_start = today.replace(day=1)
    out = []
    for g in goals(cfg):
        mode = goal_mode(g)
        if is_plan_only(g):
            out.append({
                "name": str(g["name"]),
                "target": round(float(g.get("target") or 0), 2),
                "target_date": g.get("target_date") or None,
                "monthly_plan": round(float(g.get("monthly_plan") or 0), 2),
                "saved": None, "rate_90d": None, "pct": None,
                "eta": None, "on_pace": None, "plan_only": True,
                "mode": mode, "swept_this_month": None,
                "sweep_satisfied": None,
            })
            continue
        saved = round(float(g.get("start_balance") or 0)
                      + matched_net(conn, cfg, g), 2)
        rate = round(matched_net(conn, cfg, g,
                                 since=today - dt.timedelta(days=90))
                     / 3.0, 2)
        target = float(g.get("target") or 0)
        remaining = max(0.0, target - saved)
        eta = None
        if target <= 0:
            pass                       # open-ended: no pct/ETA until a target
        elif remaining <= 0:
            eta = today.isoformat()
        elif rate > 0:
            # a big target on a cents-a-month rate is a century-scale ETA;
            # timedelta overflows past ~2.7M years and the exception would
            # take every OTHER goal down with it — cap the horizon, an
            # ETA past a lifetime is "never" in every sense that matters
            days = min(30.44 * remaining / rate, 365.0 * 100)
            eta = (today + dt.timedelta(days=days)).isoformat()
        on_pace = None
        if target > 0 and g.get("target_date") and remaining > 0:
            try:
                td = dt.date.fromisoformat(str(g["target_date"]))
                months_left = max(0.1, (td - today).days / 30.44)
                on_pace = rate * months_left >= remaining - 0.005
            except ValueError:
                pass
        # a sweep goal's month is satisfied by the SAME matched-transfer
        # evidence progress runs on — the ledger seeing the transfer (or
        # its checking leg) is what marks the month done, so the forecast
        # stops owing it and the Today line can say "swept".
        plan = float(g.get("monthly_plan") or 0)
        swept = None
        satisfied = None
        if mode == "sweep" and plan > 0:
            swept = round(max(0.0, posted_for_plan(conn, cfg, g,
                                                   since=month_start,
                                                   until=today,
                                                   memo=memo)), 2)
            satisfied = swept >= plan - 0.005
        out.append({
            "name": str(g["name"]), "target": round(target, 2),
            "target_date": g.get("target_date") or None,
            "monthly_plan": round(plan, 2),
            "saved": saved, "rate_90d": rate,
            "pct": (round(min(100.0, 100 * saved / target), 1)
                    if target > 0 else None),
            "eta": eta, "on_pace": on_pace, "plan_only": False,
            "mode": mode, "swept_this_month": swept,
            "sweep_satisfied": satisfied,
        })
    return out


def check_milestones(conn, cfg: dict, today: dt.date | None = None):
    """Edge-triggered transitions since the last check: crossed milestones
    and on→off pace flips for dated goals. Returns (messages, new_state);
    the caller persists new_state to config `savings_goal_state` and emails
    the messages (nightly sweep)."""
    state = dict(cfg.get("savings_goal_state") or {})
    msgs: list[str] = []
    new_state: dict = {}
    for p in progress(conn, cfg, today):
        prev = state.get(p["name"]) or {}
        crossed = max((m for m in MILESTONES
                       if p["pct"] is not None and p["pct"] >= m),
                      default=0)
        if crossed > int(prev.get("milestone") or 0):
            msgs.append(
                f"{p['name']}: {crossed}% funded — "
                f"${p['saved']:,.0f} of ${p['target']:,.0f}"
                + (f" (ETA {p['eta']})" if p["eta"] and crossed < 100 else ""))
        if (p["on_pace"] is False and prev.get("on_pace") is not False
                and prev):
            msgs.append(
                f"{p['name']}: off pace for {p['target_date']} — "
                f"${p['rate_90d']:,.0f}/mo recently vs "
                f"${p['target'] - p['saved']:,.0f} still to go")
        new_state[p["name"]] = {"milestone": crossed,
                                "on_pace": p["on_pace"]}
    return msgs, new_state
