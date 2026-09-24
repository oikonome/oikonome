"""Budget engine: schedule-aware fixed costs + two variable buckets.

The month's spending is split three ways:
  fixed — transactions matched to a recurring bill (merchant/token match +
          amount tolerance), recognized per OCCURRENCE (rent due on the 1st
          is *expected* on day 1 — timing never reads as "over budget").
  food  — FOOD_AND_DRINK / a store item match's "<Store> - Food & Drink".
  other — every other unmatched spend transaction.

Config lives per-tenant in tenant_settings.config (JSONB).

Verdict: VARIABLE-ONLY (fixed bills never drive over/under), tolerance
max($50, 5% of variable expected-to-date).

Amount sign follows Plaid: positive = money out.
"""

import calendar
import contextlib
import datetime as dt
import json
import re

from .. import localtime
from .compat import as_date, as_dict, jsonb
from . import merchant_sql
from . import categories as _cats

# effective category (override wins) so a manual recategorization to/away
# from FOOD_AND_DRINK moves the row between buckets
FOOD_SQL = ("(COALESCE(t.category_override, t.category_primary) = 'FOOD_AND_DRINK'"
            " OR t.category_override IN ('Amazon - Food & Drink',"
            " 'Costco - Food & Drink'))")
# the same test per PART of a hand-split row (categories.SPLIT_JOIN must be
# in the query): the grocery half of a mixed receipt is food, the hardware
# half is not
PART_FOOD_SQL = (f"({_cats.PART_CAT} = 'FOOD_AND_DRINK'"
                 f" OR {_cats.PART_CAT} IN ('Amazon - Food & Drink',"
                 " 'Costco - Food & Drink'))")

# How much month must elapse before UNDER BUDGET is a claim about spending
# rather than about the calendar. 0.10 ≈ days 1–4 of a 31-day month
# (the fraction counts elapsed days: today is not one of them).
EARLY_MONTH_FRAC = 0.10

# partial reimbursements net the received money off the row — the
# remainder stays real spend. Defined HERE (not in reporting) because
# reporting imports budget, not the other way around; reporting re-exports it
# so the two can't drift apart.
#
# Floored at 0. Over-linked deposits would drive the net NEGATIVE, which a
# SUM() then subtracts from real spend elsewhere in the month — food/other
# understated, a false UNDER BUDGET. link_reimbursement caps links at the
# charge, but a restored row need not have passed through it, and a floor is
# the cheaper invariant to keep true.
NET_AMOUNT = """GREATEST(t.amount - COALESCE((SELECT SUM(pr.amount)
    FROM reimbursements pr
    WHERE pr.expense_id = t.id AND pr.partial = 1), 0), 0)"""

# hard separation: business-entity money is excluded from every
# PERSONAL aggregate. A transaction is business if its own entity_id is set OR
# its account is entity-assigned (app.entity_account_ids — a comma list of
# assigned account ids, set per tenant_connect alongside app.shadow_ids). Empty
# session var (no entities declared) → no-op, so this is free until a tenant
# actually creates an entity. This is the user's own assignment, NOT billing —
# self-host separates too. Canonical here (like NET_AMOUNT); reporting imports
# it so _spend_rows and SPEND_WHERE cannot drift. `t` must be the transactions
# alias. (Requires the app.tenant_id ambient set; unset → the OR-NULL guard
# makes it a no-op rather than an error.)
# The combined toggle (config combine_entities=true) sets app.combine_entities
# per tenant_connect; when 'true' the whole predicate short-circuits to no-op so
# personal views show business + personal together.
#
# The GUC reads are wrapped as scalar subqueries on purpose: a bare
# current_setting() in a WHERE clause is re-evaluated for every row the scan
# visits, while `(SELECT current_setting(...))` is planned as an InitPlan and
# read once per statement. Same answer, measurably cheaper on a full scan.
PERSONAL_ONLY_SQL = """
  AND (NULLIF((SELECT current_setting('app.combine_entities', true)), '') = 'true'
       OR (t.entity_id IS NULL
           AND (NULLIF((SELECT current_setting('app.entity_account_ids', true)), '') IS NULL
                OR NOT (t.account_id = ANY(string_to_array(
                        (SELECT current_setting('app.entity_account_ids', true)), ','))))))"""

# Account-level combined guard (net worth / forecast, which filter a.entity_id).
COMBINE_OR = ("NULLIF((SELECT current_setting('app.combine_entities', true)), '')"
              " = 'true' OR ")

# What counts as SPENDING, as SQL. `t` must be the transactions alias, and the
# caller supplies `t.removed = 0` and its own scope. Canonical here so the
# ledger's own aggregates cannot drift from the verdict's: money out only,
# never a card payment or a transfer between the user's own accounts (the
# EFFECTIVE category, so a manual TRANSFER_OUT override excludes the row), and
# never the loan-servicer side of a payment the checking account already
# recorded.
SPEND_ONLY_SQL = """
  AND t.amount > 0
  AND COALESCE(t.category_detailed,'') != 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
  AND COALESCE(t.category_override, t.category_primary, '')
      NOT IN ('TRANSFER_OUT','TRANSFER_IN')
  AND t.account_id NOT IN (SELECT id FROM accounts WHERE type='loan')"""

STOP_TOKENS = {"the", "and", "for", "inc", "llc", "corp", "com", "www", "payment",
               "pmt", "pmts", "online", "web", "bill", "billpay", "ach", "achdw",
               "auto", "pay", "des", "tran", "future", "amount"}

DEFAULT_CONFIG = {"food_monthly": 0.0, "other_monthly": 0.0,
                  "dynamic_variable_budget": True}


def excluded_account_ids(cfg: dict) -> list[str]:
    """The Accounts page's "excl" toggle (config `excluded_accounts`) as the
    account-id list the money math filters on. The ONE reader — every
    surface that drops those accounts' rows, balances and payoff events
    goes through here.

    Read defensively, because the settings blob is a document a restore can
    write verbatim: `cfg.get(key) or []` substitutes only on a FALSY value,
    so a `true` or a number left under the key by a hand-edited or buggy
    export walks straight into `list()` and raises `TypeError: 'bool'
    object is not iterable` — one malformed field turning the forecast, the
    calendar strip and every report into a 500 with no UI to undo it. A
    value that is not a list reads as "nothing excluded", and elements that
    cannot be an account id are dropped rather than stringified into a
    predicate that matches nothing."""
    v = cfg.get("excluded_accounts")
    if not isinstance(v, (list, tuple)):
        return []
    return [str(a) for a in v
            if isinstance(a, (str, int)) and not isinstance(a, bool)]


def load_config(conn, *, memo: dict | None = None) -> dict:
    """Per-tenant config from tenant_settings (RLS-scoped). Seeds bucket
    budgets from 3-month history on first run.
    `memo`: the request-scoped cache — every engine pass a Today request
    runs loads the config, and nothing writes it mid-request."""
    if memo is not None and "config" in memo:
        return memo["config"]
    cfg = _load_config(conn)
    if memo is not None:
        memo["config"] = cfg
    return cfg


def _load_config(conn) -> dict:
    row = conn.execute("SELECT config FROM tenant_settings").fetchone()
    cfg = dict(row["config"]) if row else {}
    # the live document, coerced once at the door: a reader anywhere in the
    # app may iterate this list, and not all of them are inside a
    # failure-tolerant surface (see excluded_account_ids)
    if "excluded_accounts" in cfg:
        cfg["excluded_accounts"] = excluded_account_ids(cfg)
    if "food_monthly" in cfg and "other_monthly" in cfg:
        return cfg
    seed = _seed_config(conn, excluded=excluded_account_ids(cfg),
                        today=localtime.now_local(cfg).date())
    cfg = {**seed, **cfg}
    # Don't LOCK IN a zero seed computed before any spend has synced (e.g.
    # load_config called during setup, before the first sync) — that leaves
    # onboarding budgets stuck at 0 forever. Persist only once there's real
    # spend to base them on; until then re-seed each call so the Budgets
    # step reflects the synced data.
    if (seed.get("food_monthly") or 0) > 0 or (seed.get("other_monthly") or 0) > 0:
        # The seed aggregation above is SLOW (a 3-month spend scan), and
        # writing the pre-seed snapshot back would discard any settings save
        # that landed meanwhile — a wizard step un-completed, a just-added
        # recipient gone. Take the row lock, RE-READ, and let
        # the fresh document win; the seed fills only what is still
        # missing. (Not config_txn — that helper calls load_config itself.)
        with conn.transaction():
            # FOR UPDATE locks the rows it MATCHES, and this is the seeding
            # path — by definition a tenant that may have no settings row at
            # all, in which case the lock takes nothing and the re-read
            # below protects nothing — the same gap config_txn closes. Seed
            # the default row first so there is something to lock; every
            # reader treats a missing row and an empty config identically.
            conn.execute("INSERT INTO tenant_settings (config) "
                         "VALUES ('{}'::jsonb) ON CONFLICT (tenant_id) "
                         "DO NOTHING")
            row = conn.execute(
                "SELECT config FROM tenant_settings FOR UPDATE").fetchone()
            fresh = dict(row["config"]) if row else {}
            merged = {**seed, **fresh}
            save_config(conn, merged)
        return merged
    return cfg


@contextlib.contextmanager
def config_txn(conn, *, skip_unchanged: bool = True):
    """Read-modify-write the settings blob under a row lock.

        with budget.config_txn(conn) as cfg:
            cfg["excluded_accounts"] = [...]

    `load_config` + mutate + `save_config` writes the WHOLE document back, so
    two writers that each read before the other wrote silently discard one
    set of changes — the settings page saving while the nightly worker seeds
    budgets, or two tabs. Taking the row lock first makes the pair
    atomic.

    Deliberately opt-in rather than folded into `load_config`: that is called
    on nearly every request for READING, and locking a row per read to protect
    the rare write would be a much worse trade. Reach for this anywhere the
    pattern is load → change → save.

    The explicit transaction block is load-bearing, not decoration.
    Engine connections are **autocommit** — see tenancy's module docstring —
    so a bare `SELECT ... FOR UPDATE` commits the instant it returns and drops
    the row lock before the caller has read, let alone written — leaving the
    window this helper exists to close exactly as wide as before.

    TENANT CONNECTIONS ONLY. The `SELECT ... FOR UPDATE` below is deliberately
    unqualified and relies on RLS to scope it to the one row; on an
    `admin_connect` (which BYPASSES RLS) the same statement would lock EVERY
    tenant's settings row. A comment does not lock anything, so the rule is
    checked, on the only marker that distinguishes the two:
    `tenant_connect` issues `SET app.tenant_id`, `admin_connect` does not.

    The seeding INSERT is what makes the lock bite. `FOR UPDATE` locks the
    rows it MATCHES, and a tenant that has never saved settings has no row at
    all — so on a brand-new tenant the lock takes nothing, both writers read
    an empty config, and both `INSERT ... ON CONFLICT DO UPDATE` in
    save_config, the second silently discarding the first. That is the exact
    lost-update this helper exists to prevent, otherwise open on the tenants
    most likely to have two writers at once (the setup wizard saving while the
    first sync seeds budgets). Inserting the default row first is a no-op for
    every reader — `load_config`, tenancy and app.py all treat a missing row
    and an empty config identically, and the column defaults produce exactly
    the '{}' they already assume.
    """
    with conn.transaction():
        tid = conn.execute(
            "SELECT NULLIF(current_setting('app.tenant_id', true), '') AS tid"
        ).fetchone()["tid"]
        if not tid:
            raise RuntimeError(
                "config_txn requires a tenant_connect: on an admin connection "
                "the unqualified SELECT ... FOR UPDATE would lock every "
                "tenant's settings row")
        conn.execute("INSERT INTO tenant_settings (config) VALUES ('{}'::jsonb) "
                     "ON CONFLICT (tenant_id) DO NOTHING")
        conn.execute("SELECT config FROM tenant_settings FOR UPDATE")
        cfg = load_config(conn)
        # A SHALLOW-ish snapshot: json round-trip, not deepcopy. This runs
        # inside the row lock, at 28 call sites, on a document with no size
        # bound (recipient lists, MX/Plaid state, budget history all grow),
        # and every other writer to this row waits behind it. json is
        # markedly cheaper than deepcopy for a plain JSON document — which
        # is exactly what this is, since it round-trips through JSONB — and
        # it fails loudly on anything that is NOT JSON-able rather than
        # silently copying it wrong.
        before = json.loads(json.dumps(cfg)) if skip_unchanged else None
        yield cfg
        # A block that changes nothing should not rewrite the whole document
        # — and should not have taken the row lock's write slot to do it,
        # which delays a save that IS changing something. Callers that
        # already know they changed something pass skip_unchanged=False and
        # pay nothing for the comparison.
        if skip_unchanged and cfg == before:
            return
        save_config(conn, cfg)


def save_config(conn, cfg: dict) -> None:
    conn.execute(
        """INSERT INTO tenant_settings (config) VALUES (%s)
           ON CONFLICT (tenant_id) DO UPDATE
           SET config = EXCLUDED.config, updated_at = now()""", (jsonb(cfg),))
    # demoguard caches demo_mode per tenant — every config write
    # goes through here, so this is the one hook that keeps a runtime flip
    # honest (lazy import: same engine→web pattern as netguard callers)
    from ..web import demoguard
    row = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS tid").fetchone()
    demoguard.invalidate(row["tid"] if row and row["tid"] else None)


# ---- month budget snapshots --------------------------------------------
# The settings blob is one mutable document, so a past month judged against
# it is judged against TODAY's budget. These helpers freeze the working
# config per month: the nightly job upserts the CURRENT month only, so the
# last write before the month turns is the budget the month closed under,
# and earlier months are never rewritten. The lenses judge closed months
# against their snapshot; a closed month with no snapshot is shown as net
# income vs spending rather than given a fabricated verdict.


def _freeze_bills(conn, today: dt.date) -> dict:
    """The recurring schedule as it stands, for the month snapshot: the
    active bill rows verbatim (so a closed month can re-run the exact
    matching the live table would have given it) plus the schedule's
    evened-out monthly load (recurring_load's numbers, so the history card
    never re-derives them from rows under a different method)."""
    rows = [{"payee": r["payee"], "amount": r["amount"],
             "merchant": r["merchant"], "category": r["category"],
             "raw": as_dict(r["raw"])}
            for r in conn.execute(
                "SELECT payee, amount, raw, merchant, category FROM bills "
                "WHERE active = 1").fetchall()]
    load = recurring_load(conn, today)
    return {"rows": rows, "bills_monthly": load["bills_monthly"],
            "income_monthly": load["income_monthly"]}


def snapshot_month(conn, today: dt.date) -> bool:
    """Upsert (today.year, today.month)'s snapshot from the live config,
    plus the bill schedule it ran beside (_freeze_bills). Skipped while no
    variable budget is set — a $0/$0 month is a month the budget feature
    wasn't in use, and freezing it would turn the honest "no budget set"
    state into a verdict against zero.

    Stored SCRUBBED (the export's credential/policy scrub): none of the
    dropped keys feed month_status, and duplicating SMTP/API secrets into
    a second table — one the data export ships verbatim — would widen the
    secret surface for nothing. (Lazy import: engine→sync, same pattern
    as save_config's demoguard hook.)"""
    cfg = load_config(conn)
    if not ((cfg.get("food_monthly") or 0) > 0
            or (cfg.get("other_monthly") or 0) > 0):
        return False
    from ..sync.restore import scrub_config
    conn.execute(
        """INSERT INTO budget_snapshots (year, month, config, bills)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (tenant_id, year, month) DO UPDATE
           SET config = EXCLUDED.config, bills = EXCLUDED.bills,
               captured_at = now()""",
        (today.year, today.month, jsonb(scrub_config(cfg)),
         jsonb(_freeze_bills(conn, today))))
    return True


