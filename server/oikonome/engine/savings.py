"""Savings goals: ledger-verified progress toward named funds
(trip fund, emergency fund) — spec `specs/income-scenarios-goals.md`.

A goal matches TRANSFER rows on its destination account (optional token
carve-outs so several goals share one savings account), plus an optional
starting balance. A 'monthly'-mode plan reduces plan surplus and lands in
the cash forecast as a scheduled outflow; a 'sweep'-mode plan contributes
only at month end and only what the month's realized surplus covers, so
it never reduces the plan surplus and never walks money out of the
forecast that the month's numbers say cannot leave. The daily verdict
never sees goals (it stays variable-spend-only).

A goal with a target and a target date is judged against its schedule,
the straight line from the day that plan was set to the target on its
date (`specs/savings-pace.md`): the verdict reads what is saved on each
day and nothing about how the deposits arrive. Milestone emails are
edge-triggered (25/50/75/100% and falling off pace for a dated goal);
their state and each goal's plan are kept in tenant config
`savings_goal_state`."""

from __future__ import annotations

import calendar
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


def _tokens_of(g: dict) -> list[str]:
    return [str(t).strip().lower() for t in (g.get("tokens") or [])
            if str(t).strip()]


def _siblings(conn, cfg: dict, g: dict, acc: str) -> tuple[list[dict], int]:
    """The goals reading account `acc` now, in the household's own order,
    and the position of `g` among them (found by identity, then by name; a
    goal the config does not carry reads last). Two goals pinned to the two
    sources of one linked account share it like any other pair."""
    same = [s for s in goals(cfg) if _goal_account(conn, s) == acc]
    for i, s in enumerate(same):
        if s is g:
            return same, i
    for i, s in enumerate(same):
        if str(s.get("name")) == str(g.get("name")):
            return same, i
    return same + [g], len(same)


def _partition(conn, sibs: list[dict]) -> list[list[tuple]]:
    """THE allocator of one account's rows among the goals that read it:
    every contribution row (`_matched_where` without the words) goes to
    exactly one goal, by row id — the first goal in `sibs` order whose
    match words all appear in it, else the first goal with no match words,
    else to none. Returns, per goal in `sibs`, its rows as (id, date,
    amount). Every figure a goal shows — saved, a window's net, the pace's
    days, the sweep line's evidence — sums these rows, so goals sharing an
    account add up to what arrived and no row counts twice whatever words
    overlap. Asked once per account per checkout."""
    specs = repr([(str(s.get("account_id")), _tokens_of(s)) for s in sibs])
    mkey = ("goal_partition", specs)
    hit = getattr(conn, "memo_get", lambda k: None)(mkey)
    if hit is not None:
        return hit
    # one pool per pinned account (its owner scope); one query in the
    # usual case of every goal pinned to the same source
    pools: dict[str, dict] = {}
    for s in sibs:
        pin = str(s.get("account_id"))
        if pin in pools:
            continue
        cond, params = _matched_where(
            conn, {"account_id": s.get("account_id")}, None, None)
        pools[pin] = {
            str(r["id"]): (str(r["id"]), as_date(r["d"]), float(r["amount"]),
                           str(r["text"] or ""))
            for r in conn.execute(
                "SELECT t.id AS id, t.date AS d, t.amount AS amount, "
                "LOWER(COALESCE(t.merchant_name,'') || ' ' || "
                f"COALESCE(t.name,'')) AS text FROM transactions t {cond}",
                params).fetchall()}
    words = [_tokens_of(s) for s in sibs]
    keep = next((i for i, w in enumerate(words) if not w), None)
    out: list[list[tuple]] = [[] for _ in sibs]
    for rid in sorted(set().union(*(p.keys() for p in pools.values()))):
        owner = None
        for i, s in enumerate(sibs):
            row = pools[str(s.get("account_id"))].get(rid)
            if row and words[i] and all(w in row[3] for w in words[i]):
                owner = i
                break
        if owner is None and keep is not None:
            if rid in pools[str(sibs[keep].get("account_id"))]:
                owner = keep
        if owner is not None:
            row = pools[str(sibs[owner].get("account_id"))][rid]
            out[owner].append(row[:3])
    if hasattr(conn, "memo_put"):
        from . import budget as _b   # late: avoid a circular import at load
        conn.memo_put(mkey, out, *_b._LEDGER_TABLES)
    return out


