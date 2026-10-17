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
from .compat import as_date

MILESTONES = (25, 50, 75, 100)


def goals(cfg: dict) -> list[dict]:
    out = []
    for g in cfg.get("savings_goals") or []:
        if isinstance(g, dict) and str(g.get("name") or "").strip():
            out.append(g)
    return out


def is_plan_savings_goal(g: dict) -> bool:
    """True for the plan's standing Savings goal. Matched trimmed and
    case-insensitively, the same way the clients find it and the goal
    editor refuses a duplicate name: a goal typed as "savings" IS that
    goal everywhere, so the server must not read it as some other goal and
    drop the month map's and Today's Savings figures."""
    return str(g.get("name") or "").strip().lower() == "savings"


def goal_mode(g: dict) -> str:
    """'monthly' (the default: the plan is a fixed commitment, walked out
    of the forecast like a bill) or 'sweep' (contribute at month end, but
    only what the month's own surplus covers — min(plan, realized
    surplus), nothing in a month that didn't work out). Any unknown or
    absent value reads as 'monthly' so old configs behave unchanged."""
    return ("sweep" if str(g.get("mode") or "").strip().lower() == "sweep"
            else "monthly")


def household_goals(conn, cfg: dict) -> list[dict]:
    """The goals whose plan is HOUSEHOLD money: every goal except one on a
    business-entity account (the owner rule _owner_scope applies to its
    progress). A business saving toward equipment pays the transfer out
    of its own checking, so its plan must not come out of the household
    plan surplus, the household cash forecast or the household sweep line
    — those all start from household-only balances. With the combined view
    on (config combine_entities) the household figures include the
    businesses, and so do these goals, the same way PERSONAL_ONLY_SQL
    stands aside."""
    gs = goals(cfg)
    if not any(g.get("account_id") for g in gs):
        return gs
    business = {r["id"] for r in conn.execute(
        """SELECT id FROM accounts WHERE entity_id IS NOT NULL
             AND NOT COALESCE(NULLIF((SELECT current_setting(
                 'app.combine_entities', true)), '') = 'true', false)"""
    ).fetchall()}
    return [g for g in gs if g.get("account_id") not in business]


def monthly_plan_total(cfg: dict, mode: str | None = None,
                       conn=None) -> float:
    """Sum of monthly plans, optionally restricted to one contribution
    mode — the plan surplus subtracts only the fixed ('monthly')
    commitments, while the sweep line reports the 'sweep' total. Given
    `conn`, only the household's goals count (household_goals)."""
    gs = household_goals(conn, cfg) if conn is not None else goals(cfg)
    return round(sum(float(g.get("monthly_plan") or 0) for g in gs
                     if mode is None or goal_mode(g) == mode), 2)


def is_plan_only(g: dict) -> bool:
    """A goal with no destination account is never measured.

    With no account, `_matched_net` would sum EVERY transfer on the tenant
    (dashboard-wide phantom progress), and match words do not rescue it:
    they match both legs of a transfer, the money arriving in savings and
    the money leaving checking, and with no destination no rule (sign,
    subtype, pairing) can tell which side is the fund. Summed, a real
    contribution nets to $0, a household that links only checking reads a
    negative balance, and a withdrawal out of savings counts as the plan
    paid. So the plan still reduces surplus and rides the forecast, and
    progress stays null until the user picks the account. The balance
    planner saves the account-less shape ({name, monthly_plan}) on
    purpose, so this is common, not an edge."""
    return not g.get("account_id")