def backfill_snapshots(conn, today: dt.date) -> int:
    """Apply the CURRENT budget to every closed month since the ledger
    began that has no snapshot — the owner's explicit, opt-in version of
    the retroactive judgment the lenses otherwise refuse to fabricate.
    Rows are marked source='backfill' so the history card can say which
    verdicts were opted into after the fact. Never touches existing
    snapshots or the current month; returns the number written."""
    cfg = load_config(conn)
    if not ((cfg.get("food_monthly") or 0) > 0
            or (cfg.get("other_monthly") or 0) > 0):
        return 0
    row = conn.execute("SELECT MIN(date) AS d FROM transactions "
                       "WHERE removed = 0").fetchone()
    if not row or not row["d"]:
        return 0                     # no ledger, nothing to judge
    have = {(r["year"], r["month"]) for r in conn.execute(
        "SELECT year, month FROM budget_snapshots")}
    from ..sync.restore import scrub_config
    blob = jsonb(scrub_config(cfg))
    # the CURRENT schedule, same opt-in retroactivity as the config — the
    # row stays source='backfill' so the card can say the schedule (like
    # the budget) was chosen after the fact
    sched = jsonb(_freeze_bills(conn, today))
    first = row["d"]
    n = 0
    y, m = first.year, first.month
    while (y, m) < (today.year, today.month):
        if (y, m) not in have:
            conn.execute(
                """INSERT INTO budget_snapshots (year, month, config, bills,
                       source)
                   VALUES (%s, %s, %s, %s, 'backfill')
                   ON CONFLICT (tenant_id, year, month) DO NOTHING""",
                (y, m, blob, sched))
            n += 1
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return n


def _snapshot_budgets_numeric(cfg: dict) -> bool:
    """Both variable budgets present and genuinely numeric (bool is an
    int in Python — excluded on purpose)."""
    return all(
        isinstance(cfg.get(k), (int, float))
        and not isinstance(cfg.get(k), bool)
        for k in ("food_monthly", "other_monthly"))


def snapshot_config(conn, year: int, month: int) -> dict | None:
    """The config frozen for (year, month), or None when the month has no
    snapshot (predates the product, or the budget was unset)."""
    row = conn.execute(
        "SELECT config FROM budget_snapshots WHERE year=%s AND month=%s",
        (year, month)).fetchone()
    if row is None:
        return None
    cfg = dict(row["config"])
    # a snapshot always comes through load_config, so these exist and are
    # numbers; treat a malformed row as absent rather than crashing the
    # whole lens — a restored ZIP is the door junk can arrive through,
    # and a "x" here would TypeError month_status's budget math
    if not _snapshot_budgets_numeric(cfg):
        return None
    return cfg


def snapshot_bill_rows(conn, year: int, month: int) -> list | None:
    """The bill rows frozen for (year, month), shaped for _recurring_bills
    /_envelope_bills' rows= override, or None when the snapshot predates
    bill freezing (the caller falls back to the live table). Rows
    are type-checked one by one — a restored ZIP is the door junk arrives
    through, and one malformed row must not cost the month its schedule."""
    row = conn.execute(
        "SELECT bills FROM budget_snapshots WHERE year=%s AND month=%s",
        (year, month)).fetchone()
    sched = row["bills"] if row else None
    if not isinstance(sched, dict) or not isinstance(sched.get("rows"), list):
        return None
    return [r for r in sched["rows"]
            if isinstance(r, dict)
            and isinstance(r.get("payee"), str)
            and isinstance(r.get("amount"), (int, float))
            and not isinstance(r.get("amount"), bool)
            and isinstance(r.get("raw"), dict)
            and isinstance(r.get("merchant"), (str, type(None)))
            and isinstance(r.get("category"), (str, type(None)))]


def snapshot_months(conn, year: int) -> set[int]:
    """Months of `year` that have a budget snapshot."""
    return {r["month"] for r in conn.execute(
        "SELECT month FROM budget_snapshots WHERE year=%s", (year,))}


# paychecks-per-month factor by cadence. Biweekly is ×2 by doctrine (the 2
# extra annual checks read as bonus); weekly follows the same logic at ×4.
CADENCE_MONTHLY = {"weekly": 4, "biweekly": 2, "semimonthly": 2, "monthly": 1}


def income_scenarios(cfg: dict) -> tuple[list[dict], str | None]:
    """Named income scenarios, exactly one active. The legacy
    saving/not-saving pair read-migrates into list form so existing
    config keeps working unchanged."""
    scen = cfg.get("income_scenarios")
    if isinstance(scen, list) and scen:
        return scen, cfg.get("active_scenario")
    save = cfg.get("income_biweekly_saving")
    nosave = cfg.get("income_biweekly_not_saving")
    if save and nosave:
        scen = [{"name": "saving", "take_home": save, "cadence": "biweekly"},
                {"name": "not saving", "take_home": nosave,
                 "cadence": "biweekly"}]
        return scen, ("saving" if cfg.get("retirement_saving")
                      else "not saving")
    return [], None


def active_income_scenario(cfg: dict) -> dict | None:
    scen, active = income_scenarios(cfg)
    if not scen:
        return None
    for s in scen:
        if s.get("name") == active:
            return s
    return scen[0]


def active_monthly_income(cfg: dict) -> float | None:
    """Monthly budgeted income from the active scenario (falls back to the
    static budgeted_income_monthly when no scenarios are set)."""
    s = active_income_scenario(cfg)
    if s and s.get("take_home"):
        factor = CADENCE_MONTHLY.get(str(s.get("cadence") or "biweekly"), 2)
        return round(float(s["take_home"]) * factor, 2)
    return cfg.get("budgeted_income_monthly")


def monthly_income(conn) -> float:
    """monthly-equivalent of every CONFIRMED recurring income stream,
    so a biweekly $2,400 paycheck counts as $5,200/mo. Sums the stored
    monthly_amount (save_bill's canonical _monthly conversion — the same
    figure the Bills page shows) rather than re-deriving cadence math.
    One-time rows (frequency NULL) are not income streams and don't count.
    Used to pre-fill the onboarding 'expected income' field from the streams
    the user approved in the recurring finder."""
    row = conn.execute(
        "SELECT COALESCE(SUM(monthly_amount), 0) AS mi FROM bills "
        "WHERE active = 1 AND type = 'INCOME' AND frequency IS NOT NULL"
    ).fetchone()
    return round(float(row["mi"]), 2)


def _seed_config(conn, excluded: list | None = None, *,
                 today: dt.date | None = None) -> dict:
    """First run: derive bucket budgets from the last 3 full months.
    Honors any excluded_accounts already set (the Accounts 'excl'
    toggle can run before budgets seed), else an excluded investment/side
    account inflates the very first food/other budgets.

    `today` is the household's day — which month counts as in-progress
    decides which three months the seed reads. The caller passes it
    because this runs INSIDE the config load and so cannot ask for the
    config itself."""
    from . import merchant_seed
    today = today or localtime.now_local(None).date()
    start = (today.replace(day=1) - dt.timedelta(days=85)).replace(day=1)
    rows = _spend_rows(conn, start, today.replace(day=1), excluded=excluded)
    recurring = _recurring_bills(conn)
    food = other = 0.0
    for r in rows:
        if _match_recurring(r, recurring):
            continue
        # the seed often runs BEFORE the first categorize pass finishes, so
        # sparse-source bank mechanics (card payments, transfers) are still
        # uncategorized and would count as spend — the very leak the
        # categorize pass guards against. Filter them by TEXT here: without
        # it a sparse-source instance can seed other_monthly at ~2× reality.
        txt = f"{r['payee'] or ''} {r['name'] or ''}"
        if merchant_seed.flow_match(txt) or merchant_seed.skip_llm(txt):
            continue
        if r["is_food"]:
            food += r["amount"]
        else:
            other += r["amount"]
    return {"food_monthly": round(food / 3, -1),
            "other_monthly": round(other / 3, -1),
            "dynamic_variable_budget": True}


# ---- recurring schedule expansion -------------------------------------


def _memo_key(name: str, *parts) -> tuple:
    """Key for the per-request `memo` dict several engine passes share (see
    savings.posted_for_plan). Lists and dicts in the inputs are frozen so
    the key hashes; the key carries every input that changes the answer."""
    def _freeze(v):
        if isinstance(v, dict):
            return tuple(sorted((str(k), _freeze(x)) for k, x in v.items()))
        if isinstance(v, (list, tuple, set)):
            return tuple(_freeze(x) for x in v)
        return v
    return (name,) + tuple(_freeze(p) for p in parts)


def _recurring_bills(conn, caps: dict | None = None,
                     disabled: list | None = None,
                     income: bool = False, *,
                     memo: dict | None = None,
                     rows: list | None = None) -> list[dict]:
    """caps: {payee: N} from 'occurrence_caps' — limit expansions to N/month.
    disabled: config 'disabled_bills' payee list — excluded from the budget
    without touching the bill row.
    income=True loads the INCOME series (amount > 0) instead — same
    occurrence shapes, matched against _inflow_rows so the Bills page
    can mark a paycheck received. Without that, income rows never enter
    the occurrence engine and the received column stays empty no matter
    what posted.
    `memo`: the request-scoped cache — one Today request expands this
    schedule from several engine passes over the same snapshot; the bill
    dicts are never mutated downstream, so sharing them is safe.
    `rows`: a frozen schedule (snapshot_bill_rows) replaces the live table
    — never memoized, since one request can span months with different
    frozen schedules and the key carries no row identity."""
    if rows is not None:
        return _recurring_bills_load(conn, caps, disabled, income, rows)
    key = _memo_key("recurring_bills", caps, disabled, income)
    if memo is not None and key in memo:
        return memo[key]
    out = _recurring_bills_load(conn, caps, disabled, income)
    if memo is not None:
        memo[key] = out
    return out


def _recurring_bills_load(conn, caps, disabled, income,
                          rows=None) -> list[dict]:
    if rows is None:
        rows = conn.execute(
            f"SELECT payee, amount, raw, merchant FROM bills "
            f"WHERE amount {'>' if income else '<'} 0 AND active = 1"
        ).fetchall()
    else:
        rows = [r for r in rows
                if ((r["amount"] or 0) > 0) == income and (r["amount"] or 0)]
    skip = {(p or "").lower() for p in (disabled or [])}
    # the merchants attached to each payee, once for the whole schedule
    ids = bill_merchant_ids(conn, rows)
    out = []
    for r in rows:
        if (r["payee"] or "").lower() in skip:
            continue
        raw = as_dict(r["raw"])
        if raw.get("bill_type") == "envelope":
            continue  # no occurrences — handled by _envelope_bills
        try:
            anchor = as_date(raw.get("dueOn"))
        except (TypeError, ValueError):
            continue
        out.append({
            "merchant_tokens": _tokens(r["merchant"] or ""),
            "merchant_phrase": _merchant_phrase(r["merchant"]),
            "matcher": merchant_matcher(r["merchant"], _key_token(r["payee"] or "")),
            "ids": ids.get(r["payee"] or ""),
            "cap": (caps or {}).get(r["payee"] or ""),
            "payee": r["payee"] or "",
            "tokens": _tokens(r["payee"] or ""),
            "key": _key_token(r["payee"] or ""),
            "amount": abs(r["amount"]),  # positive dollars (out, or in)
            "anchor": anchor,
            # a series with no history (no lastDueOn) starts AT dueOn — never
            # back-project it into earlier dates
            "floor": None if raw.get("lastDueOn") else anchor,
            "recurrence": raw.get("recurrence") or {},
            "show_today": bool(raw.get("show_today")),
            # the fees that ride with the payment — planned on top of the
            # main charge, matched to the same occurrence (bills.companions_of)
            "companions": _companions(raw),
            "fee_total": round(sum(c["amount"] for c in _companions(raw)), 2),
        })
    return [b for b in out if b["merchant_tokens"] or b["key"]]


def _envelope_bills(conn, disabled: list | None = None, *,
                    memo: dict | None = None,
                    rows: list | None = None) -> list[dict]:
    """Envelope bills: merchants whose TOTAL over a period is the predictable
    thing. No occurrences, no due dates — `amount` is a pool, matched spend
    counts against it cap-only; overage overflows to the variable bucket.

    The pool covers `envelope_months` — 1 (classic monthly pool) or
    12 (an annual pool: lumpy yearly costs like domain renewals, where a
    monthly cap would call every renewal month overspending). `monthly` is the
    pool spread over its months — what the plan reserves and the forecast
    drips — and at 1 month it IS the pool. `memo`: request-scoped cache, as on _recurring_bills. `rows`: a
    frozen schedule replaces the live table, memo bypassed as there."""
    if rows is not None:
        return _envelope_bills_load(conn, disabled, rows)
    key = _memo_key("envelope_bills", disabled)
    if memo is not None and key in memo:
        return memo[key]
    out = _envelope_bills_load(conn, disabled)
    if memo is not None:
        memo[key] = out
    return out


def _envelope_bills_load(conn, disabled, rows=None) -> list[dict]:
    if rows is None:
        rows = conn.execute(
            "SELECT payee, amount, raw, merchant, category FROM bills "
            "WHERE amount < 0 AND active = 1").fetchall()
    else:
        rows = [r for r in rows if (r["amount"] or 0) < 0]
    skip = {(p or "").lower() for p in (disabled or [])}
    ids = bill_merchant_ids(conn, rows)
    out = []
    for r in rows:
        if (r["payee"] or "").lower() in skip:
            continue
        raw = as_dict(r["raw"])
        if raw.get("bill_type") != "envelope":
            continue
        months = envelope_months(raw)
        pool = -r["amount"]              # positive dollars out per period
        out.append({
            "payee": r["payee"] or "",
            "key": _key_token(r["payee"] or ""),
            "merchant_tokens": _tokens(r["merchant"] or ""),
            "merchant_phrase": _merchant_phrase(r["merchant"]),
            "matcher": merchant_matcher(r["merchant"], _key_token(r["payee"] or "")),
            "ids": ids.get(r["payee"] or ""),
            "pool": pool,
            "period_months": months,
            "monthly": pool / months,
            "show_today": bool(raw.get("show_today")),
            # category pool: rows of this effective category count toward the
            # envelope even when their merchants share nothing (withdrawals,
            # peer-to-peer payees). Merchant matches keep priority.
            "match_cat": (_cat_norm(r["category"])
                          if raw.get("match_category") and r["category"]
                          else None),
        })
    return [b for b in out if b["merchant_tokens"] or b["key"]]


def _envelope_prior_used(conn, envelopes, today: dt.date,
                         excluded: list | None = None, *,
                         memo: dict | None = None,
                         bills: list | None = None) -> dict[str, float]:
    """Dollars each ANNUAL envelope already drew from this calendar year's
    pool, before the current month.

    Monthly envelopes are never asked: their period IS the month being
    matched, so they always start it empty. First-match-wins mirrors
    `match_envelopes`, so two envelopes claiming one merchant can't both
    count the same row.

    `bills` are the occurrence bills: a row an occurrence claims is the
    bill's, exactly as in the current month, where envelopes only see the
    rows no occurrence matched. Without it a category-matched annual
    envelope absorbs every earlier month's bill charges in its category
    and reports a pool spent by bills that were never its."""
    annual = [e for e in envelopes if e["period_months"] > 1]
    if not annual:
        return {}
    used = {e["payee"]: 0.0 for e in annual}
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    if year_start >= month_start:       # January: the pool is untouched
        return used
    # the year-to-date match is the widest pass on the Today path, and one
    # request runs month_status more than once (the verdict, the forecast,
    # the bills-left tile); the answer depends only on the month, the
    # excluded accounts and the schedules, so it is computed once per
    # request. Copied out so a caller cannot mutate the cached dict.
    key = _memo_key("envelope_prior_used", month_start, excluded,
                    [e["payee"] for e in annual],
                    [(b["payee"], b["amount"]) for b in (bills or [])])
    if memo is not None and key in memo:
        return dict(memo[key])
    # the claimer reads payee/name/amount/category only — the year-to-date
    # window is the widest scan on the Today path, so it skips the display
    # name, logo and account columns the month rows carry for rendering
    rows = _spend_rows(conn, year_start, month_start, excluded=excluded,
                       slim=True, memo=memo)
    claimed: set[int] = set()
    if bills:
        # the same candidate set month_status matches over: the December
        # before as decoys, every month of the year so far, and the
        # current month, so a payment made late in the prior month for
        # this month's occurrence is the bill's too
        occs = month_occurrences(bills, year_start.year - 1, 12)
        y, m = year_start.year, 1
        while (y, m) <= (month_start.year, month_start.month):
            occs += month_occurrences(bills, y, m)
            y, m = (y, m + 1) if m < 12 else (y + 1, 1)
        claimed, _ = match_occurrences(rows, occs)
    for i, r in enumerate(rows):
        if i in claimed:
            continue
        txt = f"{r['payee'] or ''} {r['name'] or ''}".lower()
        toks = _tokens(txt)
        e = _claim_envelope(annual, r, toks, txt)
        if e is not None:
            used[e["payee"]] += r["amount"]
    if memo is not None:
        memo[key] = dict(used)
    return used