def _own_rows(conn, cfg: dict, g: dict, since: dt.date | None = None,
              until: dt.date | None = None) -> list[tuple] | None:
    """The rows `_partition` gives `g`, as (id, date, amount), inside the
    window; None for a goal with no account (it is never partitioned)."""
    acc = _goal_account(conn, g)
    if not acc:
        return None
    sibs, i = _siblings(conn, cfg, g, acc)
    return [r for r in _partition(conn, sibs)[i]
            if (since is None or r[1] >= since)
            and (until is None or r[1] <= until)]


def goal_row_ids(conn, cfg: dict, g: dict, since: dt.date | None = None,
                 until: dt.date | None = None) -> set[str]:
    """The ids of the rows a goal counts (see `_partition`)."""
    return {r[0] for r in (_own_rows(conn, cfg, g, since, until) or [])}


def matched_net(conn, cfg: dict, g: dict, since: dt.date | None = None,
                until: dt.date | None = None) -> float:
    """A goal's net contributions over the window: the sum of the rows the
    account's one partition (`_partition`) gives it. Goals sharing an
    account therefore add up to the account's net when one of them has no
    match words, a transfer whose text carries two goals' words counts for
    the first of them only, and later goals with no words read 0 (give them
    words to scope them)."""
    rows = _own_rows(conn, cfg, g, since, until)
    if rows is None:
        return _matched_net(conn, g, since=since, until=until)
    return round(sum(-a for _, _, a in rows), 2)


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
    inferred a second time by an amount-matching neighbour. Between goals
    WITH words the destination's order holds too: a checking leg whose
    text carries two goals' words is the first one's (`_partition`)."""
    acc = _goal_account(conn, g)
    if not acc:
        return round(sum(a for _, a in _checking_side_rows(
            conn, g, plan, since, until)), 2)
    siblings, pos = _siblings(conn, cfg, g, acc)
    if _tokens_of(g):
        # only the goals with words ahead of this one can claim its legs
        tokened = [s for s in siblings[:pos] if _tokens_of(s)]
    else:
        tokenless = [s for s in siblings if not _tokens_of(s)]
        if tokenless and tokenless[0] is not siblings[pos]:
            return 0.0                   # a sibling already claimed it
        tokened = [s for s in siblings if _tokens_of(s)]
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
    the plan predicts), and only while the destination has no rows of its
    own in the window. max — never both — so two posted legs of one
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
        # The checking leg stands in only for a SILENT destination. When the
        # destination has rows of its own in the window they are ledger
        # truth whatever they net to: $500 in and $200 back out saved $300,
        # and a month that took money out saved less than nothing. The rows
        # are the ones matched_net adds (the account's one partition), so a
        # sibling's transfer never counts.
        if goal_row_ids(conn, cfg, g, since, until):
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


def pace_since(today: dt.date) -> dt.date:
    """The first day of a goal's pace window ("the last 3 months", 89 to
    92 days, so never called a day count): the day after the same day
    three calendar months back, so a monthly deposit is counted three
    times however long those months are — Jan 1 to Apr 1 is 90 days, and
    a window opening ON Jan 1 holds four. A month's last day
    goes back to a month's last day: Jun 30 opens on Apr 1, not Mar 31,
    or a month-end deposit lands four times."""
    y, m = divmod(today.year * 12 + today.month - 1 - 3, 12)
    back_len = calendar.monthrange(y, m + 1)[1]
    if today.day == calendar.monthrange(today.year, today.month)[1]:
        day = back_len
    else:
        day = min(today.day, back_len)
    return dt.date(y, m + 1, day) + dt.timedelta(days=1)


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


def _contributed(conn, cfg: dict, g: dict, since: dt.date,
                 until: dt.date | None) -> bool:
    """Whether the goal's net counts a deposit in the window: a TRANSFER
    into its account on the rows `matched_net` adds (the account's one
    partition). Asked by row, not by sum, because a deposit and a larger
    withdrawal net below zero and the deposit still happened."""
    rows = _own_rows(conn, cfg, g, since, until)
    if rows is None:
        cond, params = _matched_where(conn, g, since, until)
        return bool(conn.execute(
            f"SELECT 1 FROM transactions t {cond} AND t.amount < 0 LIMIT 1",
            params).fetchone())
    return any(a < 0 for _, _, a in rows)


def _has_contribution_this_month(conn, cfg: dict, g: dict,
                                 today: dt.date) -> bool:
    """Whether the month in progress holds a contribution the goal's net
    counts: a TRANSFER into its account (amount < 0, Plaid signs) on the
    rows `matched_net` adds, less the rows it hands to tokened siblings.
    Interest or a fee would add a month the numerator never sees; so would
    a withdrawal, which only lowers the net, and so would a sibling's own
    transfer, which the net already carves out — each halves the rate of a
    goal funded once."""
    return _contributed(conn, cfg, g, today.replace(day=1), today)


# The schedule a dated goal is judged against (specs/savings-pace.md).
# Late is "less saved today than the line asked for PACE_GRACE_DAYS ago":
# one monthly cycle (31 days), stretched to the 35 a "fourth Thursday"
# month can run, plus three days for a weekend or a slow posting. A goal
# that has been late is on pace again once it is within PACE_RETURN_DAYS of
# the line; the gap between the two keeps a saver a few percent short from
# reading on at every deposit and off again days later. The target date
# gets PACE_POSTING_DAYS for a finishing deposit that posts after a weekend.
PACE_GRACE_DAYS = 38
PACE_RETURN_DAYS = 7
PACE_POSTING_DAYS = 3


def schedule_days(nets: dict, base: float, plan_day: dt.date,
                  start: float, target: float, target_date: dt.date,
                  days: int):
    """A goal's verdict on each of the `days` days after its plan day, in
    order: True (on its schedule), False (off it), or None while there is
    nothing to judge (the plan's first cycle, a funded goal).

    The schedule is the straight line from (`plan_day`, `start`) to
    (`target_date`, `target`). `nets` holds each later day's net
    contributions and `base` what was saved on the plan day. The days are
    replayed in order because being late and being back on pace use two
    thresholds, so a day's answer depends on the days before it and on
    nothing else. No rate, rhythm or deposit size is read.

    Day offsets, not dates: a target date at the calendar's far end must
    not overflow date arithmetic."""
    span = (target_date - plan_day).days
    by_offset: dict[int, float] = {}
    for d, v in nets.items():
        n = (d - plan_day).days
        if 0 < n <= days:
            by_offset[n] = by_offset.get(n, 0.0) + v
    gap = target - start
    saved, verdict = base, None
    for n in range(1, days + 1):
        if n in by_offset:
            saved += by_offset[n]
        if saved >= target - 0.005:
            verdict = True             # funded: nothing to be late for
            yield None
            continue
        if n - span > PACE_POSTING_DAYS:
            verdict = False            # the date passed with money left
        elif n <= PACE_GRACE_DAYS:
            verdict = None
        else:
            back = PACE_RETURN_DAYS if verdict is False else PACE_GRACE_DAYS
            m = n - back
            if span <= 0:              # the date had passed when it was set
                due = target if m >= span else start
            else:
                due = start + gap * min(1.0, max(0.0, m / span))
            verdict = saved >= due - 0.005
        yield verdict


def schedule_verdict(nets: dict, base: float, plan_day: dt.date,
                     start: float, target: float, target_date: dt.date,
                     today: dt.date) -> bool | None:
    """`schedule_days` for one day: the verdict on `today`, None on or
    before the plan day."""
    verdict = None
    for verdict in schedule_days(nets, base, plan_day, start, target,
                                 target_date, (today - plan_day).days):
        pass
    return verdict


def _target_date(g: dict) -> dt.date | None:
    """A goal's target date, or None when it has none or it is not one."""
    try:
        return dt.date.fromisoformat(str(g.get("target_date") or ""))
    except ValueError:
        return None


def _plan_terms(g: dict) -> list:
    """What a goal's plan was made for. A change to any of these is a new
    plan: the target and date move the line, and the account, match words
    and starting balance move what counts as saved. The name is left out,
    so a renamed goal keeps its plan."""
    return [round(float(g.get("target") or 0), 2),
            str(g.get("target_date") or ""),
            str(g.get("account_id") or ""),
            [str(t).strip().lower() for t in (g.get("tokens") or [])
             if str(t).strip()],
            round(float(g.get("start_balance") or 0), 2)]


def _stored_plan(cfg: dict, g: dict) -> dict | None:
    """The plan stored for a goal, if it was made for the goal as it is
    now: its own entry, or the entry of a name no goal carries any more
    when the terms match (the goal was renamed)."""
    state = cfg.get("savings_goal_state") or {}
    if not isinstance(state, dict):
        return None
    terms = _plan_terms(g)

    def usable(entry) -> dict | None:
        plan = entry.get("plan") if isinstance(entry, dict) else None
        if (isinstance(plan, dict) and plan.get("for") == terms
                and plan.get("day")):
            return plan
        return None

    own = usable(state.get(str(g.get("name"))))
    if own:
        return own
    names = {str(x.get("name")) for x in goals(cfg)}
    for name, entry in state.items():
        if name not in names and usable(entry):
            return usable(entry)
    return None


def _day_nets(conn, g: dict, since: dt.date, until: dt.date) -> dict:
    """One goal predicate's net contributions per day over a window."""
    cond, params = _matched_where(conn, g, since, until)
    sql = ("SELECT t.date AS d, COALESCE(SUM(-t.amount), 0) AS net "
           f"FROM transactions t {cond} GROUP BY t.date")
    mkey = ("goal_day_nets", sql, repr(params))
    hit = getattr(conn, "memo_get", lambda k: None)(mkey)
    if hit is not None:
        return hit
    out = {as_date(r["d"]): float(r["net"])
           for r in conn.execute(sql, params).fetchall()}
    if hasattr(conn, "memo_put"):
        from . import budget as _b   # late: avoid a circular import at load
        conn.memo_put(mkey, out, *_b._LEDGER_TABLES)
    return out


def _net_by_day(conn, cfg: dict, g: dict, since: dt.date,
                until: dt.date) -> dict:
    """`matched_net`, day by day: the same rows, the same partition between
    goals sharing an account, so the schedule and the saved total can never
    count different money."""
    if since > until:
        return {}
    rows = _own_rows(conn, cfg, g, since, until)
    if rows is None:
        return dict(_day_nets(conn, g, since, until))
    out: dict = {}
    for _, d, a in rows:
        out[d] = out.get(d, 0.0) - a
    return out


def _schedule_on_pace(conn, cfg: dict, g: dict, plan: dict | None,
                      today: dt.date) -> bool | None:
    """A dated goal's verdict under its stored plan; None without one."""
    td = _target_date(g)
    if td is None:
        return None
    if (today - td).days > PACE_POSTING_DAYS:
        return False                   # true of any plan, and of none
    if not plan:
        return None
    try:
        plan_day = dt.date.fromisoformat(str(plan.get("day")))
        start = float(plan.get("saved") or 0)
    except (TypeError, ValueError):
        return None
    if today <= plan_day:
        return None
    # what is saved on the plan day is read from the ledger as it is now, so
    # a row that arrives later with an earlier date still counts as saved;
    # the line keeps the starting point it was drawn from
    base = (float(g.get("start_balance") or 0)
            + matched_net(conn, cfg, g, until=plan_day))
    nets = _net_by_day(conn, cfg, g, plan_day + dt.timedelta(days=1), today)
    return schedule_verdict(nets, base, plan_day, start,
                            float(g.get("target") or 0), td, today)


def _plan_tonight(conn, cfg: dict, g: dict, today: dt.date,
                  prev: dict, adopted: bool) -> tuple[dict | None, bool]:
    """The plan a dated goal carries after tonight, and whether it was
    started tonight from an earlier day.

    A goal keeps the plan made for its terms. A goal whose terms changed,
    or a new goal, starts its plan tonight with what is saved tonight. A
    goal that was being saved toward before any plan was recorded (its
    stored entry has no plan field, or nothing here has one) has its line
    drawn from PACE_GRACE_DAYS back, at what the ledger shows saved on that
    day, so it is judged from tomorrow instead of going five weeks without
    a verdict; never from before its account's first row."""
    if float(g.get("target") or 0) <= 0 or _target_date(g) is None:
        return None, False
    stored = _stored_plan(cfg, g)
    if stored:
        return dict(stored), False
    day = today
    existing = ("plan" not in prev) if prev else not adopted
    if existing:
        first = _goal_first_date(conn, g)
        if first and first < today:
            day = max(today - dt.timedelta(days=PACE_GRACE_DAYS), first)
    saved = round(float(g.get("start_balance") or 0)
                  + matched_net(conn, cfg, g, until=day), 2)
    plan = {"day": day.isoformat(), "saved": saved, "for": _plan_terms(g)}
    # a line drawn from an earlier day gives no credit for being ahead of
    # it before then, so an off-pace verdict under it may be the change of
    # measure and not the saver: nothing is mailed until the goal has read
    # on pace under this line once
    if day < today:
        plan["quiet"] = True
    return plan, day < today


def progress(conn, cfg: dict, today: dt.date | None = None,
             memo: dict | None = None) -> list[dict]:
    """Per-goal progress + pace. `saved_90d` is the net of the last 3
    months (`pace_since`), the figure the goal line prints; `rate_90d` is that net per month
    the window covers, and the arrival date projects from it; a goal
    with a target date is instead judged on/off pace against its schedule
    (`schedule_verdict`), which reads neither. `memo` is the optional
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
                "saved": None, "rate_90d": None, "saved_90d": None,
                "pct": None,
                "eta": None, "on_pace": None, "contributed_90d": None,
                "plan_only": True,
                "needs_account": needs_account(g),
                "mode": mode, "swept_this_month": None,
                "sweep_satisfied": None,
            })
            continue
        saved = round(float(g.get("start_balance") or 0)
                      + matched_net(conn, cfg, g), 2)
        # The pace window is the last 3 months (pace_since), divided by the months
        # the goal's ledger actually covers inside it: a goal funded for a
        # month is not a third of a three-month window, which would read
        # it "off pace" and put its ETA three times too far.
        since = pace_since(today)
        cover = _pace_months(conn, cfg, g, since, today)
        # the goal line prints the window's own net ("$X in the last 3
        # months"), which is literally what was saved; the per-month figure
        # stays in the payload for released apps and drives the ETA
        net_90d = matched_net(conn, cfg, g, since=since)
        rate = round(net_90d / cover, 2)
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
            on_pace = _schedule_on_pace(conn, cfg, g, _stored_plan(cfg, g),
                                        today)
        # a rate at or below zero has two causes the clients word apart:
        # nothing arrived, or withdrawals outweighed what did
        contributed = rate > 0 or _contributed(conn, cfg, g, since, None)
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
            "saved_90d": round(net_90d, 2),
            "pct": (round(min(100.0, 100 * saved / target), 1)
                    if target > 0 else None),
            "eta": eta, "on_pace": on_pace, "contributed_90d": contributed,
            "plan_only": False,
            "needs_account": False,
            "mode": mode, "swept_this_month": swept,
            "sweep_satisfied": satisfied,
        })
    return out


def check_milestones(conn, cfg: dict, today: dt.date | None = None):
    """Edge-triggered transitions since the last check: crossed milestones
    and on→off pace flips for dated goals. Returns (messages, new_state);
    the caller emails the messages (nightly sweep) and persists new_state
    to config `savings_goal_state`.

    Each entry of new_state also carries the goal's plan (None for a goal
    without a target date). The caller stores the plans whether or not the
    mail went out (`plans_of`): a verdict that waits for a retry is
    recomputed tomorrow, but a plan that was not stored would start again
    from tomorrow's balance."""
    today = today or localtime.now_local(cfg).date()
    state = cfg.get("savings_goal_state") or {}
    state = dict(state) if isinstance(state, dict) else {}
    adopted = any(isinstance(e, dict) and "plan" in e for e in state.values())
    plans: dict = {}
    started_back: set = set()
    by_name = {str(g["name"]): g for g in goals(cfg)}
    for name, g in by_name.items():
        if is_plan_only(g):
            plans[name] = None
            continue
        prev = state.get(name)
        prev = prev if isinstance(prev, dict) else {}
        plans[name], back = _plan_tonight(conn, cfg, g, today, prev, adopted)
        if back:
            started_back.add(name)
    # tonight's verdicts are read under tonight's plans
    judged = dict(cfg)
    judged["savings_goal_state"] = {
        **state, **{n: {**(state.get(n) if isinstance(state.get(n), dict)
                           else {}), "plan": pl}
                    for n, pl in plans.items()}}
    msgs: list[str] = []
    new_state: dict = {}
    for p in progress(conn, judged, today):
        prev = state.get(p["name"])
        prev = prev if isinstance(prev, dict) else {}
        plan = plans.get(p["name"])
        crossed = max((m for m in MILESTONES
                       if p["pct"] is not None and p["pct"] >= m),
                      default=0)
        if crossed > int(prev.get("milestone") or 0):
            msgs.append(
                f"{p['name']}: {crossed}% funded — "
                f"${p['saved']:,.0f} of ${p['target']:,.0f}"
                + (f" (ETA {p['eta']})" if p["eta"] and crossed < 100 else ""))
        on_pace = p["on_pace"]
        if p["name"] in started_back:
            # no verdict exists yet under a line drawn tonight; the stored
            # one stands for the night rather than reading as a change
            on_pace = prev.get("on_pace")
        elif plan and plan.get("quiet"):
            if on_pace is True:
                plan = {k: v for k, v in plan.items() if k != "quiet"}
        elif (on_pace is False and prev.get("on_pace") is not False
                and prev):
            msgs.append(_off_pace_message(p, plan, today))
        new_state[p["name"]] = {"milestone": crossed, "on_pace": on_pace,
                                "plan": plan}
    return msgs, new_state