def needs_account(g: dict) -> bool:
    """A goal the user gave match words but no account: it reads as
    plan-only (see `is_plan_only`), and the clients prompt for the account
    the money lands in rather than presenting it as a deliberate plan."""
    return not g.get("account_id") and any(
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


_MISS = object()


def _owner_scope(conn, g: dict) -> tuple[str, list]:
    """The money a goal may count, as (SQL fragment, params) on alias `t`.

    A goal on a business-entity account belongs to that business: the
    household-only predicate would drop every row in the very account the
    person chose, and the goal would read $0 saved while the money sits
    there. So the goal counts its OWNER's money — that entity's rows when
    its account is an entity's, the household's otherwise."""
    acc = g.get("account_id")
    ent = None
    if acc:
        # asked once per figure per goal on one page; remembered on the
        # checkout until an account row is written
        mkey = ("goal_account_entity", str(acc))
        hit = getattr(conn, "memo_get", lambda k, d=None: d)(mkey, _MISS)
        if hit is not _MISS:
            ent = hit
        else:
            row = conn.execute("SELECT entity_id FROM accounts WHERE id=%s",
                               (acc,)).fetchone()
            ent = row["entity_id"] if row else None
            if hasattr(conn, "memo_put"):
                conn.memo_put(mkey, ent, "accounts")
    if ent:
        return ("""
  AND (t.entity_id = %s OR (t.entity_id IS NULL AND t.account_id IN
       (SELECT id FROM accounts WHERE entity_id = %s)))""", [ent, ent])
    from . import budget as _b   # late: avoid a circular import at load
    return _b.PERSONAL_ONLY_SQL, []


def _goal_account(conn, g: dict) -> str | None:
    """The account a goal reads, as of now: its pinned account, or the
    effective primary of that account's link group. A goal pinned to one
    source of a dual-sourced savings account would otherwise read $0 the
    moment that source fails over and becomes the group's shadow, since
    every query below drops shadow rows."""
    from . import links
    return links.effective_account(conn, g.get("account_id"))


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
    cond, params = _matched_where(conn, g, since, until)
    sql = f"SELECT COALESCE(SUM(-t.amount), 0) AS net FROM transactions t {cond}"
    # The status, the forecast walk and goal progress each ask for the same
    # goal's net over the same window in one request; the answer is the
    # query's, so the query and its parameters are the key, and it is
    # dropped with any write to what the budget engine's ledger reads.
    mkey = ("goal_net", sql, repr(params))
    hit = getattr(conn, "memo_get", lambda k: None)(mkey)
    if hit is not None:
        return hit
    row = conn.execute(sql, params).fetchone()
    out = float(row["net"])
    if hasattr(conn, "memo_put"):
        from . import budget as _b   # late: avoid a circular import at load
        conn.memo_put(mkey, out, *_b._LEDGER_TABLES)
    return out


def _matched_where(conn, g: dict, since: dt.date | None,
                   until: dt.date | None) -> tuple[str, list]:
    """The rows that count as a goal's contributions, as a WHERE clause on
    alias `t` and its parameters: TRANSFER rows on the goal's account,
    token-filtered, shadows dropped, inside its owner's scope. One
    definition, so what a goal's net sums and what its pace counts as a
    month of saving can never drift apart."""
    from . import links
    where = ["t.removed = 0",
             """COALESCE(t.category_override, t.category_primary, '')
                IN ('TRANSFER_IN','TRANSFER_OUT')"""]
    params: list = []
    shadows = links.shadow_ids(conn)
    if shadows:
        where.append("NOT (t.account_id = ANY(%s))")
        params.append(shadows)
    acc = _goal_account(conn, g)
    if acc:
        where.append("t.account_id = %s")
        params.append(acc)
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
    scope, scope_params = _owner_scope(conn, g)
    return f"WHERE {' AND '.join(where)}" + scope, params + scope_params


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
    part = _net_parts(conn, cfg, g)
    if part is None:
        return 0.0                       # a sibling already claimed it
    own, carved = part
    if not carved and own is g:
        return _matched_net(conn, g, since=since, until=until)
    total = _matched_net(conn, own, since=since, until=until)
    return round(total - sum(_matched_net(conn, s, since=since, until=until)
                             for s in carved), 2)


def _net_parts(conn, cfg: dict, g: dict) -> tuple[dict, list[dict]] | None:
    """How `matched_net` partitions a goal's account: the predicate whose
    rows it adds (`own`, for `_matched_where`) and the tokened siblings
    whose rows it takes back out, or None when an earlier token-less
    sibling claimed the account and the goal reads 0. Shared with the
    pace's month test so both see the same rows as this goal's."""
    acc = _goal_account(conn, g)
    has_tokens = any(str(t).strip() for t in (g.get("tokens") or []))
    if not acc or has_tokens:
        return g, []
    # siblings are goals reading the same account now: two goals pinned to
    # the two sources of one linked account share it like any other pair
    same = [s for s in goals(cfg) if _goal_account(conn, s) == acc]
    tokenless = [s for s in same
                 if not any(str(t).strip() for t in (s.get("tokens") or []))]
    tokened = [s for s in same
               if any(str(t).strip() for t in (s.get("tokens") or []))]
    if len(tokenless) <= 1 and not tokened:
        return g, []
    if tokenless and str(tokenless[0].get("name")) != str(g.get("name")):
        return None
    return {"account_id": acc}, tokened


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
    acc = _goal_account(conn, g)
    if not acc:
        # with no destination there is no "other than the destination", so
        # every matching outflow would read as this goal's checking leg,
        # withdrawals out of savings included; callers gate on is_plan_only,
        # and one that forgets counts nothing rather than everything
        return []
    # the destination and every source of it: a linked twin's legs are
    # the destination's own rows seen twice, never a checking leg
    where.append("NOT (t.account_id = ANY(%s))")
    params.append(sorted({acc, g["account_id"]}))
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
    # the funding leg comes from the goal owner's own accounts: a business
    # saving into its own savings pays from its own checking
    scope, scope_params = _owner_scope(conn, g)
    rows = conn.execute(
        f"SELECT t.id AS id, t.amount AS amount FROM transactions t "
        f"LEFT JOIN accounts a ON a.id = t.account_id "
        f"WHERE {' AND '.join(where)}" + scope,
        params + scope_params).fetchall()
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
    acc = _goal_account(conn, g)
    has_tokens = any(str(t).strip() for t in (g.get("tokens") or []))
    if not acc or has_tokens:
        return round(sum(a for _, a in _checking_side_rows(
            conn, g, plan, since, until)), 2)
    siblings = [s for s in goals(cfg) if _goal_account(conn, s) == acc]
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
    if is_plan_only(g):
        # nothing to verify against (see is_plan_only): every reader gates on
        # it already, and a future one that does not still counts nothing
        return 0.0
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


def month_saved(conn, cfg: dict, g: dict, since: dt.date, until: dt.date,
                memo: dict | None = None) -> float:
    """What one calendar month (or the elapsed part of it) put toward a
    goal, as the Month and Year lenses and Today's this-month row show it:
    `posted_for_plan` over that window, so the month row and the sweep line
    beside it can never disagree."""
    return posted_for_plan(conn, cfg, g, since=since, until=until, memo=memo)


# A goal's pace needs about a month of its account's ledger before
# "off pace" is a claim about saving rather than about how new the feed is.
PACE_MIN_DAYS = 30


def _goal_first_date(conn, g: dict) -> dt.date | None:
    """The first ledger day of the account a goal reads (the household's
    ledger start when the goal names no account)."""
    from . import budget as _b   # late: avoid a circular import at load
    acc = _goal_account(conn, g)
    if not acc:
        return _b.ledger_start(conn)
    mkey = ("goal_first_date", str(acc))
    hit = getattr(conn, "memo_get", lambda k: None)(mkey)
    if hit is not None:
        return hit[0]
    row = conn.execute(
        "SELECT MIN(date) AS d FROM transactions "
        "WHERE removed = 0 AND account_id = %s", (acc,)).fetchone()
    out = as_date(row["d"]) if row and row["d"] else None
    if hasattr(conn, "memo_put"):
        conn.memo_put(mkey, (out,), *_b._LEDGER_TABLES)
    return out


def _covered_days(conn, g: dict, since: dt.date, today: dt.date) -> int:
    """Days of the pace window [since, today] the goal's ledger covers."""
    first = _goal_first_date(conn, g)
    lo = max(since, first) if first else since
    return max(0, (today - lo).days)


def _pace_months(conn, cfg: dict, g: dict, since: dt.date,
                 today: dt.date) -> float:
    """The months a goal's pace divides by: three for a full window, else
    the CALENDAR months from the ledger's first day to today. The first
    row is itself a deposit, so the month it funds counts: two monthly
    $500 transfers 35 days apart are two months of saving ($500/mo), not
    1.15 months ($870/mo) — and a single transfer last week is one month,
    never a rate four times its size.

    Calendar months, not days / 30.44: April 1 to May 1 is 30 days, which
    a day count reads as under one month, so the May 1 deposit would make
    two months' saving read as one $1,000 month. The month in progress
    counts once the goal's account has a row in it; before that, a goal
    funded on the 15th read on the 15th of the next month is one month of
    saving, not half a month each of two. Only a contribution opens it:
    the interest or fee the bank posts on the 1st is not saving, and
    counting its month would halve the rate of a goal funded once, and
    neither is a withdrawal or a sibling goal's deposit."""
    days = _covered_days(conn, g, since, today)
    if days >= (today - since).days:
        return 3.0
    first = _goal_first_date(conn, g)
    lo = max(since, first) if first else since
    months = (today.year * 12 + today.month) - (lo.year * 12 + lo.month)
    if months == 0 or _has_contribution_this_month(conn, cfg, g, today):
        months += 1
    return float(min(3, max(1, months)))


def _has_contribution_this_month(conn, cfg: dict, g: dict,
                                 today: dt.date) -> bool:
    """Whether the month in progress holds a contribution the goal's net
    counts: a TRANSFER into its account (amount < 0, Plaid signs) on the
    rows `matched_net` adds, less the rows it hands to tokened siblings.
    Interest or a fee would add a month the numerator never sees; so would
    a withdrawal, which only lowers the net, and so would a sibling's own
    transfer, which the net already carves out — each halves the rate of a
    goal funded once."""
    part = _net_parts(conn, cfg, g)
    if part is None:
        return False
    own, carved = part
    since = today.replace(day=1)

    def contributions(goal: dict) -> set:
        cond, params = _matched_where(conn, goal, since, today)
        rows = conn.execute(
            f"SELECT t.id FROM transactions t {cond} AND t.amount < 0",
            params).fetchall()
        return {r["id"] for r in rows}

    mine = contributions(own)
    for s in carved:
        if not mine:
            break
        mine -= contributions(s)
    return bool(mine)


def progress(conn, cfg: dict, today: dt.date | None = None,
             memo: dict | None = None) -> list[dict]:
    """Per-goal progress + pace. Pace projects from the trailing 3 months
    of actual contributions; with a target_date it reports on/off pace,
    without one it estimates the arrival month. `memo` is the optional
    request-scoped cache described on `posted_for_plan`.

    Saved is the goal's original measure, start_balance plus the
    destination's ledger net, because every rule that also added inferred
    checking legs into the total counted some household's unrelated
    transfers, or a withdrawal, as money saved."""
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
                "needs_account": needs_account(g),
                "mode": mode, "swept_this_month": None,
                "sweep_satisfied": None,
            })
            continue
        saved = round(float(g.get("start_balance") or 0)
                      + matched_net(conn, cfg, g), 2)
        # The pace window is the trailing 90 days, divided by the months
        # the goal's ledger actually covers inside it: a goal funded for a
        # month is not a third of a three-month window, which would read
        # it "off pace" and put its ETA three times too far.
        since = today - dt.timedelta(days=90)
        cover = _pace_months(conn, cfg, g, since, today)
        rate = round(matched_net(conn, cfg, g, since=since) / cover, 2)
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
        # under a month of ledger there is no rate to judge a pace by
        if (target > 0 and g.get("target_date") and remaining > 0
                and _covered_days(conn, g, since, today) >= PACE_MIN_DAYS):
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
            "needs_account": False,
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