def _claim_envelope(envelopes, row, toks, txt):
    """Which envelope claims this row? Merchant matches win over category
    matches (mirrors _claim_custom); within each tier, list order breaks
    ties. One shared claimer so prior-used and the month's matching cannot
    disagree about who owns a row."""
    for e in envelopes:
        if _bill_matches(e, toks, txt, row_merchant_id(row)):
            return e
    rc = _cat_norm(row["category"]) if row.get("category") else ""
    for e in envelopes:
        if e.get("match_cat") and e["match_cat"] == rc:
            return e
    return None


def match_envelopes(rows, envelopes, matched_idx: set[int],
                    prior_used: dict[str, float] | None = None):
    """Assign not-yet-matched rows to envelope bills (cap-only). Rows are
    consumed in DATE order; dollars beyond what's left of the envelope's pool
    stay with the envelope's `overflow` (caller routes them to variable).

    `prior_used` is what an annual envelope already spent earlier in
    the calendar year — the cap binds on the YEAR's pool, not the month's.
    Omit it for monthly envelopes and the cap is the monthly pool, as ever."""
    prior_used = prior_used or {}
    states = [{"bill": e, "used": 0.0, "overflow": 0.0,
               "overflow_food": 0.0, "overflow_other": 0.0, "rows": [],
               "overflow_rows": [],
               "prior_used": prior_used.get(e["payee"], 0.0)}
              for e in envelopes]
    env_matched: set[int] = set()
    order = sorted((i for i in range(len(rows)) if i not in matched_idx),
                   key=lambda i: rows[i]["date"])
    by_bill = {id(st["bill"]): st for st in states}
    bills_in_order = [st["bill"] for st in states]
    for i in order:
        r = rows[i]
        txt = f"{r['payee'] or ''} {r['name'] or ''}".lower()
        toks = _tokens(txt)
        e = _claim_envelope(bills_in_order, r, toks, txt)
        if e is None:
            continue
        st = by_bill[id(e)]
        left = e["pool"] - st["prior_used"] - st["used"]
        take = min(r["amount"], max(0.0, left))
        st["used"] += take
        over = r["amount"] - take
        st["overflow"] += over
        st["overflow_food" if r["is_food"] else "overflow_other"] += over
        # One purchase, two slices, both on the day it HAPPENED: the part
        # inside the cap is the envelope's row (fixed), the part beyond it
        # a variable row. Anything that sums rows by date — the week lens,
        # the today-ledger's spent-today — then sees the purchase once, on
        # its own day — never a full-amount fixed row plus an overflow
        # stamped today, which would count it twice and charge today for an
        # earlier swipe.
        if take > 0.005:
            st["rows"].append(r if over <= 0.005 else {**r, "amount": take})
        if over > 0.005:
            st["overflow_rows"].append({
                # `txn_id` stays None — the overflow slice is not a ledger
                # row, and nothing may treat it as one. But the purchase it
                # was carved from IS a row, and a surface that opens "the
                # rows behind this number" has to open that one, so its id
                # travels under its own name. `stored_category` is cleared for
                # the same reason the label changes: "envelope overflow" is
                # a composed label, not a stored category anyone can filter on.
                **r, "txn_id": None, "overflow_of": r["txn_id"],
                "amount": over,
                "payee": f"{e['payee']} (over envelope)", "name": e["payee"],
                "category": "envelope overflow", "stored_category": None,
                "pending": 0})
        env_matched.add(i)
    return env_matched, states


def _cycle_days(rec: dict) -> int:
    per = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 30, "YEARLY": 365}
    return per.get((rec.get("frequency") or "MONTHLY").upper(), 30) * (rec.get("interval") or 1)


# How far a payment may sit from its occurrence's due date and still
# match. A tight cap would leave a long-cycle (quarterly/annual) bill
# prepaid a few weeks early reserved anyway — double-counting money already
# out of checking. 35 is safe because the window below never exceeds HALF
# the bill's cycle: a payment inside one occurrence's window is always
# strictly nearer to it than to any adjacent occurrence, and the greedy
# nearest-first match (with prior-month decoys) keeps late payments on
# their own occurrence. Monthly and shorter cadences are untouched — their
# half-cycle (15/7/3 days) binds before this cap does.
PREPAY_MATCH_DAYS = 35


def _companions(raw: dict) -> list[dict]:
    from . import bills as _bills          # late: bills imports budget
    return _bills.companions_of(raw)