def _off_pace_message(p: dict, plan: dict | None, today: dt.date) -> str:
    """The off-pace line of the milestone mail. It quotes the balance and
    the schedule the verdict was read from, never the monthly rate, so the
    mail cannot say one thing and its own numbers another."""
    left = p["target"] - p["saved"]
    td = dt.date.fromisoformat(str(p["target_date"]))
    if today > td:
        return (f"{p['name']}: off pace — {p['target_date']} has passed "
                f"with ${left:,.0f} still to go")
    text = (f"{p['name']}: off pace for {p['target_date']} — "
            f"${p['saved']:,.0f} saved")
    try:
        plan_day = dt.date.fromisoformat(str((plan or {}).get("day")))
        start = float((plan or {}).get("saved") or 0)
        span = (td - plan_day).days
        if span > 0:
            due = start + (p["target"] - start) * min(
                1.0, max(0.0, (today - plan_day).days / span))
            text += (f"; the straight line to ${p['target']:,.0f} asked "
                     f"for ${due:,.0f} by now")
    except (TypeError, ValueError):
        pass
    days_left = (td - today).days
    if days_left > 30:
        text += (f". ${left / (days_left / 30.44):,.0f}/mo from here "
                 "gets there")
    return text


def plans_of(state: dict, new_state: dict) -> dict:
    """`state` with each goal's plan taken from `new_state` and nothing
    else changed: what the nightly sweep stores on a night the milestone
    mail was not delivered, when the milestones and verdicts must wait for
    the retry but the plans must not."""
    out = dict(state) if isinstance(state, dict) else {}
    for name, entry in new_state.items():
        kept = out.get(name)
        plan = entry.get("plan") if isinstance(entry, dict) else None
        if plan is None and not isinstance(kept, dict):
            continue                   # nothing of this goal's to keep yet
        kept = dict(kept) if isinstance(kept, dict) else {}
        kept["plan"] = plan
        out[name] = kept
    return out


def undelivered_state(state: dict, new_state: dict) -> dict:
    """What the nightly sweep stores on a night the milestone mail was not
    delivered: every goal's plan (`plans_of`), and tonight's verdict for
    each goal whose verdict was not news. Only a fall onto off pace is
    mailed (`check_milestones`), so only that verdict waits for the retry;
    any other, held back, would leave an old verdict standing (one read
    under an earlier rule, say) and a later real fall would find no edge
    and never be mailed. Milestones always wait."""
    out = plans_of(state, new_state)
    for name, entry in new_state.items():
        if not isinstance(entry, dict):
            continue
        prev = state.get(name) if isinstance(state, dict) else None
        prev = prev if isinstance(prev, dict) else {}
        on_pace = entry.get("on_pace")
        if on_pace is False and prev.get("on_pace") is not False and prev:
            continue                   # the news the retry must carry
        if on_pace == prev.get("on_pace"):
            continue
        kept = dict(out.get(name)) if isinstance(out.get(name), dict) else {}
        kept["on_pace"] = on_pace
        out[name] = kept
    return out