def month_occurrences(bills: list[dict], year: int, month: int) -> list[dict]:
    """All bill occurrences for the month, unfloored — payments may attach to
    a floored occurrence (proof the series is real); only UNPAID floored
    occurrences are excluded from expectations."""
    occs = []
    for bill in bills:
        window = max(3, min(PREPAY_MATCH_DAYS,
                            _cycle_days(bill["recurrence"]) // 2))
        expanded = _occurrences_in_month(bill, year, month)
        cap = bill.get("cap")
        if cap:
            try:
                cap_n = int(cap)
            except (TypeError, ValueError):
                cap_n = 0
            if cap_n > 0:
                expanded = expanded[:cap_n]
        for d in expanded:
            occs.append({
                "bill": bill, "due": d,
                # what the occurrence costs: the charge plus its fees
                "planned": round(bill["amount"] + bill.get("fee_total", 0), 2),
                "txn": None, "companions": [],
                "window": window,
                "floored": bill["floor"] is not None and d < bill["floor"],
            })
    return occs


def occurrences_in_month(bill: dict, year: int, month: int) -> list[dt.date]:
    """Expand a bill's recurrence onto calendar dates within one month."""
    out = _occurrences_in_month(bill, year, month)
    floor = bill.get("floor")
    return [d for d in out if floor is None or d >= floor]


def _occurrences_in_month(bill: dict, year: int, month: int) -> list[dt.date]:
    rec, anchor = bill["recurrence"], bill["anchor"]
    if not anchor:
        return []
    last_day = calendar.monthrange(year, month)[1]
    first, last = dt.date(year, month, 1), dt.date(year, month, last_day)

    if not rec.get("frequency"):
        # no recurrence = one-time scheduled bill: occurs on its due date only
        return [anchor] if first <= anchor <= last else []

    freq = rec["frequency"].upper()
    try:
        interval = int(rec.get("interval") or 1)
    except (TypeError, ValueError):
        interval = 1
    if interval < 1:
        interval = 1
    end_on = rec.get("endOn")
    if end_on:
        try:
            if dt.date.fromisoformat(str(end_on)[:10]) < first:
                return []
        except (TypeError, ValueError):
            pass

    if freq in ("DAILY", "WEEKLY"):
        step = interval * (7 if freq == "WEEKLY" else 1)
        # project the anchor's phase into this month (works from either side —
        # the anchor is usually the NEXT due date, which may be after `last`)
        anchor = anchor - dt.timedelta(days=((anchor - first).days // step) * step)
        d, out = anchor, []
        while d <= last:
            if d >= first:
                out.append(d)
            d += dt.timedelta(days=step)
        return out

    if freq == "MONTHLY":
        months_diff = (year - anchor.year) * 12 + (month - anchor.month)
        if months_diff % interval:  # phase check only; past/future both valid
            return []
        if rec.get("byDay"):  # nth-weekday pattern, e.g. FR + bySetPos [1,2,3]
            wd = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
            out = []
            for code in _codes(rec.get("byDay")):
                target = wd.get(code)
                if target is None:
                    continue
                first_occ = 1 + (target - dt.date(year, month, 1).weekday()) % 7
                last_occ = last_day - (dt.date(year, month, last_day).weekday() - target) % 7
                for n in _ints(rec.get("bySetPos"), [1, 2, 3, 4, 5]):
                    day = (first_occ + 7 * (n - 1)) if n > 0 else (last_occ + 7 * (n + 1))
                    if 1 <= day <= last_day and (day - first_occ) % 7 == 0:
                        out.append(dt.date(year, month, day))
            return sorted(set(out))
        # raw can arrive verbatim from a restore ZIP — a malformed day
        # list falls back to the anchor, never a crash on every Today load
        days = _ints(rec.get("byMonthDay"), [anchor.day])
        out = []
        for d in days:
            if d < 0:  # iCal convention: -1 = last day of month
                d = last_day + 1 + d
            out.append(dt.date(year, month, min(max(d, 1), last_day)))
        return out

    if freq == "YEARLY":
        months = _ints(rec.get("byMonth"), [anchor.month])
        if month not in months or (year - anchor.year) % interval:
            return []
        return [dt.date(year, month, min(anchor.day, last_day))]

    return []


# ---- custom budget buckets -----------------------------------
# A custom bucket carves spend OUT of a named parent (food | other) for
# display: bars, email sections, reasons. It is a DECOMPOSITION layer only —
# the verdict math (variable actual vs variable budget) never sees it.


RESERVED_BUCKET_NAMES = {"food", "other", "fixed", "variable"}


def _cat_norm(s: str) -> str:
    """Category comparison form: 'FOOD_AND_DRINK', 'FOOD AND DRINK' and
    'Food & Drink' all normalize alike."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def custom_bucket_specs(cfg: dict) -> list[dict]:
    """Validated view of config `custom_buckets`: a list of
    {name, parent, monthly, categories[], merchants[]}. Malformed entries
    are skipped — a hand-edited config must not 500 the Today page."""
    out, seen = [], set()
    for e in cfg.get("custom_buckets") or []:
        if not isinstance(e, dict):
            continue
        name = str(e.get("name") or "").strip()
        parent = e.get("parent")
        if (not name or parent not in ("food", "other")
                or name.lower() in seen
                or name.lower() in RESERVED_BUCKET_NAMES):
            continue
        try:
            monthly = max(0.0, float(e.get("monthly") or 0))
        except (TypeError, ValueError):
            monthly = 0.0
        cats = [str(c).strip() for c in (e.get("categories") or [])
                if str(c).strip()]
        merch = [str(m).strip() for m in (e.get("merchants") or [])
                 if str(m).strip()]
        # A matcher-less bucket is a valid PLAN PLACEHOLDER, and
        # /api/settings accepts one deliberately — the Budget planner's
        # add-a-category row writes name + monthly with no rules at all.
        # Dropping one here would make it vanish: no bar on Today / the
        # month lens / the email, and its dollars left silently inside
        # "Everything else" (which the planner has already sized to INCLUDE
        # them, so the pool is double-counted). A budget is reason enough to
        # exist; the bucket simply claims no rows until categories or
        # merchants are added.
        if not cats and not merch and monthly <= 0:
            continue                    # no rules AND no money — dead weight
        seen.add(name.lower())
        out.append({"name": name, "parent": parent, "monthly": monthly,
                    "categories": cats, "merchants": merch})
    return out


def _compiled_buckets(specs: list[dict]) -> list[dict]:
    return [{**s,
             "_cats": {_cat_norm(c) for c in s["categories"]},
             "_matchers": [merchant_matcher(m) for m in s["merchants"]]}
            for s in specs]


def _claim_custom(customs: list[dict], row) -> dict | None:
    """Which custom bucket owns this row? Merchant rules win over category
    rules (spec §2 — a merchant match assigns the txn regardless of
    category); within each tier, config order breaks ties."""
    txt = f"{row['payee'] or ''} {row['name'] or ''}"
    for c in customs:
        if any(m(txt) for m in c["_matchers"]):
            return c
    rc = _cat_norm(row["category"])
    for c in customs:
        if rc in c["_cats"]:
            return c
    return None


def _pending_expense_shapes(conn) -> list[dict]:
    """Mid-wizard: pending non-income bill proposals, as coarse matchers so
    suggest_budgets can exclude those merchants from food/other and from
    carve-out candidates (same as approved bills)."""
    out = []
    for p in conn.execute(
            """SELECT payee, amount, evidence FROM bill_proposals
               WHERE status = 'pending' AND kind = 'add'""").fetchall():
        ev = as_dict(p["evidence"]) or {}
        if ev.get("income"):
            continue
        payee = p["payee"] or ""
        key = _key_token(payee)
        if not key and not _tokens(payee):
            continue
        out.append({
            "merchant_tokens": set(),
            "merchant_phrase": None,
            "matcher": merchant_matcher(None, key),
            "cap": None,
            "payee": payee,
            "tokens": _tokens(payee),
            "key": key,
            "amount": abs(float(p["amount"] or 0)),
            "anchor": None,
            "floor": None,
            "recurrence": {},
            "pending": True,
        })
    return out


def _pending_expense_monthly(conn) -> tuple[float, int]:
    """Monthly-equivalent load of pending expense proposals (wizard seed)."""
    from .bills import _monthly
    total = 0.0
    n = 0
    for p in conn.execute(
            """SELECT amount, frequency, "interval", evidence
               FROM bill_proposals
               WHERE status = 'pending' AND kind = 'add'""").fetchall():
        if (as_dict(p["evidence"]) or {}).get("income"):
            continue
        total += _monthly(abs(float(p["amount"] or 0)),
                          p["frequency"], p["interval"] or 1)
        n += 1
    return round(total, 2), n


def suggest_budgets(conn, today: dt.date) -> dict:
    """'Suggest from history' (§3): per-bucket trailing ~6 months
    median (including the in-progress month so mid-month onboarding sees
    this month's groceries), outlier months >2× the initial median
    excluded — rounded to $10. Read-only; the user approves each number
    before it lands in config."""
    cfg = load_config(conn)
    customs = _compiled_buckets(custom_bucket_specs(cfg))
    month_start = today.replace(day=1)
    start = month_start
    for _ in range(6):
        start = (start - dt.timedelta(days=1)).replace(day=1)
    # Include today (until exclusive → tomorrow) so MTD food isn't invisible
    # on the 20th when the only grocery runs landed this month.
    rows = _spend_rows(conn, start, today + dt.timedelta(days=1),
                       excluded=excluded_account_ids(cfg))
    bills = _recurring_bills(conn, caps=cfg.get("occurrence_caps"),
                             disabled=cfg.get("disabled_bills"))
    # Wizard mid-flight: treat pending expense proposals like bills so
    # the mortgage/utility payees don't inflate "other" or show up as carve-outs
    bills = list(bills) + _pending_expense_shapes(conn)
    envelopes = _envelope_bills(conn, disabled=cfg.get("disabled_bills"))

    def _month_key(d):
        d = as_date(d)
        return f"{d.year:04d}-{d.month:02d}"

    months = []
    m = start
    while m <= month_start:   # include current partial month
        months.append(_month_key(m))
        m = (m + dt.timedelta(days=32)).replace(day=1)
    # months before the ledger starts aren't $0 spending — they're no data
    first = conn.execute("SELECT MIN(date) AS d FROM transactions "
                         "WHERE removed = 0").fetchone()
    if first and first["d"]:
        first_key = _month_key(first["d"])
        months = [mk for mk in months if mk >= first_key]

    # Envelopes join the same merchant-exclude set
    bill_shaped = list(bills) + list(envelopes)

    keys = ["food", "other"] + [c["name"] for c in customs]
    totals = {k: {mk: 0.0 for mk in months} for k in keys}
    for r in rows:
        mk = _month_key(r["date"])
        if mk not in months:
            continue
        # Anything the Bills line already owns drops out of variable
        # estimates (merchant match only — see _match_bill_shaped).
        if _match_bill_shaped(r, bill_shaped):
            continue
        c = _claim_custom(customs, r)
        key = c["name"] if c else ("food" if r["is_food"] else "other")
        totals[key][mk] += r["amount"]

    def _median(vals: list[float]) -> float:
        vs = sorted(vals)
        n = len(vs)
        if not n:
            return 0.0
        mid = n // 2
        return vs[mid] if n % 2 else (vs[mid - 1] + vs[mid]) / 2

    def _suggest_monthly(vals: list[float]) -> float:
        """Median of months, dropping >2× outliers. Sparse categories
        (food often lands in 1–2 months of a 6-month window) otherwise
        collapse to $0: either `v <= 2*0` keeps only zeros, or a low median
        treats the real grocery month as a >2× outlier. When at least half
        the months are empty, median the active months only."""
        if not vals:
            return 0.0
        nz = [v for v in vals if v > 0]
        if not nz:
            return 0.0
        med0 = _median(vals)
        if med0 <= 0 or len(nz) <= len(vals) // 2:
            return round(_median(nz), -1)
        kept = [v for v in vals if v <= 2 * med0]
        sug = _median(kept if kept else vals)
        if sug <= 0:                       # filter wiped the signal
            return round(_median(nz), -1)
        return round(sug, -1)

    suggestions: dict = {"buckets": {}}
    samples: dict = {}
    for k in keys:
        vals = [totals[k][mk] for mk in months]
        suggestion = _suggest_monthly(vals)
        if k == "food":
            suggestions["food_monthly"] = suggestion
        elif k == "other":
            suggestions["other_monthly"] = suggestion
        else:
            suggestions["buckets"][k] = suggestion
        samples[k] = [[mk, round(totals[k][mk], 2)] for mk in months]
    # seed the expected-income field from confirmed recurring income;
    # when nothing is approved yet (mid-wizard), fall back to the PENDING
    # income proposals — the finder already proved their cadence, the user
    # just hasn't clicked approve, and a blank field reads as "we found
    # nothing" when we did
    inc = monthly_income(conn)
    if inc <= 0:
        from .compat import as_dict
        from .bills import _monthly
        inc = round(sum(
            _monthly(abs(p["amount"]), p["frequency"], p["interval"] or 1)
            for p in conn.execute(
                """SELECT amount, frequency, "interval", evidence
                   FROM bill_proposals
                   WHERE status = 'pending' AND kind = 'add'""").fetchall()
            if (as_dict(p["evidence"]) or {}).get("income")), 2)
    if inc > 0:
        suggestions["income_monthly"] = inc
    # carve-out candidates for the wizard — top non-food
    # categories by monthly median (bill rows already excluded above),
    # floored (a <$75/mo bucket is noise), skipping categories an existing
    # custom bucket already claims
    claimed = {c for b in customs for c in (b.get("categories") or [])}
    by_cat: dict[str, dict[str, float]] = {}
    for r in rows:
        mk = _month_key(r["date"])
        if mk not in months or r["is_food"] or _match_bill_shaped(r, bill_shaped):
            continue
        cat = (r["category"] or "?").replace("_", " ")
        by_cat.setdefault(cat, {m2: 0.0 for m2 in months})[mk] += r["amount"]
    candidates = []
    for cat, per_month in by_cat.items():
        if cat in ("?", "OTHER") or cat.upper() in claimed \
                or cat.replace(" ", "_").upper() in claimed:
            continue
        # Same sparse-month logic as food/other — don't zero a real category
        med = _suggest_monthly(list(per_month.values()))
        if med >= 75:
            candidates.append({"name": cat.title(),
                               "category": cat.replace(" ", "_").upper(),
                               "monthly": med})
    candidates.sort(key=lambda c: -c["monthly"])
    # monthly bill load for the savings-surplus prefill — same
    # evened-out year_total/12 the month plan uses. Approved bills only for
    # the occurrence engine (pending rows lack recurrence anchors); when
    # none are approved yet (mid-wizard), fall back to pending proposals'
    # monthly-equivalent — same pattern as income_monthly above.
    approved = [b for b in bills if not b.get("pending")]
    year_total = sum(o["planned"] for m2 in range(1, 13)
                     for o in month_occurrences(approved, today.year, m2)
                     if not o["floored"])
    year_total += sum(e["monthly"] for e in envelopes) * 12
    avg_bills = round(year_total / 12, 2)
    bills_count = len(approved) + len(envelopes)
    if avg_bills <= 0:
        pend_mo, pend_n = _pending_expense_monthly(conn)
        if pend_mo > 0:
            avg_bills = pend_mo
            bills_count = pend_n
    return {"months": months, "suggestions": suggestions, "samples": samples,
            "bucket_candidates": candidates[:6],
            "avg_bills_monthly": avg_bills,
            "bills_count": bills_count}


# ---- transaction classification ---------------------------------------


def envelope_months(raw: dict) -> int:
    """An envelope's pool length, 1 or 12. `raw` can arrive verbatim from
    a restore ZIP, so anything that is not a number reads as the month —
    never a crash on every Today load."""
    try:
        return 12 if int(raw.get("envelope_months") or 1) >= 12 else 1
    except (TypeError, ValueError):
        return 1


def _ints(seq, default: list[int]) -> list[int]:
    """A recurrence list (byMonthDay/byMonth/bySetPos) as ints, dropping
    what does not parse; the default when nothing survives."""
    if not isinstance(seq, (list, tuple)):
        return default
    out = []
    for v in seq:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out or default


def _codes(seq) -> list[str]:
    """A byDay list as weekday codes — strings only; anything else (a
    restore-planted dict, a number) is dropped rather than looked up."""
    if not isinstance(seq, (list, tuple)):
        return []
    return [str(c) for c in seq if isinstance(c, str)]


# Function words, for the short-token fallback only: two letters that carry
# no identity even when they are all a string has left.
_SHORT_FILLER = {"of", "at", "on", "in", "to", "by", "or", "as", "is", "it",
                 "an", "my", "no", "so", "up", "we", "he", "me", "be", "do",
                 "go", "if", "st", "us"}


# An apostrophe JOINS the letters around it; it is never a word break.
# Which spelling reaches us is an accident of the source — a person types
# the bill as "Juniper's Market", a feed resolves the payee the same way,
# and the card line for the very same purchase prints "JUNIPERS MARKET".
# Broken on, the possessive yields "juniper" and the bank's yields
# "junipers", so a bill and the charge that pays it share no word and the
# bill reads as unpaid. Deleted, both are "junipers". It is the reading the
# string clean takes (merchant_dedup._APOSTROPHE) and the one identity
# compares descriptors under, so a name means the same words everywhere.
#
# Only an apostrophe that FOLLOWS a letter or digit joins: a leading one is
# a quote mark in front of a word, and the break it sits on is real. All
# three characters a feed writes it with count. Applied to LOWERED text,
# here and in SQL (apostrophes_joined_sql), which is what keeps the two
# spellings of this rule the same rule.
_APOSTROPHE = re.compile(r"(?<=[a-z0-9])['\u2019\u02bc]")


def join_apostrophes(low: str) -> str:
    """`low` (already lowercased) with its letter-joining apostrophes
    deleted."""
    return _APOSTROPHE.sub("", low)


def apostrophes_joined_sql(expr: str) -> str:
    """join_apostrophes, in SQL, over an expression that is already
    lowercased."""
    return f"regexp_replace({expr}, '([a-z0-9])[''\u2019\u02bc]', '\\1', 'g')"


def _match_words(text: str) -> list[str]:
    """The words a bill and a ledger row are matched on.

    Four letters and up — a floor that stops 'card'-class filler proving a
    match — with a fallback to the SHORT words when a string has no long
    word at all. Without the fallback a payee spelled in initials ('AT&T',
    'CVS', 'IRS', 'DMV') yields no match token whatever, so its bill can
    never find the rows that pay it and reads as unpaid forever. Both sides
    fall back independently, and only when they have nothing else, so a
    payee with a real word still never matches on 'of' or 'at'."""
    low = join_apostrophes((text or "").lower())
    long_ = [t for t in re.findall(r"[a-z]{4,}", low) if t not in STOP_TOKENS]
    if long_:
        return long_
    short = [t for t in re.findall(r"[a-z]{2,}", low)
             if t not in STOP_TOKENS and t not in _SHORT_FILLER]
    if short:
        return short
    # last resort for a name punctuation has shredded: 'AT&T' tokenizes to
    # 'at' + 't', both worthless, while the ledger spells the same payee
    # 'ATT'. Collapsing to letters recovers it, and only ever runs on a
    # string that produced no usable word at all.
    letters = re.sub(r"[^a-z]", "", low)
    return [letters] if len(letters) >= 3 else []


def _tokens(text: str) -> set[str]:
    return set(_match_words(text))


def _key_token(payee: str) -> str | None:
    """The bill's distinctive lead token ('maplehurst', 'city', 'northern').
    Fallback matcher for bills whose canonical merchant hasn't been derived."""
    words = _match_words(payee)
    return words[0] if words else None


def _match_tokens(bill: dict) -> set:
    mt = bill.get("merchant_tokens")
    if mt:
        return mt
    key = bill.get("key")
    return {key} if key else set()


def _text_norm(text: str) -> str:
    """Lowercased, alnum-separated, word-boundary-padded form for phrase tests
    (so 'go mobile' matches 'GO MOBILE' but not 'cargo mobile' or 't-mobile')."""
    return " " + re.sub(r"[^a-z0-9]+", " ",
                        join_apostrophes((text or "").lower())).strip() + " "


def _merchant_phrase(merchant: str | None) -> str | None:
    """A word-boundary-padded phrase to SUBSTRING-match, used when a merchant's
    LEADING word is a too-short-to-tokenize disambiguator ('go mobile').
    Only a dropped FIRST word triggers phrase mode — dropped trailing junk
    (order codes, TLDs) must NOT."""
    if not merchant:
        return None
    words = [w for w in re.split(r"[^a-z0-9]+",
                                 join_apostrophes(merchant.lower())) if w]
    if words and words[0] not in _tokens(merchant):   # leading word was dropped
        return " " + " ".join(words) + " "
    return None


def merchant_matcher(merchant: str | None, key: str | None = None,
                     ids=None):
    """Precompiled predicate(text, tokens=None, display=None)->bool for a
    bill's merchant. Three text forms:
      • alternatives  'northwind|northwind energy' — ANY alternative
                      matches (rebrands)
      • phrase        'go mobile' — word-boundary substring
      • tokens        else — token-subset of the merchant (or the lead key)

    `tokens` is the caller's already-computed `_tokens(text)`: every
    matching loop tests one row against every bill, so tokenizing inside
    the predicate would redo the same split once per bill per row (the bulk
    of the Bills page's time). Callers that hold the row's tokens pass them;
    the predicate derives them only when they are not supplied.

    `ids` is the bill's merchant IDENTITY — the ids of the merchants
    attached to it (bill_merchant_ids: raw.merchant_refs resolved to the
    live rows). When a bill has an identity that identity is the whole
    rule: a row is the bill's exactly when its merchant_id is one of
    them, whatever its letters say — the bank renames a payee ("Northwind
    Headquarters" becomes "Northwind" once the aggregator resolves the
    entity), the person merges two spellings on the Merchants page, and
    the bill follows the merchant. `None` means the bill has no identity
    and text is the rule; an EMPTY set is an identity that resolves to
    nothing here and matches nothing. bill_displays is what proposes an
    identity; the person attaches one."""
    base = _text_matcher(merchant, key)
    if ids is None:
        return base
    idset = frozenset(str(i) for i in ids if i)
    # an attached identity is the WHOLE rule: the bill's charges are the
    # rows of its merchants, not any row whose letters resemble them — and
    # an identity that resolves to nothing here matches nothing, never
    # the letters again
    return lambda text, tokens=None, merchant_id=None: (
        merchant_id is not None and str(merchant_id) in idset)


def _text_matcher(merchant: str | None, key: str | None):
    """merchant_matcher's text forms alone (no identity)."""
    if merchant and "|" in merchant:
        subs = [_text_matcher(alt.strip(), None)
                for alt in merchant.split("|") if alt.strip()]
        return lambda text, tokens=None, merchant_id=None: any(
            s(text, tokens) for s in subs)
    phrase = _merchant_phrase(merchant)
    if phrase:
        return lambda text, tokens=None, merchant_id=None: phrase in _text_norm(text)
    toks = _tokens(merchant) if merchant else ({key} if key else set())
    if not toks:
        return lambda text, tokens=None, merchant_id=None: False
    return lambda text, tokens=None, merchant_id=None: toks <= (
        _tokens(text) if tokens is None else tokens)


def row_merchant_id(r) -> str | None:
    """The merchant row a ledger row belongs to, as the matching passes
    carry it (`merchant_id`, text), or None for a row no merchant claims
    or a plain sequence row."""
    try:
        mid = r.get("merchant_id")
    except AttributeError:
        return None
    return str(mid) if mid else None


def merchant_claim_sql(merchant: str | None,
                       key: str | None = None) -> tuple[str, list]:
    """merchant_matcher's three forms as a predicate over ONE merchant
    NAME, precomputed as `runs` (its maximal letter runs, lowercased, a
    text[]) and `norm` (its _text_norm form): alternatives OR, phrase as
    a substring of norm, tokens as array containment. The same decision
    tokens_match_sql makes with a regex per token — a token is a maximal
    letter run — without the regex, so a few thousand names can be tested
    against every bill in one statement. 'FALSE' where the Python form
    never matches."""
    if merchant and "|" in merchant:
        frags, params = [], []
        for alt in merchant.split("|"):
            if alt.strip():
                f, p = merchant_claim_sql(alt.strip(), None)
                frags.append(f)
                params += p
        return ("(" + " OR ".join(frags) + ")") if frags else "FALSE", params
    phrase = _merchant_phrase(merchant)
    if phrase:
        return "strpos(norm, %s) > 0", [phrase]
    toks = _tokens(merchant) if merchant else ({key} if key else set())
    if not toks:
        return "FALSE", []
    return "%s::text[] <@ runs", [sorted(toks)]


def bill_tolerance(amount) -> float:
    """How far a charge may sit from the bill's amount and still be its
    payment: a quarter of the bill, with a floor for the small bills whose
    quarter is pocket change — but a floor no wider than half the bill,
    so a $15 subscription does not take every $40 charge at a busy payee.
    $15 → $7.50, $60 → $30, $200 → $50."""
    a = abs(float(amount or 0))
    return max(min(30.0, a * 0.5), a * 0.25)


def bill_refs(raw) -> list[dict]:
    """A bill's attached merchants as stored: [{"id": merchant id or None,
    "name": display name}], from raw.merchant_refs (id + name) or the
    older raw.merchant_names (names only)."""
    raw = as_dict(raw) if raw is not None else {}
    refs = raw.get("merchant_refs")
    if isinstance(refs, list):
        out = []
        for r in refs:
            if isinstance(r, dict) and isinstance(r.get("name"), str) and r["name"]:
                rid = r.get("id")
                out.append({"id": str(rid) if rid else None, "name": r["name"]})
        return out
    return [{"id": None, "name": n} for n in (raw.get("merchant_names") or [])
            if isinstance(n, str) and n]


def _resolve_lookup(conn, refs: list[dict]) -> tuple[dict, dict]:
    """The two tables resolve_refs reads: start id -> (live id, current
    name) for every id that resolves (a merge chain followed to its
    survivor), and lower(name) -> live id for every name that names a
    live merchant here (an aggregator-backed row first)."""
    ids = sorted({r["id"] for r in refs if r.get("id")})
    current: dict[str, tuple[str, str]] = {}
    if ids:
        for r in conn.execute(
                """WITH RECURSIVE chain AS (
                       SELECT id::text AS start, id, merged_into, name, 0 AS hop
                         FROM merchants WHERE id::text = ANY(%s)
                       UNION ALL
                       SELECT c.start, m.id, m.merged_into, m.name, c.hop + 1
                         FROM chain c JOIN merchants m ON m.id = c.merged_into
                        WHERE c.hop < 16)
                   SELECT DISTINCT ON (start) start, id::text AS id, name
                     FROM chain ORDER BY start, hop DESC""", (ids,)).fetchall():
            current[r["start"]] = (r["id"], r["name"])
    names = sorted({r["name"].lower() for r in refs
                    if not r.get("id") or r["id"] not in current})
    by_name: dict[str, str] = {}
    if names:
        for r in conn.execute(
                """SELECT DISTINCT ON (lower(name)) lower(name) AS lname, id::text AS id
                     FROM merchants WHERE merged_into IS NULL
                      AND lower(name) = ANY(%s)
                    ORDER BY lower(name), (plaid_entity_id IS NULL), created_at""",
                (names,)).fetchall():
            by_name[r["lname"]] = r["id"]
    return current, by_name


def _resolve_one(r: dict, current: dict, by_name: dict) -> dict:
    rid, name = r.get("id"), r["name"]
    if rid and rid in current:
        rid, name = current[rid]
    else:
        rid = by_name.get(name.lower())
    return {"id": rid, "name": name}


def resolve_refs(conn, refs: list[dict]) -> list[dict]:
    """The same refs with every id that resolves replaced by the LIVE
    merchant it names today — its current name, the survivor of a merge
    chain — and every nameless-id ref given the id of the live merchant
    of that exact name, when one exists. An id no merchant here answers
    to (a bill mirrored or restored from another instance) keeps its name
    and no id. Deduplicated on the live id (else the name), sorted by
    name. Pure: writes nothing."""
    current, by_name = _resolve_lookup(conn, refs)
    out, seen = [], set()
    for r in refs:
        rr = _resolve_one(r, current, by_name)
        key = rr["id"] or rr["name"].lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(rr)
    return sorted(out, key=lambda r: r["name"])


def _refs_by_bill(conn, bills) -> dict[str, list]:
    out: dict[str, list] = {}
    need = []
    for b in bills:
        payee = b["payee"] or ""
        if not payee or payee in out:
            continue
        try:
            raw = as_dict(b["raw"])
        except (KeyError, TypeError):
            raw = None
        if raw is None:
            need.append(payee)
            out[payee] = None
            continue
        out[payee] = (raw, bill_refs(raw))
    if need:
        for r in conn.execute(
                "SELECT payee, raw FROM bills WHERE payee = ANY(%s)",
                (need,)).fetchall():
            raw = as_dict(r["raw"])
            out[r["payee"]] = (raw, bill_refs(raw))
    return {k: v for k, v in out.items() if v is not None}


def bill_merchant_ids(conn, bills) -> dict[str, frozenset | None]:
    """Each bill's identity as the matcher reads it: payee -> the ids of
    the live merchants its refs resolve to (resolve_refs: an id follows a
    merge chain to its survivor; a name-only ref — a bill mirrored or
    restored from another instance — takes the id of the merchant of that
    exact name here, else nothing). None for a bill with NO identity — no
    refs, nothing pinned, nothing offered — which matches by text; a
    frozenset otherwise, empty included: a person's emptied list, or refs
    no merchant here answers to, is an identity that matches nothing,
    never the letters. Two queries for a whole schedule.

    `merchant_offered` is the third way to have an identity that is empty:
    the nightly found the ledger's only offer to be one the bill may not
    take silently and put it to the person instead (bills.attachable,
    reconcile_bill_merchants). Until they answer, the bill matches nothing
    rather than falling back to text — the letters would take the very
    rows the offer is asking about."""
    by = _refs_by_bill(conn, bills)
    all_refs = [r for raw, refs in by.values() for r in refs]
    current, by_name = _resolve_lookup(conn, all_refs) if all_refs else ({}, {})
    out: dict[str, frozenset | None] = {}
    for payee, (raw, refs) in by.items():
        if (not refs and not raw.get("merchant_pinned")
                and not raw.get("merchant_offered")):
            out[payee] = None
            continue
        out[payee] = frozenset(
            rr["id"] for rr in (_resolve_one(r, current, by_name) for r in refs)
            if rr["id"])
    return out


def bill_merchants(conn, bills) -> dict[str, frozenset]:
    """Each bill's attached merchants by their CURRENT display names —
    what the clients list and what discovery compares against. Matching
    reads bill_merchant_ids, not this."""
    by = _refs_by_bill(conn, bills)
    all_refs = [r for raw, refs in by.values() for r in refs]
    current, by_name = _resolve_lookup(conn, all_refs) if all_refs else ({}, {})
    return {payee: frozenset(_resolve_one(r, current, by_name)["name"] for r in refs)
            for payee, (raw, refs) in by.items()}


def bill_displays(conn, bills, today: dt.date | None = None,
                  *, lookback_days: int = 400) -> dict[str, frozenset]:
    """DISCOVERY: the merchants the ledger says each bill pays — payee ->
    the display names under which the ledger files the rows the bill's
    words claim. Not a matcher: bills match by their ATTACHED merchants
    (bill_merchants); this is what a new bill's identity is bootstrapped
    from, and what the nightly pass turns into attach proposals when a
    payee gains a name (a rename, a merge). One statement for every bill
    at once, decided in the database over the recent window's DISTINCT
    merchant keys — only (bill, name) pairs come back, never the ledger —
    because identity is what the payee looks like now, and a name it wore
    years ago cannot claim today's rows.

    The rule that keeps this from offering a bill a stranger: a display
    name is the bill's when the bill's words name a whole raw MERCHANT KEY
    under it — the aggregator's merchant string (else the bare descriptor),
    so every row of that key — AND that key is filed under the name on
    someone's authority: a person's rename or merge, or the aggregator's
    entity. A rename is exactly that shape: the old spelling is a key the
    bill names entirely, and the person (or Plaid) filed it under the
    merchant's new name. A processor's note in the descriptor is not:
    "SMASH BURGER … GOOGLE PAY" is the Smashburger key, whose name says
    nothing of Google, so a Google bill is never offered Smashburger's rows
    however few they are. Keys grouped only by the string clean or a
    reviewed map carry no such authority, so there the bill's keys must be
    at least half the name's rows — one stray descriptor folded into a
    stranger's name must not hand the bill the stranger's whole history.
    A display name equal to the payee itself is the bill's outright.

    `bills` is any iterable of rows/dicts with `payee` and `merchant`."""
    today = today or localtime.household_day(conn)
    out: dict[str, set] = {}
    frags, params, payees = [], [], []
    for b in bills:
        payee = b["payee"] or ""
        if not payee or payee in out:
            continue
        out[payee] = set()
        frag, p = merchant_claim_sql(b["merchant"], _key_token(payee))
        # the payee's own name is a claim on its bucket, whatever the text
        frags.append(f"({frag} OR lower(display) = %s)")
        params += [*p, payee.lower()]
        payees.append(payee)
    if not payees:
        return {}
    hits = ", ".join(frags)
    # k: rows per (display name, merchant key) with each bill's claim on
    # the key — a function of the key's name alone, so decided once per
    # key; b: rows per display name; the join asks, per bill and name,
    # whether the keys the bill names are its on authority, else carry at
    # least half the name's rows
    rows = conn.execute(
        f"""WITH t AS (
                SELECT t.merchant_id, t.merchant_outlet, t.merchant_name,
                       t.name, count(*) AS n
                  FROM transactions t
                 WHERE t.removed = 0 AND t.date >= %s
                 GROUP BY 1, 2, 3, 4),
            k0 AS (
                SELECT {merchant_sql.DISPLAY_MERCHANT} AS display,
                       {apostrophes_joined_sql(
                           f"lower({merchant_sql.RAW_KEY})")} AS raw_key,
                       sum(t.n) AS n,
                       -- filed under the name by a person or by Plaid,
                       -- not merely grouped there by the string clean
                       bool_or(mc.method = 'manual'
                               OR mm.name_source = 'manual'
                               OR mm.plaid_entity_id IS NOT NULL) AS auth
                  FROM t {merchant_sql.MC_JOIN}
                 GROUP BY 1, 2),
            k AS (
                SELECT display, n, auth, ARRAY[{hits}]::boolean[] AS hits
                  FROM (SELECT display, n, auth,
                               regexp_split_to_array(btrim(regexp_replace(
                                   raw_key, '[^a-z]+', ' ', 'g')), ' ') AS runs,
                               ' ' || btrim(regexp_replace(
                                   raw_key, '[^a-z0-9]+', ' ', 'g')) || ' ' AS norm
                          FROM k0 WHERE display IS NOT NULL) x),
            b AS (SELECT display, sum(n) AS bucket_n FROM k GROUP BY 1)
            SELECT g.i, k.display
              FROM k JOIN b USING (display),
                   generate_series(1, %s) AS g(i)
             GROUP BY g.i, k.display, b.bucket_n
            HAVING bool_or(k.hits[g.i] AND k.auth)
                OR sum(k.n) FILTER (WHERE k.hits[g.i]) * 2 >= b.bucket_n""",
        [today - dt.timedelta(days=lookback_days), *params, len(payees)]
    ).fetchall()
    for r in rows:
        out[payees[r["i"] - 1]].add(r["display"])
    return {k: frozenset(v) for k, v in out.items()}


def text_matches_merchant(text: str, merchant: str | None,
                          key: str | None = None) -> bool:
    return merchant_matcher(merchant, key)(text)


# ---- the same matches, spelled for the database ---------------------------
# A matcher that exists only in Python forces every candidate row through the
# app before anything can be decided about it. That is fine where the rows are
# already in hand (the Bills page holds one month of ledger), and ruinous where
# the question is "which of the ledger's rows are this merchant?" — asked in
# Python, that reads the WHOLE ledger and re-tokenizes every descriptor once
# per request. These fragments answer the same question in SQL, so the caller
# can narrow first and tokenize only what survives. They live beside the Python
# forms deliberately: the two must agree, and the cheapest way to keep them
# agreeing is to make them impossible to read apart.

# The text a merchant match reads off a ledger row, and there is ONE of it:
# the row's identity key (merchant_sql.RAW_KEY — the fuel-arm outlet when
# one was identified, else the aggregator's merchant name, else the bank
# line) followed by the bank line, lowercased. Every loader that feeds a
# bill matcher selects MATCH_PAYEE_SQL as its payee and builds the text
# with match_text; every SQL pre-filter reads MATCH_TEXT_SQL. They are the
# same string by construction, which is the whole requirement: a
# pre-filter is only a pre-filter while it admits every row the Python
# matcher would, and two pages only agree about a bill while they read
# its rows the same way. specs/bill-match-text.md has the reasoning, and
# tests/test_bill_match_text_is_one_definition.py holds the line.
# Callers alias transactions as `t`.
MATCH_PAYEE_SQL = merchant_sql.RAW_KEY
MATCH_TEXT_SQL = apostrophes_joined_sql(
    f"lower(COALESCE({MATCH_PAYEE_SQL}, '') || ' ' || COALESCE(t.name, ''))")


def match_text(payee, name) -> str:
    """MATCH_TEXT_SQL, in Python: `payee` is the row's MATCH_PAYEE_SQL
    column, `name` its bank line. Apostrophes joined, as every reader of
    this text joins them (see _APOSTROPHE)."""
    return join_apostrophes(f"{payee or ''} {name or ''}".lower())


# _text_norm, in SQL: alnum-separated and word-boundary padded, so a phrase
# test is a plain substring test that still respects word edges.
MATCH_NORM_SQL = (f"' ' || btrim(regexp_replace({MATCH_TEXT_SQL}, "
                  "'[^a-z0-9]+', ' ', 'g')) || ' '")


def tokens_match_sql(toks: set[str]) -> tuple[str, list]:
    """_tokens' subset test, in SQL: every required token present.

    A token is a MAXIMAL run of four or more ASCII letters, which is what
    keeps "care" out of "healthcare" — so each token is required with its
    edges, never as a bare substring. Tokens are letters only, so they
    carry no regex metacharacters."""
    if not toks:
        return "FALSE", []
    frag = " AND ".join(f"{MATCH_TEXT_SQL} ~ %s" for _ in toks)
    return (f"({frag})" if len(toks) > 1 else frag,
            [f"(^|[^a-z]){t}([^a-z]|$)" for t in sorted(toks)])


def merchant_match_sql(merchant: str | None,
                       key: str | None = None) -> tuple[str, list]:
    """merchant_matcher's three forms as a SQL predicate — the same
    alternatives / phrase / token-subset decision, on the same inputs, so a
    bill payee's family is matched identically whether the test runs in
    Python (row by row) or in the database (once).
    'FALSE' where the Python form returns a never-matching predicate."""
    if merchant and "|" in merchant:
        frags, params = [], []
        for alt in merchant.split("|"):
            if alt.strip():
                f, p = merchant_match_sql(alt.strip(), None)
                frags.append(f)
                params += p
        return ("(" + " OR ".join(frags) + ")") if frags else "FALSE", params
    phrase = _merchant_phrase(merchant)
    if phrase:
        return f"strpos({MATCH_NORM_SQL}, %s) > 0", [phrase]
    return tokens_match_sql(_tokens(merchant) if merchant
                            else ({key} if key else set()))


def _bill_matches(bill: dict, txn_tokens: set, txn_text: str = "",
                  merchant_id: str | None = None) -> bool:
    """Does this row pay this bill? A bill with an identity (`ids`, a set
    of merchant ids — possibly empty) is paid by those merchants' rows
    and nothing else (see merchant_matcher); a bill with none (`ids` is
    None) falls back to the text forms."""
    ids = bill.get("ids")
    if ids is not None:
        return merchant_id is not None and str(merchant_id) in ids
    matcher = bill.get("matcher")
    if matcher is not None:
        return matcher(txn_text, txn_tokens)
    phrase = bill.get("merchant_phrase")
    if phrase:
        return phrase in _text_norm(txn_text)
    mt = _match_tokens(bill)
    return bool(mt) and mt <= txn_tokens


def _match_recurring(txn, recurring: list[dict]) -> dict | None:
    """Coarse matcher used only for seeding the config."""
    txn_text = f"{txn['payee'] or ''} {txn['name'] or ''}"
    txn_tokens = _tokens(txn_text)
    for bill in recurring:
        if not _bill_matches(bill, txn_tokens, txn_text, row_merchant_id(txn)):
            continue
        tol = bill_tolerance(bill["amount"])
        if abs(txn["amount"] - bill["amount"]) <= tol:
            return bill
    return None


def _match_bill_shaped(txn, recurring: list[dict]) -> dict | None:
    """Merchant/key match for budget SUGGESTIONS — no amount gate.

    The plan already covers approved (and pending) bills on the Bills line.
    Variable food/other + carve-out candidates must not re-count those
    merchants when the charge drifted (utility seasonality, annual envelope
    tops) or when the bill is an envelope with no fixed amount. Stricter
    amount matching stays on `_match_recurring` for the live month plan."""
    txn_text = f"{txn['payee'] or ''} {txn['name'] or ''}"
    txn_tokens = _tokens(txn_text)
    for bill in recurring:
        if _bill_matches(bill, txn_tokens, txn_text, row_merchant_id(txn)):
            return bill
        key = bill.get("key")
        if key and key in txn_tokens:
            return bill
    return None


def match_occurrences(rows, occurrences) -> tuple[set[int], list]:
    """Assign spend transactions to scheduled bill occurrences, one txn per
    occurrence, best matches first (date proximity, then amount closeness)."""
    candidates = []
    # The text and amount tests depend on the BILL, not the occurrence, and
    # one bill fans out into an occurrence per month of the candidate
    # window (a year of them on the envelope lookback). Test each row
    # against each distinct bill once; the per-occurrence part is only the
    # date gap.
    distinct: dict[int, dict] = {}
    for occ in occurrences:
        distinct.setdefault(id(occ["bill"]), occ["bill"])
    for ridx, r in enumerate(rows):
        txn_text = f"{r['payee'] or ''} {r['name'] or ''}".lower()
        txn_tokens = _tokens(txn_text)
        txn_date = as_date(r["date"])
        txn_mid = row_merchant_id(r)
        fits: dict[int, bool] = {}
        for bid, bill in distinct.items():
            # every token of the bill's canonical merchant must appear in
            # the txn — 'harbor refresh' won't swallow 'Alder Harbor' —
            # unless the ledger already displays the row under the bill's
            # merchant (a rename the words no longer carry)
            fits[bid] = (
                _bill_matches(bill, txn_tokens, txn_text, txn_mid)
                and abs(r["amount"] - bill["amount"])
                <= bill_tolerance(bill["amount"]))
        if not any(fits.values()):
            continue
        for oidx, occ in enumerate(occurrences):
            bill = occ["bill"]
            if not fits[id(bill)]:
                continue
            day_gap = abs((txn_date - occ["due"]).days)
            if day_gap > occ["window"]:
                continue
            candidates.append((day_gap, abs(r["amount"] - bill["amount"]), ridx, oidx))
    candidates.sort()
    matched_rows: set[int] = set()
    used_occ: set[int] = set()
    for _, _, ridx, oidx in candidates:
        if ridx in matched_rows or oidx in used_occ:
            continue
        matched_rows.add(ridx)
        used_occ.add(oidx)
        occurrences[oidx]["txn"] = rows[ridx]
    # Then the fees that ride with each matched charge: a companion row —
    # its words present, its amount fee-sized — landing within the
    # companion's window of the main charge belongs to the SAME occurrence,
    # so the cap and the health see the true cost and the fee never looks
    # like a stray $1 purchase. Unmatched rows only; one row per companion.
    from . import bills as _bills
    for occ in occurrences:
        comps = occ["bill"].get("companions") or []
        if occ["txn"] is None or not comps:
            continue
        occ["companions"] = []
        paid_on = as_date(occ["txn"]["date"])
        for c in comps:
            best = None
            for ridx, r in enumerate(rows):
                if ridx in matched_rows or r["amount"] <= 0:
                    continue
                gap = abs((as_date(r["date"]) - paid_on).days)
                if gap > c["window_days"]:
                    continue
                text = f"{r['payee'] or ''} {r['name'] or ''}".lower()
                if not _bills.companion_matches(c, _tokens(text), r["amount"]):
                    continue
                if best is None or gap < best[0]:
                    best = (gap, ridx)
            if best is not None:
                matched_rows.add(best[1])
                occ["companions"].append(rows[best[1]])
    return matched_rows, occurrences


# display label for a transaction's account: the institution name is prefixed
# only when the account's own name does not already start with it
# ("Northwind Checking 1234" keeps its name, "Everyday 5678" gains one)
ACCT_LABEL_SQL = (
    "TRIM(CASE WHEN LOWER(COALESCE(a.display_name,a.name,'')) LIKE "
    "LOWER(substr(i.institution_name||' ',1,strpos(i.institution_name||' ',' ')-1))||'%%' "
    "THEN COALESCE(a.display_name,a.name,'') "
    "ELSE TRIM(COALESCE(i.institution_name,'')||' '||COALESCE(a.display_name,a.name,'')) END "
    "|| CASE WHEN COALESCE(a.mask,'')!='' THEN ' '||a.mask ELSE '' END)")


def _spend_rows(conn, since: dt.date, until: dt.date,
                excluded: list | None = None, *, slim: bool = False,
                memo: dict | None = None, parts: bool = True) -> list[dict]:
    """excluded: config 'excluded_accounts' account-id list — those accounts'
    transactions are invisible to the budget math (the Accounts page 'excl'
    toggle). Linked shadow accounts (a lower-ranked source of the
    same real-world account) are excluded the same way, computed live so
    failover needs no bookkeeping.

    slim=True returns only the matching columns (payee, name, amount,
    category, date, pending) — for passes that classify rows and never
    render them, so the year-to-date scans skip the merchant, logo and
    account joins. `memo`: request-scoped cache keyed on every input; one
    Today request asks for the same month window from the status, the
    forecast and the runway, and the ledger cannot move inside one request.

    parts=True (the default) fans a hand-split row out into one row per
    part — its category, its share of the amount, the same txn_id and a
    `split_line` — exactly as if the bank had sent that many charges. That
    is what makes the verdict, the buckets, the bill matcher and the Why
    see the groceries half of a mixed receipt as food and the rest as other;
    a reader that wants the charge whole (the anomaly pass asks whether the
    CHARGE was unusual) passes parts=False."""
    key = _memo_key("spend_rows", since, until, excluded, slim, parts)
    if memo is not None and key in memo:
        return memo[key]
    from . import links
    excluded = list(excluded or []) + links.shadow_ids(conn)
    excl_sql = "AND NOT (t.account_id = ANY(%s))" if excluded else ""
    params: tuple = ((since, until, list(excluded)) if excluded
                     else (since, until))
    if parts:
        cat_disp, cat_key = _cats.PART_CAT_DISPLAY, f"COALESCE({_cats.PART_CAT}, '?')"
        amount, food = _cats.part_net(NET_AMOUNT), PART_FOOD_SQL
        split_join, split_line = _cats.SPLIT_JOIN, "sp.line"
    else:
        cat_disp = "REPLACE(COALESCE(t.category_override, t.category_primary,'?'),'_',' ')"
        cat_key = "COALESCE(t.category_override, t.category_primary,'?')"
        amount, food = NET_AMOUNT, FOOD_SQL
        split_join, split_line = "", "NULL::integer"
    if slim:
        cols = f"""t.id AS txn_id, t.date, {amount} AS amount,
                   {merchant_sql.RAW_KEY} AS payee, t.name,
                   t.merchant_id::text AS merchant_id,
                   {cat_disp} AS category,
                   -- `category` is the DISPLAY form (underscores blown out);
                   -- the stored key rides alongside so a surface that turns a
                   -- category label into a ledger filter has the value the
                   -- ledger actually compares against
                   {cat_key} AS stored_category,
                   {food} AS is_food, t.pending,
                   {split_line} AS split_line"""
        joins = split_join
    else:
        cols = f"""t.id AS txn_id, t.date, {amount} AS amount,
                   {merchant_sql.RAW_KEY} AS payee, t.name,
                   t.merchant_id::text AS merchant_id,
                   -- what the LEDGER shows for this row (the merchant row's
                   -- name, else the alias, else the raw) + its logo: the
                   -- Today page and the email render these; `payee` above
                   -- stays raw because bill/occurrence matching is defined
                   -- on the raw descriptor's tokens
                   COALESCE(mm.name, mc.canonical) AS display_payee,
                   {merchant_sql.MERCHANT_LOGO} AS merchant_logo,
                   {cat_disp} AS category,
                   -- the stored key behind the display form above (see slim)
                   {cat_key} AS stored_category,
                   {food} AS is_food, t.pending,
                   {split_line} AS split_line,
                   {ACCT_LABEL_SQL} AS account"""
        joins = f"""LEFT JOIN accounts a ON a.id = t.account_id
                 LEFT JOIN items i ON i.id = a.item_id
                 {merchant_sql.MC_JOIN}
                 {split_join}"""
    out = conn.execute(
        f"""SELECT {cols}
            FROM transactions t
                 {joins}
            WHERE t.removed = 0 {SPEND_ONLY_SQL}
              AND t.date >= %s AND t.date < %s
              {PERSONAL_ONLY_SQL}
              {excl_sql}
            ORDER BY t.amount DESC, {split_line}""",
        params).fetchall()
    if memo is not None:
        memo[key] = out
    return out


def _inflow_rows(conn, since: dt.date, until: dt.date,
                 excluded: list | None = None, *,
                 memo: dict | None = None) -> list[dict]:
    """Deposit-side twin of _spend_rows for INCOME occurrence matching:
    posted inflows on depository accounts, amounts flipped positive
    (match_occurrences compares against the bill's positive dollars).
    Transfers are excluded — a savings→checking move that happens to be
    paycheck-sized must not mark the paycheck received. `memo`: the
    request-scoped cache, as on _spend_rows."""
    key = _memo_key("inflow_rows", since, until, excluded)
    if memo is not None and key in memo:
        return memo[key]
    from . import links
    excluded = list(excluded or []) + links.shadow_ids(conn)
    excl_sql = "AND NOT (t.account_id = ANY(%s))" if excluded else ""
    params: tuple = ((since, until, list(excluded)) if excluded
                     else (since, until))
    out = conn.execute(
        f"""SELECT t.id AS txn_id, t.date, -t.amount AS amount,
                   {merchant_sql.RAW_KEY} AS payee, t.name,
                   -- the merchant the ledger files the row under, so an
                   -- income series follows its payer like a bill does
                   t.merchant_id::text AS merchant_id
            FROM transactions t
                 JOIN accounts a ON a.id = t.account_id
            WHERE t.removed = 0 AND t.pending = 0 AND t.amount < 0
              AND a.type = 'depository'
              AND COALESCE(t.category_override, t.category_primary, '')
                  NOT IN ('TRANSFER_OUT','TRANSFER_IN')
              AND t.date >= %s AND t.date < %s
              -- Inflow is money arriving in a PERSONAL account. Without this
              -- the sibling of every outflow query, a deposit into a business
              -- account read as household income: it seeded income detection
              -- and matched paycheck occurrences on the Bills page, so a
              -- consulting client's payment could become somebody's salary.
              {PERSONAL_ONLY_SQL}
              {excl_sql}
            ORDER BY t.amount ASC""",
        params).fetchall()
    if memo is not None:
        memo[key] = out
    return out


# ---- upcoming bills: the one rule every cash surface uses ---------------


def _card_pay_bill(b: dict) -> bool:
    """A recurring bill whose payee names a card payment. Checked against
    BOTH detectors: merchant_seed.flow_match is deliberately high-precision
    and misses issuer+AUTOPAY names ("DISCOVER AUTOPAY") that the importers'
    own detector (flowmap.looks_like_card_payment) catches."""
    from ..sync import flowmap
    from . import merchant_seed
    fm = merchant_seed.flow_match(b["payee"])
    if fm and fm[1] == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT":
        return True
    return flowmap.looks_like_card_payment(b["payee"])


def drop_card_pay_bills(conn, bills: list[dict], *, cfg: dict | None = None,
                        memo: dict | None = None) -> list[dict]:
    """Bills minus the hand-tracked "card payment" ones, while live card
    debt exists to be paid by the card machinery instead.

    Such a bill double-reserves against the live card machinery — Today
    headroom's required is card_debt + due_total (the bill counts the same
    payment twice) and the forecast walks BOTH the bill occurrence and the
    per-card payoff event. It can also never occurrence-match its payment
    (_spend_rows excludes card payments), so on the month verdict it sits
    overdue forever and its planned amount inflates the fixed month, which
    shrinks the dynamic variable budgets that the forecast burns at. One
    filter, applied wherever bills become expectations, so the verdict and
    the cash surfaces see the same set. With no live card debt the bill is
    the only signal an unlinked, hand-tracked card exists and it stays.

    "Live card debt" means debt the CARD MACHINERY will actually pay on a
    cash surface, so the question is asked with that machinery's own scope
    (forecast.CARD_SCOPE_SQL) rather than a second spelling of it: a card
    the household switched off on the Accounts page, or a business card, or
    a linked group's shadow source, schedules no payoff event, so counting
    its balance here would delete the bill and put nothing in its place —
    the payment would vanish from every cash surface, which is the exact
    failure this function exists to prevent. (Lazy import: forecast imports
    this module.)"""
    if not any(_card_pay_bill(b) for b in bills):
        return bills
    from . import forecast
    excluded = excluded_account_ids(
        cfg if cfg is not None else load_config(conn, memo=memo))
    live_debt = conn.execute(
        f"""SELECT COALESCE(SUM(GREATEST(a.balance_current, 0)), 0) AS d
           FROM accounts a WHERE a.type = 'credit'
             {forecast.CARD_SCOPE_SQL}""", (excluded,)).fetchone()["d"]
    if live_debt > 0.5:
        return [b for b in bills if not _card_pay_bill(b)]
    return bills


def months_spanned(start: dt.date, end: dt.date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)


def upcoming_bill_occurrences(conn, cfg: dict, today: dt.date,
                              end: dt.date, *,
                              income: bool = False,
                              memo: dict | None = None) -> list[dict]:
    """Unpaid bill occurrences from today through `end`, one rule for every
    cash surface (forecast, runway, Today headroom, email).

    `income=True` runs the same rule over the INCOME series, matched against
    posted deposits instead of spend: a paycheck that already landed in
    checking is in the live balance, so scheduling it again on its due date
    walks the same money in twice. Two deliberate differences: card-payment
    bills are a spend-side concern, and an income occurrence whose day has
    passed without a deposit is not re-dated to tomorrow — a late paycheck
    is not owed the way an overdue bill is, and every biweekly series whose
    back-projected occurrence has no deposit yet would otherwise gain a
    phantom paycheck tomorrow.

    `memo`: the request-scoped cache — the forecast, the runway and the
    month-end tile each ask for a window from the same snapshot; the
    schedule and the matched spend rows are derived once and a repeated
    (today, end) window is answered outright.

    These surfaces must not disagree. Matching occurrences against
    month-to-date spend in one place and summing raw `month_occurrences` in a
    date window in another gives a bill PAID EARLY that is still reserved by
    the runway (headroom too tight, double-counting money already gone) and
    an OVERDUE bill that falls out of the window entirely (headroom too
    rosy). Same bills, two answers, on the page the product is built around.

    The rules, in one place:
      - EVERY occurrence in scope is occurrence-MATCHED against the ledger; a
        bill with a txn attached is already in today's balance, so counting
        it again double-counts. Matching the current month MTD only is not
        enough — a bill due Aug 1 autopaid Jul 28 would still be reserved
        (future months never matched), and a bill due Jul 2 paid Jun 28 would
        go "overdue" (no lookback before month start). Payments span a match
        window before the month start, and future-month occurrences are
        matched before being reserved.
      - LAST month's occurrences join the match as decoys only: a boundary
        payment (June's bill paid late on Jul 1) attaches to its own
        occurrence instead of eating a current/future one. They are never
        emitted.
      - `floored` occurrences are never expectations
      - an overdue unpaid bill is not dropped — it lands tomorrow, the
        soonest it can realistically be paid (current month only; future
        occurrences cannot be overdue)
      - every month the window spans is expanded, not just the endpoints
        (endpoints alone make a 3-month horizon skip the middle month's
        bills entirely)

    Returns dicts with the EFFECTIVE `due` (overdue re-dated), positive
    `planned`, `payee`, and the underlying `occ`.
    """
    key = _memo_key("upcoming_bill_occurrences", today, end, income,
                    cfg.get("occurrence_caps"), cfg.get("disabled_bills"),
                    excluded_account_ids(cfg))
    if memo is not None and key in memo:
        return memo[key]
    bills = _recurring_bills(conn, caps=cfg.get("occurrence_caps"),
                             disabled=cfg.get("disabled_bills"),
                             income=income, memo=memo)
    if not income:
        bills = drop_card_pay_bills(conn, bills, cfg=cfg, memo=memo)
    out: list[dict] = []
    # a payment can precede its due date by up to the occurrence match
    # window (≤PREPAY_MATCH_DAYS, month_occurrences) — look back that far
    # before the month start so an early cross-boundary payment is visible
    since = today.replace(day=1) - dt.timedelta(days=PREPAY_MATCH_DAYS)
    if income:
        rows = _inflow_rows(conn, since, today + dt.timedelta(days=1),
                            excluded=excluded_account_ids(cfg), memo=memo)
    else:
        rows = _spend_rows(conn, since, today + dt.timedelta(days=1),
                           excluded=excluded_account_ids(cfg), memo=memo)
    py, pm = ((today.year, today.month - 1) if today.month > 1
              else (today.year - 1, 12))
    occs = month_occurrences(bills, py, pm)         # decoys — never emitted
    n_prev = len(occs)
    occs += month_occurrences(bills, today.year, today.month)
    n_cur = len(occs)
    for y, m in months_spanned(today, end):
        if (y, m) == (today.year, today.month):
            continue
        occs += month_occurrences(bills, y, m)
    _, occs = match_occurrences(rows, occs)
    for i in range(n_prev, len(occs)):
        occ = occs[i]
        if occ["floored"] or occ["txn"] is not None:
            continue
        due = (max(occ["due"], today + dt.timedelta(days=1))
               if i < n_cur and not income else occ["due"])
        if today < due <= end:
            out.append({"due": due, "planned": occ["planned"],
                        "payee": occ["bill"]["payee"], "occ": occ})
    out.sort(key=lambda r: r["due"])
    if memo is not None:
        memo[key] = out
    return out


# ---- cash events: bonus + investment funding ---------------------------


def cash_events(conn, month_start: dt.date, until: dt.date) -> dict:
    """Detect (a) an annual bonus — a payroll deposit far above the normal
    paycheck, EXCLUDED from all budget math by design — and (b) transfers in
    from the tenant's investment institutions, which mean spending was
    covered by selling investments (the metric this budget exists to drive
    to zero).

    An "investment institution" is any institution holding an account of
    type 'investment' — its name token in an inflow's text marks that
    inflow as investment funding."""
    from . import links
    # Health-aware shadows (same set the verdict's _spend_rows uses), so
    # during a primary-checking outage this shortfall detector looks at the
    # healthy backup — not the down favorite the rank-based GUC still names.
    shadows = links.shadow_ids(conn)
    pay = conn.execute(
        "SELECT payee, amount AS amt FROM bills "
        "WHERE amount > 0 AND active = 1 "
        "ORDER BY amount DESC LIMIT 1").fetchone()
    paycheck = pay["amt"] if pay and pay["amt"] else 5000.0
    pay_key = _key_token(pay["payee"] or "") if pay else None

    inst_keys = {k for k in (
        _key_token(r["institution_name"] or "") for r in conn.execute(
            """SELECT DISTINCT i.institution_name
               FROM accounts a JOIN items i ON i.id = a.item_id
               WHERE a.type = 'investment'""").fetchall()) if k}

    def _hit(text: str) -> bool:
        return any(k in text for k in inst_keys)

    def inflows(since):
        # Budget bank accounts only — and a depository account AT an
        # investment institution (its cash/sweep account) posts interest
        # micro-deposits every month; counting those as "sold stock to cover
        # spending" fires the red shortfall alarm on cents of interest.
        return [r for r in conn.execute(
            f"""SELECT t.date, -t.amount AS inflow, t.name,
                      LOWER(COALESCE(i.institution_name,'')) AS inst
               FROM transactions t
               JOIN accounts a ON a.id = t.account_id
               JOIN items i ON i.id = a.item_id
               WHERE t.removed = 0 AND t.amount < 0 AND a.type = 'depository'
                 AND t.date >= %s AND t.date < %s
                 AND NOT (t.account_id = ANY(%s))
                 {PERSONAL_ONLY_SQL}""",
            (since, until, shadows)).fetchall() if not _hit(r["inst"])]

    # transfers above this are one-time asset purchases (house down payment),
    # not spending shortfalls — reported separately, never in the shortfall
    # metric
    LARGE = 15_000.0

    bonus, funding_mtd, large_mtd = [], [], []
    for r in inflows(month_start):
        name = (r["name"] or "").lower()
        day = as_date(r["date"]).isoformat()
        if _hit(name):
            (large_mtd if r["inflow"] >= LARGE else funding_mtd).append(
                (day, r["inflow"]))
        elif pay_key and pay_key in name and r["inflow"] > paycheck * 1.75:
            bonus.append((day, r["inflow"]))

    ytd = sum(r["inflow"] for r in inflows(month_start.replace(month=1, day=1))
              if _hit((r["name"] or "").lower()) and r["inflow"] < LARGE)
    return {"bonus": bonus, "funding_mtd": funding_mtd, "large_mtd": large_mtd,
            "funding_mtd_total": sum(a for _, a in funding_mtd),
            "funding_ytd_total": ytd}


def recurring_load(conn, today: dt.date) -> dict:
    """What the recurring schedule costs and brings in, per month, plus the
    week ahead — the Bills page's headline.

    Deliberately the SAME evened-out year_total/12 the month plan uses
    (`avg_bills` above and in month_status), not a fresh sum of cadences.
    A page that says "$3,000/mo bills" beside a Budget page saying something
    else for the identical schedule makes both untrustworthy. Two
    methodologies behind one number always cost more than they save: a tile
    that sums the raw ledger beside a figure that excludes transfers reads an
    owner draw as a loss.
    """
    cfg = load_config(conn)
    caps, disabled = cfg.get("occurrence_caps"), cfg.get("disabled_bills")
    bills = _recurring_bills(conn, caps=caps, disabled=disabled)
    income = _recurring_bills(conn, caps=caps, disabled=disabled, income=True)
    envelopes = _envelope_bills(conn, disabled=disabled)

    def _year(rows):
        return sum(o["planned"] for m in range(1, 13)
                   for o in month_occurrences(rows, today.year, m)
                   if not o["floored"])

    bills_mo = _year(bills) / 12 + sum(e["monthly"] for e in envelopes)
    income_mo = _year(income) / 12
    # the week ahead, from the same occurrence engine — envelopes have no
    # due dates and correctly contribute nothing here
    horizon = today + dt.timedelta(days=7)
    soon = [o for m in ((today.year, today.month),
                        (horizon.year, horizon.month))
            for o in month_occurrences(bills, m[0], m[1])
            if today <= o["due"] <= horizon and not o["floored"]]
    # a month boundary inside the window makes the two expansions overlap
    seen, week = set(), []
    for o in soon:
        key = (o["bill"]["payee"], o["due"])
        if key not in seen:
            seen.add(key)
            week.append(o)
    return {"bills_monthly": round(bills_mo, 2),
            "income_monthly": round(income_mo, 2),
            "week_total": round(sum(o["planned"] for o in week), 2),
            "week_count": len(week)}


# ---- the monthly picture ----------------------------------------------


def month_status(conn, today: dt.date, *, historical: bool = False,
                 cfg: dict | None = None, memo: dict | None = None,
                 bill_rows: list | None = None,
                 closed: bool = False) -> dict:
    """historical=True: skip fixed-bill projection entirely — the recurring
    table only holds TODAY's live schedule; projecting it onto a past month
    fabricates bills that didn't exist yet. Pass True whenever `today` isn't
    the real current date.

    closed=True: `today` is the last day of a month that is OVER — the
    month lens and the report card judge a past month as of its month
    end, with every day elapsed (expected = the whole budget). Live, the
    day in progress has not elapsed yet (see `frac` below).

    `cfg` overrides the live config — the lenses pass a closed month's
    budget snapshot so the month is judged against the budget it actually
    ran under, not today's. `bill_rows` does the same for the SCHEDULE
    (snapshot_bill_rows): the bill/envelope expansion runs over the frozen
    rows instead of the live table, so a later bill edit can't re-judge the
    month. Without it the live table applies — the only option for months
    that predate bill freezing."""
    if cfg is None:
        cfg = load_config(conn, memo=memo)
    month_start = today.replace(day=1)
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    # Expected-to-date is the share of the month that has ELAPSED — today
    # has not. The today-ledger hands out today's allowance from midnight
    # (remaining ÷ days left INCLUDING today); counting today as spent
    # here would make an on-pace household with nothing bought yet this
    # morning read UNDER by exactly one day's budget, and the email's
    # verdict and its left-to-spend tile would contradict each other.
    frac = (today.day if closed else today.day - 1) / days_in_month

    bills = [] if historical else drop_card_pay_bills(
        conn, _recurring_bills(
            conn, caps=cfg.get("occurrence_caps"),
            disabled=cfg.get("disabled_bills"), memo=memo, rows=bill_rows),
        cfg=cfg, memo=memo)
    envelopes = [] if historical else _envelope_bills(
        conn, disabled=cfg.get("disabled_bills"), memo=memo, rows=bill_rows)
    # Occurrence matching reaches back
    # PREPAY_MATCH_DAYS before month start, exactly as
    # upcoming_bill_occurrences does. Without it a bill autopaid a few days
    # early — due the 2nd, paid on the 28th — is invisible here, so the
    # verdict calls it overdue while the forecast has already settled it
    # and moved on to next month's occurrence. Same bill, same ledger, two
    # answers, on the page the product is built around.
    #
    # The wider set is for MATCHING ONLY. Spend accounting stays inside the
    # month: `rows` is the in-month slice, so last month's cash cannot land
    # in this month's actuals.
    match_rows = _spend_rows(
        conn, month_start - dt.timedelta(days=PREPAY_MATCH_DAYS),
        today + dt.timedelta(days=1), excluded=excluded_account_ids(cfg),
        memo=memo)
    in_month = [i for i, r in enumerate(match_rows)
                if as_date(r["date"]) >= month_start]
    _pos = {src: dst for dst, src in enumerate(in_month)}
    rows = [match_rows[i] for i in in_month]

    # --- fixed: occurrence-level matching ---
    occs = month_occurrences(bills, today.year, today.month)
    # Next month's occurrences join the candidates, for MATCHING only. The
    # forecast attaches rent due the 1st to its autopay on the 28th before;
    # the verdict must attach the same payment to the same occurrence, or
    # the rent is a $2,000 variable purchase — OVER BUDGET — on the day it
    # went out, while headroom already counts the cash as gone. Cash
    # basis: a next-month occurrence PAID in this month is this month's
    # fixed spend and fixed plan (the money left here; next month skips
    # it as settled-before-start). One nobody has paid yet is dropped
    # right after matching — it is neither this month's expectation nor
    # its plan, or every month would carry two rents.
    ny, nm = ((today.year, today.month + 1) if today.month < 12
              else (today.year + 1, 1))
    n_this = len(occs)
    occs += month_occurrences(bills, ny, nm)
    matched_all, occs = match_occurrences(match_rows, occs)
    occs = occs[:n_this] + [o for o in occs[n_this:] if o["txn"] is not None]
    # translate to `rows` indices; a match on a PRIOR-month payment has no
    # in-month row and simply drops out of the spend side
    matched_idx = {_pos[i] for i in matched_all if i in _pos}
    env_matched, env_states = match_envelopes(
        rows, envelopes, matched_idx,
        prior_used=_envelope_prior_used(conn, envelopes, today,
                                        excluded=excluded_account_ids(cfg),
                                        memo=memo, bills=bills))
    fixed_rows = ([rows[i] for i in matched_idx]
                  + [r for st in env_states for r in st["rows"]])
    variable_rows = [r for i, r in enumerate(rows)
                     if i not in matched_idx and i not in env_matched]

    # an occurrence counts toward expectations when it's been PAID (recognized
    # at payment — early payment is never "over"), or when it's unpaid,
    # unfloored, and its due date has arrived
    fixed_expected_td = fixed_actual = fixed_month = 0.0
    drifts: dict[str, float] = {}
    overdue_unpaid = []
    # bills pinned to the Today page/email get a per-bill month summary —
    # the aggregate `fixed` bucket can't say whether THIS bill is paid
    pinned: dict[str, dict] = {}

    def _pin(bill):
        return pinned.setdefault(bill["payee"], {
            "payee": bill["payee"], "planned": 0.0, "paid": 0.0,
            "paid_count": 0, "occ_count": 0, "next_due": None,
            "overdue": False})

    for occ in occs:
        if occ["txn"] is not None:
            # settled before this month began: the cash left in the month it
            # left, and was counted there. It is neither an expectation nor
            # an actual here — it is simply not this month's problem. What
            # it must NOT be is "overdue".
            if as_date(occ["txn"]["date"]) < month_start:
                continue
            fixed_expected_td += occ["planned"]
            fixed_month += occ["planned"]
            # what was actually paid includes the companions that rode with
            # the payment — planned carries their fees (companion
            # charges), so the actual and the drift must count the same
            # money or every companion-carrying bill reports phantom drift
            paid_amount = occ["txn"]["amount"] + sum(
                c.get("amount", 0.0) for c in occ.get("companions") or [])
            fixed_actual += paid_amount
            d = paid_amount - occ["planned"]
            if abs(d) >= 1:
                drifts[occ["bill"]["payee"]] = drifts.get(occ["bill"]["payee"], 0.0) + d
            if occ["bill"].get("show_today"):
                p = _pin(occ["bill"])
                p["planned"] += occ["planned"]
                p["paid"] += paid_amount
                p["paid_count"] += 1
                p["occ_count"] += 1
        elif not occ["floored"]:
            fixed_month += occ["planned"]
            if occ["due"] <= today:
                fixed_expected_td += occ["planned"]
                overdue_unpaid.append(occ)
            if occ["bill"].get("show_today"):
                p = _pin(occ["bill"])
                p["planned"] += occ["planned"]
                p["occ_count"] += 1
                if p["next_due"] is None or occ["due"] < p["next_due"]:
                    p["next_due"] = occ["due"]
                p["overdue"] = p["overdue"] or occ["due"] <= today

    # envelope bills: cap-only — used dollars are fixed, expected at payment
    # time; the pool's monthly share is in the plan; overage overflows to
    # variable. An annual envelope therefore recognizes a lumpy
    # renewal in full the month it lands while the plan carries pool/12 —
    # the charge is fixed spend, never a variable-verdict "over".
    for st in env_states:
        fixed_actual += st["used"]
        fixed_expected_td += st["used"]
        fixed_month += st["bill"]["monthly"]

    buckets = {"food": [], "other": []}
    for r in variable_rows:
        buckets["food" if r["is_food"] else "other"].append(r)
    for st in env_states:
        for r in st["overflow_rows"]:
            buckets["food" if r["is_food"] else "other"].append(r)

    def total(rs):
        return sum(r["amount"] for r in rs)

    # Dynamic variable budget: the month's variable budget is what income
    # leaves after this month's fixed-bill schedule, split in the configured
    # ratio. SHRINK-ONLY — heavy-bill months compress budgets so the plan
    # breaks even; light months stay capped (surplus = savings).
    food_budget, other_budget = cfg["food_monthly"], cfg["other_monthly"]
    income = active_monthly_income(cfg)
    scale = None
    if cfg.get("dynamic_variable_budget", True) and income:
        available = income - fixed_month
        static_total = food_budget + other_budget
        # static_total > 0: with both budgets $0 there is nothing to
        # compress — and a heavy-bill month makes `available` negative, so
        # `available < static_total` holds and the factor divides by zero,
        # 500ing Today/forecast/email (both budgets $0 is common
        # mid-wizard). $0 budgets stay $0, no scale note (nothing was
        # scaled).
        if static_total > 0 and available < static_total:
            factor = max(0.0, available / static_total)
            food_budget *= factor
            other_budget *= factor
            scale = {"factor": factor, "static_total": static_total,
                     "available": max(0.0, available)}

    b = {
        "fixed": {"actual": fixed_actual, "expected": fixed_expected_td,
                  "month_budget": fixed_month, "rows": fixed_rows,
                  # occurrence-matched bill payments ONLY (no envelope swipes)
                  "occ_rows": [rows[i] for i in matched_idx]},
        "food": {"actual": total(buckets["food"]), "expected": food_budget * frac,
                 "month_budget": food_budget, "rows": buckets["food"]},
        "other": {"actual": total(buckets["other"]), "expected": other_budget * frac,
                  "month_budget": other_budget, "rows": buckets["other"]},
    }
    for v in b.values():
        v["variance"] = v["actual"] - v["expected"]  # positive = over

    # ---- custom buckets: decompose food/other for display ----
    # The verdict inputs above are FROZEN — b[food/other] actual/expected/
    # month_budget stay the raw two-bucket numbers. Custom buckets carve the
    # parent's rows for the bars/email/reasons only; when the dynamic scale
    # compressed the parents, children compress by the same factor so the
    # decomposition still describes the compressed plan.
    factor = scale["factor"] if scale else 1.0
    customs = _compiled_buckets(custom_bucket_specs(cfg))
    for c in customs:
        c["monthly"] *= factor
        c["actual"], c["rows"] = 0.0, []
    carved = {"food": 0.0, "other": 0.0}
    if customs:
        for pname in ("food", "other"):
            for r in buckets[pname]:
                c = _claim_custom(customs, r)
                if c is not None:
                    c["rows"].append(r)
                    c["actual"] += r["amount"]
                    carved[pname] += r["amount"]
    for pname in ("food", "other"):
        v = b[pname]
        kids = [c for c in customs if c["parent"] == pname]
        v["children"] = [
            {"name": c["name"], "actual": c["actual"],
             "month_budget": c["monthly"], "expected": c["monthly"] * frac,
             "variance": c["actual"] - c["monthly"] * frac, "rows": c["rows"]}
            for c in kids]
        # display decomposition: parent actual = raw − carved-out dollars;
        # the parent's own budget stays the user-set number, its REMAINING
        # budget (after the children's shares) is shown alongside (spec §1)
        rem_budget = max(0.0, v["month_budget"]
                         - sum(c["monthly"] for c in kids))
        v["display_actual"] = v["actual"] - carved[pname]
        v["remaining_budget"] = rem_budget
        v["display_expected"] = rem_budget * frac

    # The VERDICT is variable-only: fixed bills are obligations with their own
    # schedule — a bill posting late is not "under budget".
    variable_expected = b["food"]["expected"] + b["other"]["expected"]
    variable_actual = b["food"]["actual"] + b["other"]["actual"]
    variance = variable_actual - variable_expected
    tolerance = max(50.0, variable_expected * 0.05)
    verdict = ("ON BUDGET" if abs(variance) <= tolerance
               else "OVER BUDGET" if variance > 0 else "UNDER BUDGET")
    # Early in the month "UNDER BUDGET" reports the CALENDAR, not behaviour:
    # the morning email on the 1st would announce under-budget purely
    # because barely any month has elapsed to spend it in. Under-spending
    # only means something once there has been a real opportunity to spend, so for the
    # first tenth of the month (days 1–4 of a 31-day month) the good news
    # collapses into ON BUDGET. OVER is untouched — you can absolutely blow
    # the month's budget on the 1st, and that is worth saying immediately.
    if verdict == "UNDER BUDGET" and frac < EARLY_MONTH_FRAC:
        verdict = "ON BUDGET"

    reasons = []
    if verdict == "OVER BUDGET":
        # The Why's merchant ranking is pure-variable only, with bill overage
        # broken out as a labeled tail item (envelope overflow, or a
        # bill-shaped charge beyond its scheduled occurrences). A merchant-
        # token coincidence at a different amount stays ordinary spend.
        for name in ("food", "other"):
            v = b[name]
            if v["variance"] > tolerance / 2:
                daily_actual = v["actual"] / today.day
                daily_budget = v["month_budget"] / days_in_month
                by_merchant: dict[str, list] = {}
                billish: dict[str, float] = {}
                for r in v["rows"]:
                    if r["category"] == "envelope overflow":
                        billish[r["payee"]] = billish.get(r["payee"], 0.0) + r["amount"]
                        continue
                    txt = f"{r['payee'] or ''} {r['name'] or ''}".lower()
                    toks = _tokens(txt)
                    hit = next(
                        (bl for bl in bills
                         if _bill_matches(bl, toks, txt, row_merchant_id(r))
                         and abs(r["amount"] - bl["amount"])
                             <= bill_tolerance(bl["amount"])), None)
                    if hit is not None:
                        label = f"{hit['payee']} (beyond bill schedule)"
                        billish[label] = billish.get(label, 0.0) + r["amount"]
                        continue
                    by_merchant.setdefault(r["payee"] or "?", []).append(r["amount"])
                top = sorted(by_merchant.items(), key=lambda kv: -sum(kv[1]))[:3]
                detail = [f"{m} ${sum(a):,.0f}" + (f" ({len(a)}×)" if len(a) > 1 else "")
                          for m, a in top]
                detail += [f"{label} ${amt:,.0f}"
                           for label, amt in sorted(billish.items(),
                                                    key=lambda kv: -kv[1])]
                # custom buckets are the WHY: name each child running over
                # its own pace (additive — absent without custom buckets)
                detail += [f"{c['name']} bucket +${c['variance']:,.0f} over pace"
                           for c in sorted(v.get("children") or [],
                                           key=lambda c: -c["variance"])
                           if c["variance"] > 0.5]
                reasons.append({
                    "bucket": name, "variance": v["variance"],
                    "pace": (f"running ${daily_actual:,.0f}/day vs "
                             f"${daily_budget:,.0f}/day plan"),
                    "detail": detail,
                })

    # ---- the WHY: today's story, every bucket, every verdict ----------
    # The Why is a headline feature, not an over-budget apology. One
    # narrative built HERE so the Today page,
    # the email HTML and the plain text all say the same words: a
    # headline, then one sentence per bucket — food, everything-else AND
    # every custom bucket as first-class entries — naming the actual
    # merchants and amounts behind each number. Parents use the DISPLAY
    # decomposition (raw minus the children's carve-outs) so a dollar
    # never appears in two sentences.
    days_left_m = days_in_month - today.day + 1

    def _spenders(rows_, k=3):
        by_m: dict[str, list] = {}
        tail: dict[str, float] = {}
        for r in rows_:
            if r["category"] == "envelope overflow":
                tail[r["payee"]] = tail.get(r["payee"], 0.0) + r["amount"]
                continue
            txt = f"{r['payee'] or ''} {r['name'] or ''}".lower()
            toks = _tokens(txt)
            hit = next((bl for bl in bills
                        if _bill_matches(bl, toks, txt, row_merchant_id(r))
                        and abs(r["amount"] - bl["amount"])
                            <= bill_tolerance(bl["amount"])), None)
            if hit is not None:
                lbl = f"{hit['payee']} (beyond bill schedule)"
                tail[lbl] = tail.get(lbl, 0.0) + r["amount"]
                continue
            by_m.setdefault(r["payee"] or "?", []).append(r["amount"])
        top = [{"payee": m, "amount": round(sum(a), 2), "count": len(a)}
               for m, a in sorted(by_m.items(),
                                  key=lambda kv: -sum(kv[1]))[:k]]
        top += [{"payee": lbl, "amount": round(amt, 2), "count": 1}
                for lbl, amt in sorted(tail.items(), key=lambda kv: -kv[1])]
        return top

    def _top_text(top):
        return ", ".join(
            f"{t['payee']} ${t['amount']:,.0f}"
            + (f" ({t['count']}×)" if t["count"] > 1 else "")
            for t in top[:4])

    def _why_entry(name, spent, month_budget, expected, rows_):
        var = spent - expected
        tol = max(10.0, expected * 0.08)
        left = month_budget - spent
        rate = (left / days_left_m) if days_left_m > 0 else left
        top = _spenders(rows_)
        daily_actual = spent / today.day
        daily_plan = (month_budget / days_in_month
                      if days_in_month else 0.0)
        if var > tol:
            tone = "over"
            text = (f"is ${var:,.0f} over pace"
                    + (f" — {_top_text(top)}" if top else "")
                    + f"; running ${daily_actual:,.0f}/day against a "
                      f"${daily_plan:,.0f}/day plan.")
        elif var < -tol:
            tone = "under"
            text = (f"is ${-var:,.0f} ahead — ${max(0.0, left):,.0f} of "
                    f"${month_budget:,.0f} left"
                    + (f" (${max(0.0, rate):,.0f}/day)" if left > 0 else "")
                    + (f". Biggest: {_top_text(top[:2])}." if top else "."))
        else:
            tone = "on"
            text = (f"is on pace — ${max(0.0, left):,.0f} left, "
                    f"${max(0.0, rate):,.0f}/day keeps it green"
                    + (f". Biggest: {_top_text(top[:2])}." if top else "."))
        return {"name": name, "tone": tone, "text": text,
                "spent": round(spent, 2), "budget": round(month_budget, 2),
                "variance": round(var, 2), "top": top[:4]}

    why_entries = []
    for pname, plabel in (("food", "Food"), ("other", "Everything else")):
        v = b[pname]
        kids = v.get("children") or []
        kid_ids = {id(r) for c in kids for r in c["rows"]}
        parent_rows = ([r for r in v["rows"] if id(r) not in kid_ids]
                       if kids else v["rows"])
        spent = v["display_actual"] if kids else v["actual"]
        budget_ = v["remaining_budget"] if kids else v["month_budget"]
        expected_ = v["display_expected"] if kids else v["expected"]
        if budget_ > 0 or spent > 0:
            why_entries.append(
                _why_entry(plabel, spent, budget_, expected_, parent_rows))
        for c in kids:
            if c["month_budget"] > 0 or c["actual"] > 0:
                why_entries.append(_why_entry(
                    c["name"], c["actual"], c["month_budget"],
                    c["expected"], c["rows"]))
    _tone_rank = {"over": 0, "on": 1, "under": 2}
    why_entries.sort(key=lambda e: (_tone_rank[e["tone"]], -e["variance"]))

    over_es = [e for e in why_entries if e["tone"] == "over"]
    if verdict == "OVER BUDGET" and over_es:
        head = (f"Over because {over_es[0]['name']} "
                f"(+${over_es[0]['variance']:,.0f})")
        if len(over_es) > 1:
            head += (f" and {over_es[1]['name']} "
                     f"(+${over_es[1]['variance']:,.0f})")
        head += ("." if len(over_es) > 2 or len(over_es) == len(why_entries)
                 else " — the rest is holding.")
    elif verdict == "OVER BUDGET":
        # The per-bucket 8% bar is LAXER than the
        # combined 5% verdict bar, so every bucket can sit just under its
        # own line while the sum blows the verdict's — where a per-bucket
        # headline would fall through to "On pace" beside a red OVER pill.
        # The headline must never contradict the badge above it.
        worst = max(why_entries, key=lambda e: e["variance"], default=None)
        head = (f"Over pace overall (+${variance:,.0f}) — spread across "
                "buckets"
                + (f"; {worst['name']} is the biggest driver."
                   if worst and worst["variance"] > 0 else "."))
    elif verdict == "UNDER BUDGET":
        best = min(why_entries, key=lambda e: e["variance"], default=None)
        head = (f"${-variance:,.0f} ahead of pace overall"
                + (f" — {best['name']} is doing the saving." if best
                   and best["variance"] < 0 else "."))
    else:
        nearest = min((e for e in why_entries if e["budget"] > 0),
                      key=lambda e: (e["budget"] - e["spent"])
                                    / max(1.0, e["budget"]),
                      default=None)
        head = ("On pace"
                + (f" — closest to the line: {nearest['name']}."
                   if nearest else "."))

    # OVER BUDGET gets a way back, not just a diagnosis: per bucket, the
    # most that can go out per remaining day and still end the month inside
    # that bucket's own budget. Whole dollars, floored — rounding up would
    # break the promise the sentence makes. A bucket already through its
    # budget (or left with under $1/day) gets the honest version: $0 — every
    # remaining day is a no-spend day there. A $0-budget bucket with spend is
    # the same case, not a skip: it can be the very bucket the headline names
    # as the cause, and a recovery sentence that ignores it breaks its own
    # promise. Composed HERE like the rest of the Why, so the page, the email
    # HTML and the plain text cannot drift.
    recovery = None
    if verdict == "OVER BUDGET" and days_left_m > 0:
        capped, spent_out = [], []
        for e in why_entries:
            if e["budget"] <= 0 and e["spent"] <= 0:
                continue
            cap = int(max(0.0, e["budget"] - e["spent"]) // days_left_m)
            if cap >= 1:
                capped.append(f"${cap:,d}/day on {e['name']}")
            else:
                spent_out.append(e["name"])
        parts = []
        if capped:
            parts.append("spend at most " + ", ".join(capped))
        if spent_out:
            names = (" and ".join(spent_out) if len(spent_out) <= 2
                     else ", ".join(spent_out[:-1]) + f" and {spent_out[-1]}")
            parts.append(
                f"{names} {'is' if len(spent_out) == 1 else 'are'} spent for "
                f"the month — every remaining day is a $0 day there")
        if parts:
            recovery = (f"To get back on track over the last {days_left_m} "
                        f"day{'s' if days_left_m != 1 else ''}: "
                        + "; ".join(parts) + ".")
    why = {"headline": head, "entries": why_entries, "recovery": recovery}

    unpaid_due_total = sum(o["planned"] for o in overdue_unpaid)
    b["fixed"]["unpaid_due"] = unpaid_due_total
    b["fixed"]["drift"] = sum(drifts.values())
    b["fixed"]["envelopes"] = [
        {"payee": st["bill"]["payee"], "used": st["used"],
         "monthly": st["bill"]["monthly"], "overflow": st["overflow"],
         # an annual envelope's cap is the year's pool, so `used`
         # (this month) doesn't say how close to the cap it is — these do
         "pool": st["bill"]["pool"],
         "period_months": st["bill"]["period_months"],
         "period_used": st["prior_used"] + st["used"],
         "period_left": max(0.0, st["bill"]["pool"]
                            - st["prior_used"] - st["used"]),
         "show_today": st["bill"].get("show_today", False)}
        for st in env_states]

    # non-monthly bills scheduled this month — the named cause of a
    # compressed variable budget
    irregular = []
    for occ in occs:
        if occ["floored"] and occ["txn"] is None:
            continue
        rec = occ["bill"]["recurrence"]
        if not rec.get("frequency"):
            label = "one-time"
        elif _cycle_days(rec) > 35:
            iv = rec.get("interval") or 1
            freq = rec["frequency"].upper()
            label = ("annual" if freq == "YEARLY" and iv == 1
                     else f"every {iv} years" if freq == "YEARLY"
                     else f"every {iv} months")
        else:
            continue
        irregular.append({"payee": occ["bill"]["payee"], "amount": occ["planned"],
                          "label": label, "due": occ["due"].isoformat()})
    irregular.sort(key=lambda x: -x["amount"])

    # --- month weight + cash plan (annual bills, evened-out view) ---
    year_total = sum(o["planned"] for m in range(1, 13)
                     for o in month_occurrences(bills, today.year, m)
                     if not o["floored"])
    year_total += sum(e["monthly"] for e in envelopes) * 12
    avg_bills = year_total / 12
    # planned savings-goal contributions are commitments like
    # fixed bills — they reduce the plan surplus (never the verdict).
    # Plan surplus is a STANDING metric, so it
    # speaks the planner's language — evened yearly bills (avg_bills, not
    # this month's schedule) and the configured budgets (not this month's
    # dynamically-scaled ones). Balancing the planner therefore yields
    # surplus ≈ 0 everywhere; a heavy month shows up in the irregular-bills
    # list, the dynamic scale, and the forecast — not as a broken plan.
    # Only FIXED ('monthly') plans subtract here. A sweep-mode plan is
    # excess-conditional by definition — it takes from the surplus, so
    # subtracting it from the surplus first would both understate the
    # excess line and make the sweep's own availability self-referential.
    from . import savings as _savings
    goals_plan = _savings.monthly_plan_total(cfg, mode="monthly")
    plan_surplus = (income - avg_bills - cfg["food_monthly"]
                    - cfg["other_monthly"] - goals_plan
                    if income else None)
    # Sweep availability, month to date: the plan surplus accrued to
    # today (the surplus is a monthly figure, so it becomes real a day at
    # a time) plus however far variable spending sits under its own pace
    # — overspending eats the surplus, underspending adds to it. Floored
    # at zero: a month that didn't work out has nothing to sweep. This is
    # the one number every "$Y available so far this month" surface reads.
    sweep_plan = _savings.monthly_plan_total(cfg, mode="sweep")
    sweep_available = None
    if plan_surplus is not None and sweep_plan > 0.005:
        sweep_available = round(max(0.0, plan_surplus * frac - variance), 2)
    # posted evidence for the sweep line and the satisfied-month check —
    # the same matched-transfer evidence goal progress runs on
    sweep_posted = 0.0
    if sweep_plan > 0.005:
        for g in _savings.goals(cfg):
            if (_savings.goal_mode(g) == "sweep"
                    and float(g.get("monthly_plan") or 0) > 0
                    and not _savings.is_plan_only(g)):
                # capped at the goal's OWN plan before it joins the total:
                # over-funding one goal must not stand in for a sibling
                # that received nothing, or the sweep line reports the
                # whole plan swept while a goal sits at zero
                sweep_posted += min(
                    float(g["monthly_plan"]),
                    max(0.0, _savings.posted_for_plan(
                        conn, cfg, g, since=month_start, until=today,
                        memo=memo)))
        sweep_posted = round(sweep_posted, 2)

    return {
        "verdict": verdict, "variance": variance, "tolerance": tolerance,
        "variable_scale": scale, "irregular_bills": irregular,
        "events": cash_events(conn, month_start, today + dt.timedelta(days=1)),
        "reasons": reasons, "why": why, "buckets": b, "drifts": drifts,
        "overdue_unpaid": [(o["bill"]["payee"], o["planned"], o["due"].isoformat())
                           for o in overdue_unpaid],
        "avg_bills": avg_bills, "plan_surplus": plan_surplus, "income": income,
        # the surplus subtracts the savings plan, so every surface that
        # renders the plan flow needs it or the chain does not add up
        "savings_plan": goals_plan,
        # sweep goals ride beside the chain, not inside it: the plan cap,
        # what the month has made available so far, and the posted
        # evidence — todayview composes the one sentence all three
        # surfaces render from these
        "sweep_plan": sweep_plan,
        "sweep_available": sweep_available,
        "sweep_posted": sweep_posted,
        "variable_actual": variable_actual, "variable_expected": variable_expected,
        "total_actual": variable_actual + b["fixed"]["actual"],
        "total_expected": variable_expected + b["fixed"]["expected"],
        "config": cfg, "days_in_month": days_in_month, "rows": rows,
        "bills_tracked": not historical,
        # pinned OCCURRENCE bills' month summaries (envelope pins ride on
        # buckets.fixed.envelopes via show_today) — todayview composes both
        # into the cards all three surfaces render
        "pinned_bills": [
            dict(p, next_due=p["next_due"].isoformat()
                 if p["next_due"] else None)
            for p in sorted(pinned.values(), key=lambda p: p["payee"])],
    }


# The three verdict tiles a household reads — Food, Everything else, and
# each custom carve-out — are sums over row sets this module builds and
# nobody else can reproduce: bill-shaped rows are already out, store
# item-match food overrides are already in, envelope overflow has already been
# split off its parent purchase, and a carve-out's merchant rules have
# already beaten its category rules. A surface that offers "open the rows
# behind this number" therefore cannot re-derive the set from a category
# name; it has to ask the same pass that produced the number.
BUCKET_LABELS = {"food": "Food", "other": "Everything else"}


def bucket_ledger(status: dict, bucket: str) -> dict | None:
    """The ledger rows behind ONE bucket tile of a month_status result.

    `bucket` is 'food', 'other', or a custom bucket's configured name.
    Returns {"bucket", "label", "amount", "txn_ids"} — or None when the
    name is not a bucket of this month's plan, which is the caller's 400.

    A parent tile with carve-outs shows its DISPLAY number (its own rows
    minus the children's), because the children have tiles of their own;
    the row set matches, so the listing and the number stay the same
    money. Envelope overflow contributes the purchase it was carved from:
    the slice has no row of its own, and the ledger lists whole
    transactions — so a split purchase's full amount appears there while
    the tile counts only the part beyond the envelope.
    """
    b = status["buckets"]
    if bucket in ("food", "other"):
        v = b[bucket]
        kids = v.get("children") or []
        kid_rows = {id(r) for c in kids for r in c["rows"]}
        rows = [r for r in v["rows"] if id(r) not in kid_rows]
        amount = v["display_actual"] if kids else v["actual"]
        label = BUCKET_LABELS[bucket]
    else:
        c = next((c for parent in ("food", "other")
                  for c in (b[parent].get("children") or [])
                  if c["name"] == bucket), None)
        if c is None:
            return None
        rows, amount, label = c["rows"], c["actual"], c["name"]
    ids: list[str] = []
    seen: set[str] = set()
    for r in rows:
        tid = r.get("txn_id") or r.get("overflow_of")
        if tid and tid not in seen:
            seen.add(tid)
            ids.append(tid)
    return {"bucket": bucket, "label": label,
            "amount": round(amount, 2), "txn_ids": ids}
