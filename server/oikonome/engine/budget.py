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
import math
import re

from .. import localtime
from .compat import MAX_AMOUNT, as_date, as_dict, jsonb
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

# Fewer finished days of ledger than this are too few to call the
# household's rate settled: while the ledger is younger, the verdict does
# not call it under, the forecast burns at the plan and the month lens
# projects the plan. The rate itself is never stretched or shrunk by it —
# see pace_window and specs/short-ledger-pacing.md.
MIN_RATE_DAYS = 7

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
#
# Only a deposit that is still on the ledger repays anything. A refund the
# bank feed withdraws is retired in place and its pair can outlive it (the
# pair waits for a re-numbered twin to carry it to), so every reader that
# nets a pair reads the other side's `removed` — a withdrawn refund leaves
# the charge as unrepaid spend.
NET_AMOUNT = """GREATEST(t.amount - COALESCE((SELECT SUM(pr.amount)
    FROM reimbursements pr
    JOIN transactions pd ON pd.id = pr.reimburse_id AND pd.removed = 0
    WHERE pr.expense_id = t.id AND pr.partial = 1), 0), 0)"""


# NET_AMOUNT as a join, for a reader that pulls many rows at once: the
# correlated subquery above is re-run for every row the query returns, while
# this pre-aggregates the partial links once and probes them by expense id —
# the same SUM over the same rows, COALESCEd to 0 and floored at 0. Use the
# pair together: `FROM transactions t {REIMB_NET_JOIN}` and
# `{NET_AMOUNT_JOINED}`. The alias is its own so it can sit beside any other
# join a reader already carries.
REIMB_NET_JOIN = ("LEFT JOIN (SELECT r.expense_id, SUM(r.amount) AS s "
                  "FROM reimbursements r JOIN transactions rd "
                  "ON rd.id = r.reimburse_id AND rd.removed = 0 "
                  "WHERE r.partial = 1 "
                  "GROUP BY r.expense_id) rnet ON rnet.expense_id = t.id")
NET_AMOUNT_JOINED = "GREATEST(t.amount - COALESCE(rnet.s, 0), 0)"

# A full reimbursement pair on a deposit no more than this short of the
# charge repaid all of it; below that, only the deposit's own amount came back.
REIMB_FULL_SLACK_ABS = 1.00

# What has come back against a charge (`t` = the charge's transactions
# alias): its partial links, plus each full pair — the whole charge when the
# pair's deposit covers it, else only that deposit (a full pair on a smaller
# deposit predates short deposits being linked as partial). The one
# definition of "received" for linking, the flag that clears when enough came
# back, the awaiting list and its alert, so none can disagree about whether a
# charge is still owed. Not a spend figure: NET_AMOUNT nets partial links
# only, because a fully paired charge leaves spend by its category. A pair
# whose deposit has left the ledger (the feed withdrew it) repays nothing,
# as in NET_AMOUNT.
REIMB_RECEIVED = f"""COALESCE((SELECT SUM(CASE
        WHEN rr.partial = 1 THEN rr.amount
        WHEN ABS(dep.amount) >= ABS(t.amount) - {REIMB_FULL_SLACK_ABS}
            THEN ABS(t.amount)
        ELSE LEAST(ABS(t.amount), ABS(dep.amount))
        END)
    FROM reimbursements rr
    JOIN transactions dep ON dep.id = rr.reimburse_id AND dep.removed = 0
    WHERE rr.expense_id = t.id), 0)"""

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


# the keys a first-run budget seed fills in (_seed_config)
_SEED_KEYS = ("food_monthly", "other_monthly", "dynamic_variable_budget")


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
    provisional = seed.pop("_provisional", False)
    cfg = {**seed, **cfg}
    if provisional:
        return cfg
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

    LOCK ORDER — the one place it is written down. A write that holds this
    row lock and also locks an `accounts` row takes THIS row first and the
    account after (the classify doors and `entities.assign_account` open
    the block, then `SELECT ... FROM accounts ... FOR UPDATE` inside it).
    Two writers taking the pair in opposite orders deadlock, and PostgreSQL
    aborts one of them. A writer that has to reach the document after an
    account row — the reconnect adoption, `sync.adopt._adopt_one` — may
    only TRY this lock out of order (`FOR UPDATE NOWAIT`) and must start
    over with nothing held when the try fails; a try never waits, so it
    cannot close a cycle.

    Bill writes take the pending `bill_proposals` rows first, then the
    `bills` rows (each set in id order), and only then any `transactions`
    row: an approval stamps its bill's category onto the rows the bill
    matches while it holds both, so a merchant rename locks the offers and
    bills it rewrites (`bills.lock_merchant_rename`) before it moves a row.
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
        # a provisional budget seed (see _load_config) rides the loaded
        # document but is not the household's: the keys it filled that the
        # stored row lacks are left out of the save unless the block set
        # them, or the first unrelated write would freeze the partial read
        # and the seed would never track the backfill again. The row is
        # read again after the load, which persists a seed that is no
        # longer provisional.
        stored = dict(conn.execute(
            "SELECT config FROM tenant_settings").fetchone()["config"] or {})
        seeded = {k: cfg[k] for k in _SEED_KEYS
                  if k in cfg and k not in stored}
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
        if seeded:
            cfg = {k: v for k, v in cfg.items()
                   if not (k in seeded and v == seeded[k])}
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


def snapshot_month(conn, today: dt.date, *,
                   unchanged_since: dt.datetime | None = None) -> bool:
    """Upsert (today.year, today.month)'s snapshot from the live config,
    plus the bill schedule it ran beside (_freeze_bills). Skipped while no
    variable budget is set — a $0/$0 month is a month the budget feature
    wasn't in use, and freezing it would turn the honest "no budget set"
    state into a verdict against zero.

    Stored SCRUBBED (the export's credential/policy scrub): none of the
    dropped keys feed month_status, and duplicating SMTP/API secrets into
    a second table — one the data export ships verbatim — would widen the
    secret surface for nothing. (Lazy import: engine→sync, same pattern
    as save_config's demoguard hook.)

    `unchanged_since` (an aware datetime) freezes the month only if the
    settings have not been saved after that moment, and returns False
    without writing otherwise. A month closed late — the close sweep missed
    the first hour of the new month — may be frozen from the live settings
    only while they are still the ones the month ended under; an edit made
    since belongs to the new month and must not reach back into the closed
    one. The caller passes the moment the month ended. Any save counts,
    since the document does not say which key changed when: a skipped
    re-freeze leaves the month's earlier snapshot standing, which is the
    safe side."""
    if unchanged_since is not None and unchanged_since.tzinfo is None:
        raise ValueError("unchanged_since must be timezone-aware")
    # the stored budget, not the loaded one: under a month of history the
    # loader carries a provisional guess that was never saved, and freezing
    # it would judge the closed month against a plan nobody made
    if not budgets_set(conn):
        return False
    cfg = load_config(conn)
    if not ((cfg.get("food_monthly") or 0) > 0
            or (cfg.get("other_monthly") or 0) > 0):
        return False
    if unchanged_since is not None:
        # read AFTER the config: a save landing between the two reads then
        # shows here and skips, so the config written below can never be
        # one saved after the moment asked about
        row = conn.execute(
            "SELECT updated_at FROM tenant_settings").fetchone()
        if row is None or row["updated_at"] is None \
                or row["updated_at"] > unchanged_since:
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
    snapshots or the current month; returns the number written. Nothing
    is written while the only budget is the loader's unsaved first-month
    guess."""
    if not budgets_set(conn):
        return 0
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


def ledger_start(conn, excluded: list | None = None) -> dt.date | None:
    """The first day the ledger has data for — months before it are not
    $0 spending, they are no data. Excluded accounts do not count: an
    investment account with years of history must not make a month-old
    checking feed look like a long one."""
    excl = list(excluded or [])
    # the verdict asks on every status, beside the seed and the wizard;
    # the answer moves only with a write to the ledger
    key = _memo_key("ledger_start", excl)
    hit = getattr(conn, "memo_get", lambda k: None)(key)
    if hit is not None:
        return hit[0]
    row = conn.execute(
        "SELECT MIN(t.date) AS d FROM transactions t WHERE t.removed = 0"
        + (" AND NOT (t.account_id = ANY(%s))" if excl else ""),
        (excl,) if excl else ()).fetchone()
    out = as_date(row["d"]) if row and row["d"] else None
    if hasattr(conn, "memo_put"):
        conn.memo_put(key, (out,), *_LEDGER_TABLES)
    return out


def pace_window(conn, today: dt.date, *, excluded: list | None = None,
                closed: bool = False) -> dict:
    """How much of `today`'s month the ledger covers, read ONE way for
    every surface that paces the month: the seed, the suggestion, the
    verdict, the Today tiles and hero, the forecast's spending rate and
    the month lens' projection all derive from this window, so a figure
    taken from the ledger reads on budget on the spending it came from.
    The rule is written out in specs/short-ledger-pacing.md.

    `closed` reads a month that is over (`today` is then any day of it):
    every day through month end is finished. Live, today is in progress
    and is not a finished day."""
    return pace_window_for(ledger_start(conn, excluded), today, closed=closed)


def pace_window_for(first: dt.date | None, today: dt.date, *,
                 closed: bool = False) -> dict:
    """pace_window over a known first ledger day (None: no ledger)."""
    month_start = today.replace(day=1)
    dim = calendar.monthrange(today.year, today.month)[1]
    month_end = month_start.replace(day=dim)
    # finished days: through yesterday while the month is live, through
    # month end once it is over
    done_through = month_end if closed else today - dt.timedelta(days=1)
    # the pace runs from the later of the 1st and the ledger's first day:
    # days before the ledger are no data, not days of no spending
    pace_start = (month_start if first is None
                  else min(max(month_start, first),
                           month_end if closed else today))
    elapsed = max(0, (done_through - pace_start).days + 1)
    paced_days = dim - pace_start.day + 1
    # a ledger that begins after this month ended saw none of it: no day
    # is paced and the month may spend no share of its budget — not the
    # one day the clamp to month end would otherwise leave
    if first is not None and first > month_end:
        elapsed = paced_days = 0
    # finished days of ledger, across month boundaries — what a rate taken
    # from the ledger divides by, and what says whether it is settled
    ledger_days = (max(0, (done_through - first).days + 1)
                   if first is not None else None)
    early = elapsed / dim < EARLY_MONTH_FRAC
    young = ledger_days is not None and ledger_days < MIN_RATE_DAYS
    return {
        "month_start": month_start, "days_in_month": dim,
        "done_through": done_through, "pace_start": pace_start,
        # finished days since pace_start: expected-to-date's days
        "elapsed_days": elapsed,
        # days of the month the ledger has seen, today's included — what a
        # "running $X/day" figure divides its month-to-date spend by
        "seen_days": max(1, elapsed + (0 if closed else 1)),
        # today and every day after it
        "days_left": 0 if closed else dim - today.day + 1,
        # the days the month's budget is spread over, and their share of
        # the month: budget x share is what the month may spend
        "paced_days": paced_days, "share": paced_days / dim,
        "ledger_days": ledger_days or 0,
        # whether the read says anything about behaviour yet: not in the
        # month's first tenth, and not on under a week of ledger
        "settled": not (early or young),
        # the calendar's own finished days, for figures that accrue on the
        # calendar whatever the ledger saw (the plan surplus)
        "calendar_days": (done_through - month_start).days + 1,
    }


def daily_rate(spendable: float, actual: float, spent_today: float,
               days_left: int) -> dict:
    """The "spend at most $X/day" figure every surface shows — the Today
    tiles, the Why, its recovery sentence and the daily push/SMS — so one
    allowance is never two numbers on one page.

    The day's allowance is fixed at midnight: what was left then over the
    days left, today included. While nothing has posted today the rate is
    that remainder over the same days. Once today has spend, today is
    charged at the larger of its allowance and what was spent, and the
    rest spreads over the days after today: spending up to the allowance
    leaves the daily at plan, spending past it reduces it, and what is
    left of today plus rate x rate_days is exactly what is left of the
    month. (Charging only what was spent would hand today's unspent
    allowance out twice — once as "left today", again inside a "raised"
    daily.) On the month's last day there is no later day, so the rate is
    what is left of today.

    Returns rate, rate_days (the days the rate covers), allowance (today's,
    fixed at midnight) and spread_actual: the month-to-date spend the rate
    is the remainder of, which is what a recovery count reads.
    """
    days_left = max(1, days_left)
    # midnight's remainder takes today's net out of `actual` as it is: a
    # refund today is already in `actual`, and "left today" (allowance
    # minus today's net) counts it once — taking out a clamped 0 instead
    # would put it in the allowance as well and hand it out twice
    start_remaining = max(0.0, spendable - (actual - spent_today))
    allowance = start_remaining / days_left
    spent_today = spent_today if spent_today > 0 else 0.0
    if spent_today > 0 and days_left > 1:
        rate_days = days_left - 1
        spread_actual = actual - spent_today + max(spent_today, allowance)
    else:
        rate_days = days_left if spent_today <= 0 else 1
        spread_actual = actual
    rate = max(0.0, spendable - spread_actual) / rate_days
    return {"rate": rate, "rate_days": rate_days, "allowance": allowance,
            "spread_actual": spread_actual}


def ledger_days_in(first: dt.date | None, year: int, month: int,
                   through: dt.date) -> int:
    """Days of (year, month) the ledger covers, from its first day through
    `through` inclusive — a pace window's `done_through`, so a month's
    coverage counts finished days exactly as the verdict does."""
    lo = dt.date(year, month, 1)
    hi = min(lo.replace(day=calendar.monthrange(year, month)[1]), through)
    if first is not None:
        lo = max(lo, first)
    return max(0, (hi - lo).days + 1)


def monthly_rate(spent: float, days: int, window: dict) -> float:
    """Spend over `days` finished days of ledger, as a figure for the
    window's month: the daily rate times the month's days, never
    stretched or shrunk past the days it saw. 0 with no finished day."""
    if days < 1:
        return 0.0
    return spent / days * window["days_in_month"]


def month_coverage(first: dt.date | None, year: int, month: int,
                   today: dt.date) -> float:
    """The share of a calendar month the ledger actually covers, 0..1: a
    month before the ledger starts is 0, the month it starts in counts
    from its first day, and the month in progress counts through today.
    Monthly averages divide by the sum of these, never by a fixed count,
    so a month of history is not read as a third of a three-month window.
    (For pacing the month in progress, read pace_window: it counts
    finished days, through yesterday.)"""
    return (ledger_days_in(first, year, month, today)
            / calendar.monthrange(year, month)[1])


def _seed_skips_as_mechanics(row: dict, merchant_seed) -> bool:
    """Whether the first-run seed leaves a spend row out as bank mechanics.

    The seed often runs BEFORE the first categorize pass finishes, so on a
    category-less source a card payment or a transfer is still
    uncategorized and would count as spend: without this a sparse-source
    instance can seed other_monthly at about twice reality. So the seed
    leaves out exactly the uncategorized rows flow_classify is about to
    route out of spend, judged the way flow_classify judges them.

    That means the same key: flow_match on the row's merchant
    (COALESCE(merchant_name, name)), never on the raw statement line. A
    raw line's "WITHDRAWAL" or "TRANSFER" is how some banks spell any
    debit — "POS WITHDRAWAL EXAMPLE GROCER" under the merchant
    "Example Grocer" is a purchase — and flow_classify reads the raw
    line only for a loan payment, which stays in spend anyway. Of what
    flow_match routes, only transfers and card payments leave spend; any
    other loan payment is spend in month_status.

    A row the feed or a person already categorized is spend exactly as
    month_status counts it, and a bare mechanics string ("ONLINE
    PAYMENT") that no pass routes stays uncategorized and is counted too.
    Skipping any of these seeds the budget below what the verdict
    measures, and a new household reads OVER BUDGET from its first day."""
    if (row.get("stored_category") or "?") not in ("?", ""):
        return False
    fm = merchant_seed.flow_match(row.get("flow_merchant"))
    if not fm:
        return False
    primary, detailed = fm
    return (primary.startswith("TRANSFER_")
            or detailed == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")


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
    month_start = today.replace(day=1)
    recurring = _recurring_bills(conn)
    food = other = 0.0

    def _add(rows):
        nonlocal food, other
        # a bill is matched against the charge the bank sent: a store
        # receipt's parts are asked as their whole charge, never one by one
        billed = {i for whole, idx in bill_candidates(rows)
                  if _match_recurring(whole, recurring) for i in idx}
        for i, r in enumerate(rows):
            if i in billed:
                continue
            if _seed_skips_as_mechanics(r, merchant_seed):
                continue
            if r["is_food"]:
                food += r["amount"]
            else:
                other += r["amount"]

    _add(_spend_rows(conn, start, month_start, excluded=excluded))
    # Divide by the months the ledger actually covers in the window, not
    # by three: a household that imported its last 30 days would otherwise
    # be seeded at a third of its real spending and read OVER BUDGET every
    # day.
    first = ledger_start(conn, excluded)
    w = pace_window_for(first, today)
    closed = 0.0
    days = 0
    m = start
    while m < month_start:
        n = ledger_days_in(first, m.year, m.month, w["done_through"])
        closed += n / calendar.monthrange(m.year, m.month)[1]
        days += n
        m = (m + dt.timedelta(days=32)).replace(day=1)
    if closed >= 1.0:
        food_m, other_m = food / closed, other / closed
    else:
        # under a month of closed history, the month in progress is most of
        # what the ledger knows: its finished days join the rate (today is
        # not over, so neither its spend nor the day counts), scaled to a
        # month at the daily rate the verdict paces by (pace_window)
        _add(_spend_rows(conn, month_start, today, excluded=excluded))
        days += ledger_days_in(first, today.year, today.month,
                               w["done_through"])
        food_m = monthly_rate(food, days, w)
        other_m = monthly_rate(other, days, w)
    return {"food_monthly": round(food_m, -1),
            "other_monthly": round(other_m, -1),
            "dynamic_variable_budget": True,
            # under a complete closed month of history the seed is
            # provisional: it is never saved, so it re-seeds as the feed's
            # backfill and the month in progress arrive instead of freezing
            # a partial read
            "_provisional": closed < 1.0}


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
            # the amounts the bill has been paid at, each with the last
            # date it covers (bill_amount_bands)
            "bands": bill_amount_bands(r["amount"], raw),
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

    Monthly envelopes start every month empty, so nothing is counted for
    them — but they still CLAIM rows here, over the whole envelope list in
    the order `match_envelopes` uses. A monthly merchant envelope wins its
    merchant's rows in the current month; left out of this pass, an annual
    envelope matching by category would absorb those same rows for every
    earlier month and report its pool spent on purchases it never owned.

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
                    [(e["payee"], e["period_months"]) for e in envelopes],
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
    # an envelope with a merchant identity is claimed by merchant id and
    # never reads the row's words, so the year's rows are tokenised only
    # when some envelope still matches on text
    by_words = any(e.get("ids") is None for e in envelopes)
    # Only an ANNUAL envelope's claim is counted here, and the claimer can
    # only hand a row to an envelope that matches it by merchant or by
    # category. A row no annual envelope could match is nobody's business
    # in this pass, whoever ends up claiming it, so it is passed over before
    # its words are tokenised — the year to date is thousands of rows and
    # an annual envelope wants a handful of them. An annual envelope that
    # matches on text cannot be ruled out without the words, so every row
    # takes the full claim then.
    annual_text = any(e.get("ids") is None for e in annual)
    annual_ids = [e["ids"] for e in annual if e.get("ids") is not None]
    annual_cats = {e["match_cat"] for e in annual if e.get("match_cat")}
    for i, r in enumerate(rows):
        if i in claimed:
            continue
        if not annual_text:
            mid = row_merchant_id(r)
            if not ((mid is not None and any(mid in ids for ids in annual_ids))
                    or (annual_cats and r.get("category")
                        and _cat_norm(r["category"]) in annual_cats)):
                continue
        txt = f"{r['payee'] or ''} {r['name'] or ''}".lower()
        toks = _tokens(txt) if by_words else set()
        e = _claim_envelope(envelopes, r, toks, txt)
        if e is not None and e["period_months"] > 1:
            used[e["payee"]] += r["amount"]
    if memo is not None:
        memo[key] = dict(used)
    return used


def _claim_envelope(envelopes, row, toks, txt):
    """Which envelope claims this row? Merchant matches win over category
    matches (mirrors _claim_custom); within each tier, list order breaks
    ties. One shared claimer so prior-used and the month's matching cannot
    disagree about who owns a row."""
    mid = row_merchant_id(row)
    for e in envelopes:
        # an envelope with a merchant identity is _bill_matches' first
        # branch, tested inline: this runs per row of the year to date
        # against every envelope
        ids = e.get("ids")
        if ids is not None:
            if mid is not None and mid in ids:
                return e
        elif _bill_matches(e, toks, txt, mid):
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


# The mean Gregorian year (400 years hold 146,097 days). A cycle measured
# in days or weeks repeats over it exactly, where any one calendar year
# holds a whole extra occurrence some of the time.
MEAN_YEAR_DAYS = 365.2425


def _ends_before(rec: dict, day: dt.date) -> bool:
    """The recurrence carries an end date earlier than `day`. An end date
    that does not parse is no end date, as the occurrence expansion reads
    it."""
    end_on = rec.get("endOn")
    if not end_on:
        return False
    try:
        return dt.date.fromisoformat(str(end_on)[:10]) < day
    except (TypeError, ValueError):
        return False


# The longest cycle evened_monthly_load counts occurrence by occurrence —
# the longest cadence the bills form accepts (twenty years, bills'
# MAX_CYCLE_DAYS) in years and in months. A calendar span wider than
# CALENDAR_SPAN_YEARS is left for a monthly bill's own interval to measure.
MAX_CYCLE_YEARS = 20
MAX_CYCLE_MONTHS = 243
CALENDAR_SPAN_YEARS = 10


def evened_monthly_load(bills: list[dict], today: dt.date) -> float:
    """What a schedule of occurrence bills costs per month, evened over each
    bill's own cycle — the one figure the Bills headline, the month plan's
    avg_bills and the planner quote.

    Counted without the history floor: the floor says a series with no
    payment history has no occurrence before its first due date, which is
    what paid/overdue expectations need, but a load figure that honoured it
    would read a new $2,000 rent due in November as $333 a month. And
    counted over whole cycles, not one calendar year: a bill due every two
    years falls in one calendar year of two, so a single-year count reads it
    as nothing one year and double the next. A one-time bill keeps the
    calendar-year count — it has no cycle to even over.

    A daily or weekly cycle never lines up with calendar years at all: a
    biweekly payday whose phase lands on January 1 falls 27 times that
    year, and a weekly bill falls 53 times in a year that opens on its
    weekday. Counted over one calendar year those years read 27/26 or 53/52
    of the standing cost, then drop back the next January. So such a bill
    costs its cycle's exact rate over a mean Gregorian year. The calendar
    count stays only where the year itself is partial: a series that ends
    before the year does, or one capped per month (the cap, not the cycle,
    then decides how many occurrences a month holds)."""
    total = 0.0
    for bill in bills:
        rec = bill["recurrence"] or {}
        freq = (rec.get("frequency") or "").upper()
        try:
            interval = max(1, int(rec.get("interval") or 1))
        except (TypeError, ValueError):
            interval = 1
        if (freq in ("DAILY", "WEEKLY") and bill.get("anchor")
                and not bill.get("cap")
                and not _ends_before(rec, dt.date(today.year, 12, 31))):
            step = interval * (7 if freq == "WEEKLY" else 1)
            # what each occurrence costs, as month_occurrences plans it
            planned = round(bill["amount"] + bill.get("fee_total", 0), 2)
            total += planned * MEAN_YEAR_DAYS / step
            continue
        if freq == "YEARLY":
            years = interval
        elif freq == "MONTHLY":
            years = math.lcm(interval, 12) // 12
        else:
            years = 1
        if freq == "MONTHLY" and years > CALENDAR_SPAN_YEARS:
            # An interval sharing little with 12 (13, 17, 23 months) only
            # lines up with the calendar after `interval` years, but any
            # `interval` consecutive months hold exactly one of its
            # occurrence months — so that span is a whole cycle too, and
            # the count over it is exact where a calendar span cut short
            # is not (a 13-month bill counted over 10 years reads 97.5%).
            span = min(interval, MAX_CYCLE_MONTHS)
            y, m = today.year, today.month
            due = 0.0
            for _ in range(span):
                due += sum(o["planned"] for o in month_occurrences([bill], y, m))
                y, m = (y, m + 1) if m < 12 else (y + 1, 1)
            total += due / span * 12
            continue
        # Every accepted cadence is counted over its whole cycle. A cycle
        # cut short reads the wrong rate: a bill every 15 years counted over
        # ten reads 1.5x its cost when an occurrence falls inside them and
        # nothing when none does. The cap only bounds what a restored,
        # out-of-contract interval can cost; the bills form never saves one
        # that long.
        years = min(years, MAX_CYCLE_YEARS)
        total += sum(o["planned"] for y in range(today.year, today.year + years)
                     for m in range(1, 13)
                     for o in month_occurrences([bill], y, m)) / years
    return total / 12


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
    median over the complete months, outlier months >2× the initial
    median excluded — rounded to $10. The in-progress month and the month
    the ledger starts in are partial; they speak only when no complete
    month has spend for the bucket (mid-month onboarding still sees this
    month's groceries), averaged over the months they cover. Read-only; the user approves each number
    before it lands in config."""
    cfg = load_config(conn)
    customs = _compiled_buckets(custom_bucket_specs(cfg))
    month_start = today.replace(day=1)
    start = month_start
    for _ in range(6):
        start = (start - dt.timedelta(days=1)).replace(day=1)
    # The month in progress counts through YESTERDAY (pace_window): today
    # is not over, and a rate that took its spend without its day — or a
    # day of nothing posted yet — reads off the verdict's own pace.
    rows = _spend_rows(conn, start, today,
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
    first = ledger_start(conn, excluded_account_ids(cfg))
    if first:
        first_key = _month_key(first)
        months = [mk for mk in months if mk >= first_key]
    # how many finished days of each month the ledger covers: the
    # in-progress month and the month the ledger starts in are partial,
    # and averaging a few days of one with a whole other month halves the
    # suggestion
    w = pace_window_for(first, today)
    cover = {mk: ledger_days_in(first, int(mk[:4]), int(mk[5:]),
                                w["done_through"])
             for mk in months}
    complete = {mk: cover[mk] >= calendar.monthrange(int(mk[:4]),
                                                     int(mk[5:]))[1]
                for mk in months}

    # the rows the Bills line already owns stay out of every variable
    # estimate below (bill_paid_rows says which rows those are), and what
    # an envelope spent past its pool comes back in, as the live month
    # buckets it; an annual pool starts the window part-drawn by the
    # months of its year that came before
    prior = _envelope_prior_used(
        conn, envelopes, start, excluded=excluded_account_ids(cfg),
        bills=[b for b in bills if b.get("anchor")])
    owned, overflow = bill_paid_rows(rows, bills, envelopes, start, today,
                                     prior_used=prior)

    keys = ["food", "other"] + [c["name"] for c in customs]
    totals = {k: {mk: 0.0 for mk in months} for k in keys}
    for i, r in enumerate(rows + overflow):
        mk = _month_key(r["date"])
        if mk not in months or i in owned:
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

    def _suggest_covered(per_month: dict[str, float]) -> float:
        """Median over the COMPLETE months; partial months (the one in
        progress, the one the ledger starts in) stay out of it. With no
        complete month, or a bucket only the partial months touch, the
        partial spend is the daily rate over the finished days it covers,
        scaled to this month (monthly_rate) — the rate the verdict paces
        by, so accepting the suggestion on steady spending reads on budget
        this month and the next."""
        full = [v for mk, v in per_month.items() if complete[mk]]
        sug = _suggest_monthly(full) if full else 0.0
        if sug > 0:
            return sug
        part = [mk for mk in per_month if not complete[mk]]
        spent = sum(per_month[mk] for mk in part)
        if spent <= 0:
            return 0.0
        return round(monthly_rate(spent, sum(cover[mk] for mk in part), w),
                     -1)

    suggestions: dict = {"buckets": {}}
    samples: dict = {}
    for k in keys:
        suggestion = _suggest_covered(totals[k])
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
    # (an envelope's overflow is left out: its pool already names where
    # that money goes, and "envelope overflow" is no category to carve)
    by_cat: dict[str, dict[str, float]] = {}
    for i, r in enumerate(rows):
        mk = _month_key(r["date"])
        if mk not in months or r["is_food"] or i in owned:
            continue
        cat = (r["category"] or "?").replace("_", " ")
        by_cat.setdefault(cat, {m2: 0.0 for m2 in months})[mk] += r["amount"]
    candidates = []
    for cat, per_month in by_cat.items():
        if cat in ("?", "OTHER") or cat.upper() in claimed \
                or cat.replace(" ", "_").upper() in claimed:
            continue
        # Same sparse-month logic as food/other — don't zero a real category
        med = _suggest_covered(per_month)
        if med >= 75:
            candidates.append({"name": cat.title(),
                               "category": cat.replace(" ", "_").upper(),
                               "monthly": med})
    candidates.sort(key=lambda c: -c["monthly"])
    # monthly bill load for the savings-surplus prefill — same
    # evened_monthly_load the month plan uses. Approved bills only for
    # the occurrence engine (pending rows lack recurrence anchors); when
    # none are approved yet (mid-wizard), fall back to pending proposals'
    # monthly-equivalent — same pattern as income_monthly above.
    # and the same bill set: a hand-tracked card-payment bill leaves the
    # plan while live card debt is there for the card machinery to pay
    # (drop_card_pay_bills), so it leaves the planner's prefill too
    approved = drop_card_pay_bills(
        conn, [b for b in bills if not b.get("pending")], cfg=cfg)
    avg_bills = round(evened_monthly_load(approved, today)
                      + sum(e["monthly"] for e in envelopes), 2)
    bills_count = len(approved) + len(envelopes)
    if avg_bills <= 0:
        pend_mo, pend_n = _pending_expense_monthly(conn)
        if pend_mo > 0:
            avg_bills = pend_mo
            bills_count = pend_n
    # the history behind the suggestions, for the planner's one line:
    # finished days of ledger inside the window, and whether no complete
    # month was there to median (every figure is a partial month scaled up)
    history_days = sum(cover.values())
    # not one finished day of ledger (it began today): every rate would
    # be $0 over nothing, so there is no suggestion at all — the planner
    # says to come back tomorrow rather than offer $0 as a budget
    thin = history_days < 1
    if thin:
        suggestions = {"buckets": {}, **({"income_monthly":
                                          suggestions["income_monthly"]}
                                         if "income_monthly" in suggestions
                                         else {})}
        candidates = []
    return {"months": months, "suggestions": suggestions, "samples": samples,
            "history_days": history_days,
            "not_enough_history": thin,
            "history_scaled": not any(complete.values()),
            "min_rate_days": MIN_RATE_DAYS,
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
    (so 'ox mobile' matches 'OX MOBILE' but not 'box mobile' or 'fox-mobile')."""
    return " " + re.sub(r"[^a-z0-9]+", " ",
                        join_apostrophes((text or "").lower())).strip() + " "


def _merchant_phrase(merchant: str | None) -> str | None:
    """A word-boundary-padded phrase to SUBSTRING-match, used when a merchant's
    LEADING word is a too-short-to-tokenize disambiguator ('ox mobile').
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
      • phrase        'ox mobile' — word-boundary substring
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


# A past amount keeps matching charges dated up to this long after the day
# it stopped being the bill's amount: the person edits the bill when they
# notice the new price, which is usually after the first charge at it, and
# the last charge at the old price can post a few weeks before that edit.
AMOUNT_HISTORY_GRACE_DAYS = 35
AMOUNT_HISTORY_MAX = 12


def bill_amount_bands(amount, raw) -> list[tuple[float, float, dt.date | None]]:
    """(reference amount, tolerance, last charge date it covers) for the
    bill's current amount (no end date) and each amount it had before
    (`raw.amount_history`: [{"amount", "until"}], oldest first). A bill
    whose price changed keeps recognising the charges it was paid with
    at the old price — otherwise every edit of the amount turns the
    months before it into unmatched spending. raw is JSON a restore ZIP
    hands over verbatim: a malformed entry is skipped, never a crash."""
    ref = abs(float(amount or 0))
    bands = [(ref, bill_tolerance(ref), None)]
    for h in amount_history_of(raw):
        a = h["amount"]
        until = as_date(h["until"]) + dt.timedelta(days=AMOUNT_HISTORY_GRACE_DAYS)
        if abs(a - ref) > 0.005:
            bands.append((a, bill_tolerance(a), until))
    return bands


def amount_history_of(raw) -> list[dict]:
    """The bill's past amounts that are real past prices, as
    [{"amount": positive dollars, "until": ISO day}], oldest first, at
    most AMOUNT_HISTORY_MAX. Anything else in raw.amount_history — an
    object instead of a list, an infinite or overflowing amount, a day
    that does not parse — is dropped: an infinite amount would build a
    band of infinite tolerance that claims every charge at the payee, and
    a raise here takes down every matcher in the household. The one
    reader for the band builder, the save paths that carry history
    forward, and the restore that cleans an archive's raw."""
    try:
        d = as_dict(raw)
    except ValueError:
        return []
    hist = d.get("amount_history") if isinstance(d, dict) else None
    if not isinstance(hist, list):
        return []
    out = []
    for h in hist:
        if not isinstance(h, dict) or isinstance(h.get("amount"), bool):
            continue
        try:
            a = abs(float(h.get("amount")))
            until = as_date(h.get("until"))
        except (TypeError, ValueError, OverflowError):
            continue
        if until is None or not math.isfinite(a) or a <= 0 or a > MAX_AMOUNT:
            continue
        out.append({"amount": round(a, 2), "until": until.isoformat()})
    return out[-AMOUNT_HISTORY_MAX:]


def amount_fits(bands, amt, date=None) -> bool:
    """Is a charge of `amt` (positive dollars) on `date` inside the bill's
    current band, or inside a past band that still covers that date?"""
    return _in_bands(bands, abs(float(amt or 0)), as_date(date) if date else None)


def _in_bands(bands, a: float, d: dt.date | None) -> bool:
    """amount_fits on an amount and date already normalised — the form
    the matching loops call per row and bill."""
    for ref, tol, until in bands:
        if abs(a - ref) <= tol and (until is None or d is None or d <= until):
            return True
    return False


def amount_gap(bands, amt) -> float:
    """Distance from the nearest band's reference — the tie-breaker when
    two bills claim one charge."""
    a = abs(float(amt or 0))
    return min(abs(a - ref) for ref, _, _ in bands)


def _bands_of(bill: dict):
    return bill.get("bands") or [(bill["amount"], bill_tolerance(bill["amount"]), None)]


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
    names_all = sorted({r["name"].lower() for r in refs})
    mkey = ("resolve_lookup", tuple(ids), tuple(names_all))
    hit = getattr(conn, "memo_get", lambda k: None)(mkey)
    if hit is not None:
        return dict(hit[0]), dict(hit[1])
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
    if hasattr(conn, "memo_put"):
        conn.memo_put(mkey, (dict(current), dict(by_name)), "merchants")
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
    # the same schedule is resolved several times in one request (the
    # verdict, the forecast, the bills-left tile, the health pills), and
    # the answer moves only with a write — memoised per checkout on the
    # bills' own refs
    key = ("bill_merchant_ids",
           tuple(sorted((p, repr(sorted(map(repr, refs))),
                         bool(raw.get("merchant_pinned")),
                         bool(raw.get("merchant_offered")))
                        for p, (raw, refs) in by.items())))
    hit = getattr(conn, "memo_get", lambda k: None)(key)
    if hit is not None:
        return dict(hit)
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
    if hasattr(conn, "memo_put"):
        conn.memo_put(key, dict(out), "merchants", "bills")
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
        if amount_fits(_bands_of(bill), txn["amount"], txn.get("date")):
            return bill
    return None


# How far a charge may drift from a bill's amount and still be read as that
# bill when the planner sizes variable budgets: half the bill, wider than
# the live verdict's band so a utility in its expensive season still reads
# as the utility, never as a variable purchase the suggestion then budgets
# for a second time.
SUGGEST_DRIFT_SHARE = 0.5


def _drift_bands(bill: dict):
    return [(ref, max(tol, ref * SUGGEST_DRIFT_SHARE), until)
            for ref, tol, until in _bands_of(bill)]


def bill_paid_rows(rows, bills: list[dict], envelopes: list[dict],
                   since: dt.date, until: dt.date, *,
                   prior_used: dict[str, float] | None = None
                   ) -> tuple[set[int], list[dict]]:
    """The `rows` indices the Bills line already owns, for budget
    SUGGESTIONS — the rows that pay a bill, never every row at its merchant
    — and the envelope overflow slices that go back to variable spend.

    A row pays a scheduled bill the way the live verdict decides it
    (match_occurrences): one charge per occurrence, near its due date, its
    amount inside the bill's band — the band widened here by
    SUGGEST_DRIFT_SHARE for seasonal drift. A merchant test alone would take
    every Amazon order out of "other" because a Prime subscription bills
    there, and every grocery run out of "food" for a yearly membership fee;
    the live month then counts all of that as variable spend and reads over
    the budget it was given. A bill with no schedule yet (a pending proposal
    mid-wizard) has no occurrences to match, so its merchant's rows inside
    the drift band stand in.

    Envelopes are claimed month by month with the live claimer
    (match_envelopes), so the suggestion and the live month agree on what
    is variable: a category-matched envelope owns its category's rows at
    any merchant (its pool is already on the Bills line, and budgeting
    those rows again as "other" counts the money twice), and what a month
    spends past the pool is variable, as month_status routes it: every
    claimed row is owned, and what it spent beyond the cap comes back as an
    overflow slice (a row wholly past the cap is one whole slice, as in the
    live month). `prior_used` is what each annual envelope drew
    from its calendar year's pool before `since`; the months inside the
    window add to it as they are claimed, and January starts it over."""
    sched = [dict(b, bands=_drift_bands(b)) for b in bills if b.get("anchor")]
    loose = [(b, _drift_bands(b)) for b in bills if not b.get("anchor")]
    # one month either side of the window: a payment on its first or last
    # days may belong to an occurrence just outside it
    first = (since.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
    last = (until.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    occs = [o for y, m in _months_between(first, last)
            for o in month_occurrences(sched, y, m)]
    owned, _ = match_occurrences(rows, occs)
    owned = set(owned)
    overflow: list[dict] = []
    if not loose and not envelopes:
        return owned, overflow
    if loose:
        # a store receipt's parts are judged as the one charge they came
        # from, as bill_candidates puts them back together for the verdict
        whole: dict = {}
        for r in rows:
            if store_part(r):
                whole[r["txn_id"]] = round(whole.get(r["txn_id"], 0.0)
                                           + r["amount"], 2)
        for i, r in enumerate(rows):
            if i in owned:
                continue
            text = f"{r['payee'] or ''} {r['name'] or ''}"
            toks = _tokens(text)
            mid = row_merchant_id(r)
            amt = (whole.get(r["txn_id"], r["amount"]) if store_part(r)
                   else r["amount"])
            if any(_bill_matches(b, toks, text, mid)
                   and amount_fits(bands, amt, r.get("date"))
                   for b, bands in loose):
                owned.add(i)
    if not envelopes:
        return owned, overflow
    by_month: dict[tuple[int, int], list[int]] = {}
    for i, r in enumerate(rows):
        d = as_date(r["date"])
        by_month.setdefault((d.year, d.month), []).append(i)
    used = {e["payee"]: (prior_used or {}).get(e["payee"], 0.0)
            for e in envelopes if e["period_months"] > 1}
    for (y, m) in sorted(by_month):
        if m == 1:
            used = {k: 0.0 for k in used}
        idx = by_month[(y, m)]
        month_rows = [rows[i] for i in idx]
        taken = {j for j, i in enumerate(idx) if i in owned}
        env_matched, states = match_envelopes(month_rows, envelopes, taken,
                                              prior_used=used)
        # exactly the live month's split: every row an envelope claimed
        # leaves the variable side, and what it spent past the pool comes
        # back as the overflow slice month_status buckets as food/other
        owned.update(idx[j] for j in env_matched)
        for st in states:
            overflow.extend(st["overflow_rows"])
            if st["bill"]["payee"] in used:
                used[st["bill"]["payee"]] += st["used"]
    return owned, overflow


def store_part(r) -> bool:
    """True for a part a store's receipt wrote (source 'amazon', 'costco').

    A bill is a whole charge the bank sent. A person who splits a charge is
    saying it was really several — that stands in for charges and may pay a
    bill — but a receipt's parts are only how the items read: a $15 coffee
    line inside a $100 order must never pay a $14.99 subscription that was
    never charged."""
    src = r.get("split_source")
    return src is not None and src != "user"


def bill_candidates(rows) -> list[tuple[dict, list[int]]]:
    """The rows a bill may claim, each with the `rows` indices it stands
    for. A row the bank sent whole, or a person's split part, is its own
    candidate; a store receipt's parts are put back together into the one
    charge they came from (summed, the first part's columns otherwise), so
    the charge still pays the bill it paid before the receipt split it —
    and claiming it claims every part."""
    out: list[tuple[dict, list[int]]] = []
    whole: dict = {}
    for i, r in enumerate(rows):
        if not store_part(r):
            out.append((r, [i]))
            continue
        hit = whole.get(r["txn_id"])
        if hit is None:
            row = dict(r)
            row["split_line"] = row["split_source"] = None
            hit = whole[r["txn_id"]] = (row, [i])
            out.append(hit)
        else:
            hit[0]["amount"] = round(hit[0]["amount"] + r["amount"], 2)
            hit[1].append(i)
    return out


# how far outside a bill's band envelope match_occurrences' pre-filter
# still hands a charge to the exact amount test (see there)
_GATE_SLACK = 0.01


def match_occurrences(rows, occurrences) -> tuple[set[int], list]:
    """Assign spend transactions to scheduled bill occurrences, one txn per
    occurrence, best matches first (date proximity, then amount closeness).
    A store receipt's parts are matched as the whole charge they came from
    (bill_candidates); the returned indices are always into `rows`."""
    cands = bill_candidates(rows)
    candidates = []
    # The text and amount tests depend on the BILL, not the occurrence, and
    # one bill fans out into an occurrence per month of the candidate
    # window (a year of them on the envelope lookback). Test each row
    # against each distinct bill once; the per-occurrence part is only the
    # date gap.
    distinct: dict[int, dict] = {}
    for occ in occurrences:
        distinct.setdefault(id(occ["bill"]), occ["bill"])
    # The amount gate is a float compare and rules out nearly every row
    # against nearly every bill; the words test tokenises the row's text.
    # So the amount is tested first, per distinct bill, and a row is
    # tokenised only when some bill's amount admits it — on the
    # year-to-date window a household's ledger is tens of thousands of
    # rows against a dozen bills, and almost none of them are ever near
    # a bill's amount.
    #
    # Each bill's bands also collapse to one [lo, hi] envelope, compared
    # inline before amount_fits is called at all: the year-to-date pass
    # tests every row against every bill, and the call per pair — not the
    # compare — was the cost. The envelope is only a pre-filter, widened by
    # a hair so float rounding can never drop a pair the exact test admits;
    # amount_fits still decides every pair that passes it.
    gates = []
    for bid, bill in distinct.items():
        bands = _bands_of(bill)
        lo = min(ref - tol for ref, tol, _ in bands) - _GATE_SLACK
        hi = max(ref + tol for ref, tol, _ in bands) + _GATE_SLACK
        gates.append((bid, bill, bands, lo, hi))
    # each bill's own occurrences, so a row that pays a bill is dated
    # against that bill's months only, not against every occurrence of
    # every bill in the window
    occs_of: dict[int, list[tuple[int, dict]]] = {}
    for oidx, occ in enumerate(occurrences):
        occs_of.setdefault(id(occ["bill"]), []).append((oidx, occ))
    for ridx, (r, _) in enumerate(cands):
        amt = r["amount"]
        a = abs(float(amt or 0))
        for gate in gates:
            if gate[3] <= a <= gate[4]:
                break
        else:
            continue
        txn_date = as_date(r["date"])
        # amount_fits(bands, amt, txn_date), on the values it would derive
        near = [(bid, bill) for bid, bill, bands, lo, hi in gates
                if lo <= a <= hi
                and _in_bands(bands, a, txn_date if txn_date else None)]
        if not near:
            continue
        txn_text = f"{r['payee'] or ''} {r['name'] or ''}".lower()
        # a bill with a merchant identity is decided by the row's merchant
        # id alone, so the row's words are tokenised only for a bill that
        # still matches on text
        txn_tokens = None
        txn_mid = row_merchant_id(r)
        fits: dict[int, bool] = {}
        for bid, bill in near:
            ids = bill.get("ids")
            if ids is not None:
                # _bill_matches' identity branch, inline (see above)
                fits[bid] = txn_mid is not None and txn_mid in ids
                continue
            if txn_tokens is None:
                txn_tokens = _tokens(txn_text)
            # every token of the bill's canonical merchant must appear in
            # the txn — 'harbor refresh' won't swallow 'Alder Harbor' —
            # unless the ledger already displays the row under the bill's
            # merchant (a rename the words no longer carry)
            fits[bid] = _bill_matches(bill, txn_tokens, txn_text, txn_mid)
        for bid, ok in fits.items():
            if not ok:
                continue
            for oidx, occ in occs_of.get(bid, ()):
                day_gap = abs((txn_date - occ["due"]).days)
                if day_gap > occ["window"]:
                    continue
                candidates.append((day_gap,
                                   amount_gap(_bands_of(occ["bill"]), r["amount"]),
                                   ridx, oidx))
    candidates.sort()
    matched_rows: set[int] = set()
    used_occ: set[int] = set()
    taken: set[int] = set()
    for _, _, ridx, oidx in candidates:
        if ridx in taken or oidx in used_occ:
            continue
        taken.add(ridx)
        used_occ.add(oidx)
        occurrences[oidx]["txn"] = cands[ridx][0]
        matched_rows.update(cands[ridx][1])
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
                # a receipt's line is not a charge, so not a fee either
                if (ridx in matched_rows or r["amount"] <= 0
                        or store_part(r)):
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


def withdrawn_rows(conn, since: dt.date, until: dt.date, cfg: dict, *,
                   memo: dict | None = None) -> list[dict]:
    """The posted charges the bank feed withdrew between `since` and
    `until`, whole (a split's parts are one charge to a bill). The ONE
    reader of that evidence: the Today line (mark_withdrawn) and the Bills
    health verdict (bills._withdrawn_rows) both load it here, so they search
    the same rows — the Accounts page's excluded accounts dropped
    (excluded_account_ids), and only the account types a bill is paid from.
    An excluded account supplies no evidence anywhere, as it supplies no
    spend."""
    return _spend_rows(conn, since, until, excluded=excluded_account_ids(cfg),
                       slim=True, parts=False, memo=memo, withdrawn=True)


def _months_between(first: dt.date, last: dt.date) -> list[tuple[int, int]]:
    """(year, month) for every calendar month from `first`'s to `last`'s."""
    out, y, m = [], first.year, first.month
    while (y, m) <= (last.year, last.month):
        out.append((y, m))
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    return out


def mark_withdrawn(conn, occs: list[dict], today: dt.date, *,
                   since: dt.date, cfg: dict,
                   memo: dict | None = None) -> None:
    """Set `withdrawn_on` on every unpaid occurrence whose charge the bank
    feed took back.

    A feed can withdraw rows it already posted (a "removed" delta), and a
    bill that WAS charged then reads as never paid. When no live row pays
    an occurrence but a withdrawn posted row would have — same merchant,
    amount and date-window rules, the one matcher — the honest word is
    "withdrawn by the bank feed", not "overdue". It changes the wording
    only: the occurrence stays unpaid for every sum, because the money is
    not in the ledger either way. Nothing is stored; the moment a live
    matching charge lands the occurrence is simply paid.

    Every occurrence joins the match, the paid ones as decoys: a feed that
    re-ids a row leaves a withdrawn twin of a live payment, and that twin
    must bind to the occurrence it duplicates rather than vouch for its
    unpaid neighbour. The evidence window reaches back before the month
    (`since`), so the earlier months' occurrences of the same bills join as
    decoys too: a withdrawn twin of LAST month's payment sits nearer last
    month's due date and binds there, instead of reading as this month's
    charge taken back.

    The word has a shelf life — the occurrence's own match window, the
    furthest a charge may sit from its due date and still count. A charge
    withdrawn longer ago than that is not coming back, and the bill reads
    overdue like any other."""
    for occ in occs:
        occ["withdrawn_on"] = None
    if not any(o["txn"] is None and not o["floored"] for o in occs):
        return
    gone = withdrawn_rows(conn, since, today + dt.timedelta(days=1), cfg,
                          memo=memo)
    if not gone:
        return
    twins = [dict(o, txn=None, companions=[]) for o in occs]
    distinct = list({id(o["bill"]): o["bill"] for o in occs}.values())
    month_start = today.replace(day=1)
    decoys = [d for y, m in _months_between(since, month_start
                                            - dt.timedelta(days=1))
              for d in month_occurrences(distinct, y, m)]
    match_occurrences(gone, decoys + twins)
    for occ, twin in zip(occs, twins):
        if occ["txn"] is not None or occ["floored"] or twin["txn"] is None:
            continue
        on = as_date(twin["txn"]["date"])
        if (today - on).days <= occ["window"]:
            occ["withdrawn_on"] = on


def overdue_entries(st: dict) -> list[tuple]:
    """`overdue_unpaid` as (payee, planned, due, withdrawn_on) tuples. The
    fourth slot is newer than the other three, and a status built or
    stored before it existed (a cached payload, or one saved by an older
    version) carries
    three: those read as never posted rather than failing to unpack."""
    return [(o[0], o[1], o[2], o[3] if len(o) > 3 else None)
            for o in st.get("overdue_unpaid") or []]


# display label for a transaction's account: the institution name is prefixed
# only when the account's own name does not already start with it
# ("Northwind Checking 1234" keeps its name, "Everyday 5678" gains one)
ACCT_LABEL_SQL = (
    "TRIM(CASE WHEN LOWER(COALESCE(a.display_name,a.name,'')) LIKE "
    "LOWER(substr(i.institution_name||' ',1,strpos(i.institution_name||' ',' ')-1))||'%%' "
    "THEN COALESCE(a.display_name,a.name,'') "
    "ELSE TRIM(COALESCE(i.institution_name,'')||' '||COALESCE(a.display_name,a.name,'')) END "
    "|| CASE WHEN COALESCE(a.mask,'')!='' THEN ' '||a.mask ELSE '' END)")


# the key sync.base.mark_removed stamps into a row's `raw` when the
# aggregator's removal delta retires it — the one mark that tells a charge
# the bank feed withdrew from a row retired for any other reason
FEED_REMOVED_KEY = "feed_removed_at"

# "withdrawn more than N days ago" on alias `t`, one placeholder for N. A
# restored archive keeps a row's raw record as it was written, so the stamp
# can be anything; one that does not read as a time is never old enough
# (the row is skipped and named by `log_unreadable_removed_stamps`) rather
# than a cast error that aborts the sweep for every other row.
FEED_REMOVED_OLDER_SQL = (
    f" AND (CASE WHEN pg_input_is_valid(t.raw->>'{FEED_REMOVED_KEY}',"
    f" 'timestamptz') THEN (t.raw->>'{FEED_REMOVED_KEY}')::timestamptz"
    " END) < now() - make_interval(days => %s)")


def log_unreadable_removed_stamps(conn, txn_ids: list[str], what: str) -> None:
    """Warn about the rows among `txn_ids` whose withdrawal stamp does not
    read as a time, which the age test above skips for good: someone has to
    look at them, and a skip that says nothing would hide them forever."""
    bad = [r["id"] for r in conn.execute(
        f"""SELECT t.id FROM transactions t
             WHERE t.id = ANY(%s) AND t.raw ? '{FEED_REMOVED_KEY}'
               AND NOT pg_input_is_valid(t.raw->>'{FEED_REMOVED_KEY}',
                                         'timestamptz')""",
        (list(txn_ids),)).fetchall()]
    if bad:
        import logging
        logging.getLogger("oikonome.budget").warning(
            "%s: skipped %d withdrawn row(s) whose withdrawal time is "
            "unreadable: %s", what, len(bad), ", ".join(map(str, bad[:20])))


def release_withdrawn_pairs(conn, txn_ids: list[str], *,
                           older_than_days: int | None = None) -> int:
    """Release the reimbursement pairs still on rows the bank feed withdrew,
    the way an unlink does, so the side left on the ledger gets back the
    category its link set aside. Returns how many pairs were removed.

    For the nightly re-anchor sweep, after its twin search found no
    re-numbered twin to carry each row onto. `older_than_days` keeps a row
    whose withdrawal is more recent than that: its twin can still be on its
    way, and a released pair cannot follow it. The readers already treat such a pair as absent, but a full
    pair also stamped the surviving charge TRANSFER_OUT (or the deposit
    TRANSFER_IN), and that stamp keeps the charge out of spend for a refund
    that never arrived until the pair is gone. Only posted rows the feed
    itself withdrew qualify: a retired pending hold is the ordinary
    pending-to-posted swap, and a row a merge or restate retired lives on
    under another id."""
    ids = [i for i in dict.fromkeys(txn_ids or []) if i]
    if not ids:
        return 0
    age = ""
    params: list = [ids]
    if older_than_days is not None:
        age = FEED_REMOVED_OLDER_SQL
        params.append(int(older_than_days))
        log_unreadable_removed_stamps(conn, ids, "release_withdrawn_pairs")
    dead = [r["id"] for r in conn.execute(
        f"""SELECT t.id FROM transactions t
             WHERE t.id = ANY(%s) AND t.removed = 1 AND t.pending = 0
               AND t.raw ? '{FEED_REMOVED_KEY}'{age}
               AND EXISTS (SELECT 1 FROM reimbursements r
                            WHERE r.expense_id = t.id
                               OR r.reimburse_id = t.id)""",
        params).fetchall()]
    if not dead:
        return 0
    from ..web.data import detach_pairs
    return detach_pairs(conn, dead)

# what a month's ledger rows are read from, for the checkout memo
_LEDGER_TABLES = ("transactions", "transaction_splits", "reimbursements",
                  "manual_categories", "accounts", "items", "merchants",
                  "merchant_canonical", "account_links", "tenant_settings")


def _spend_rows(conn, since: dt.date, until: dt.date,
                excluded: list | None = None, *, slim: bool = False,
                memo: dict | None = None, parts: bool = True,
                withdrawn: bool = False) -> list[dict]:
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

    parts=True (the default) fans a split row out into one row per part —
    its category, its share of the amount, the same txn_id, a `split_line`
    and a `split_source` — exactly as if the bank had sent that many
    charges. That is what makes the verdict, the buckets and the Why see
    the groceries half of a mixed receipt as food and the rest as other; a
    reader that wants the charge whole (the anomaly pass asks whether the
    CHARGE was unusual) passes parts=False. The bill matcher is the one
    reader that tells the sources apart (see bill_candidates): a person's
    split stands in for charges, a store receipt's reading does not.

    withdrawn=True reads the other side of the ledger: POSTED rows the bank
    feed later took back (removed by the aggregator's removal delta, never
    by a merge or restate — see FEED_REMOVED_KEY). They are never spend — only
    the evidence mark_withdrawn needs to tell "never charged" from "charged,
    then withdrawn by the feed". A removed PENDING row is the ordinary
    pending-to-posted swap and says nothing about a bill."""
    key = _memo_key("spend_rows", since, until, excluded, slim, parts,
                    withdrawn)
    if memo is not None and key in memo:
        return memo[key]
    # the checkout's own memo behind the request memo: a Today request
    # loads the same month twice through callers that carry no memo
    # (the forecast, the bills-left tile), and the rows move only with
    # a write to what they are read from
    hit = getattr(conn, "memo_get", lambda k: None)(key)
    if hit is not None:
        if memo is not None:
            memo[key] = hit
        return hit
    from . import links
    excluded = list(excluded or []) + links.shadow_ids(conn)
    excl_sql = "AND NOT (t.account_id = ANY(%s))" if excluded else ""
    params: tuple = ((since, until, list(excluded)) if excluded
                     else (since, until))
    # a withdrawal is what the aggregator's removal delta did
    # (sync.base.mark_removed stamps it on the row): a row retired by a
    # merge, a file restate or a full-replace push was never taken back by
    # the bank, and its charge usually lives on under another id. Only the
    # account types a bill is paid from, as the Bills page's ledger reads.
    live_sql = (f"t.removed = 1 AND t.pending = 0 AND t.raw ? '{FEED_REMOVED_KEY}'"
                " AND t.account_id IN (SELECT id FROM accounts"
                " WHERE type IN ('depository', 'credit'))" if withdrawn
                else "t.removed = 0")
    if parts:
        cat_disp, cat_key = _cats.PART_CAT_DISPLAY, f"COALESCE({_cats.PART_CAT}, '?')"
        amount, food = _cats.part_net(NET_AMOUNT_JOINED), PART_FOOD_SQL
        split_join, split_line = _cats.SPLIT_JOIN, "sp.line"
        split_source = "sp.source"
    else:
        cat_disp = "REPLACE(COALESCE(t.category_override, t.category_primary,'?'),'_',' ')"
        cat_key = "COALESCE(t.category_override, t.category_primary,'?')"
        amount, food = NET_AMOUNT_JOINED, FOOD_SQL
        split_join, split_line = "", "NULL::integer"
        split_source = "NULL::text"
    if slim:
        cols = f"""t.id AS txn_id, t.date, {amount} AS amount,
                   {merchant_sql.RAW_KEY} AS payee, t.name,
                   -- the key the flow pass routes bank mechanics by
                   COALESCE(t.merchant_name, t.name) AS flow_merchant,
                   t.merchant_id::text AS merchant_id,
                   {cat_disp} AS category,
                   -- `category` is the DISPLAY form (underscores blown out);
                   -- the stored key rides alongside so a surface that turns a
                   -- category label into a ledger filter has the value the
                   -- ledger actually compares against
                   {cat_key} AS stored_category,
                   t.category_detailed,
                   {food} AS is_food, t.pending,
                   {split_line} AS split_line,
                   {split_source} AS split_source"""
        joins = f"{REIMB_NET_JOIN} {split_join}"
    else:
        cols = f"""t.id AS txn_id, t.date, {amount} AS amount,
                   {merchant_sql.RAW_KEY} AS payee, t.name,
                   -- the key the flow pass routes bank mechanics by
                   COALESCE(t.merchant_name, t.name) AS flow_merchant,
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
                   -- the feed's detailed category: the reports tell a
                   -- mortgage from the other loan payments by it
                   t.category_detailed,
                   {food} AS is_food, t.pending,
                   {split_line} AS split_line,
                   -- who wrote the part (NULL on an unsplit row): only a
                   -- person's split stands in for charges at a bill
                   {split_source} AS split_source,
                   {ACCT_LABEL_SQL} AS account"""
        joins = f"""{REIMB_NET_JOIN}
                 LEFT JOIN accounts a ON a.id = t.account_id
                 LEFT JOIN items i ON i.id = a.item_id
                 {merchant_sql.MC_JOIN}
                 {split_join}"""
    out = conn.execute(
        f"""SELECT {cols}
            FROM transactions t
                 {joins}
            WHERE {live_sql} {SPEND_ONLY_SQL}
              AND t.date >= %s AND t.date < %s
              {PERSONAL_ONLY_SQL}
              {excl_sql}
            ORDER BY t.amount DESC, {split_line}""",
        params).fetchall()
    if hasattr(conn, "memo_put"):
        conn.memo_put(key, out, *_LEDGER_TABLES)
    if memo is not None:
        memo[key] = out
    return out


def _inflow_rows(conn, since: dt.date, until: dt.date,
                 excluded: list | None = None, *,
                 memo: dict | None = None,
                 include_pending: bool = False) -> list[dict]:
    """Deposit-side twin of _spend_rows for INCOME occurrence matching:
    posted inflows on depository accounts, amounts flipped positive
    (match_occurrences compares against the bill's positive dollars).
    Transfers are excluded — a savings→checking move that happens to be
    paycheck-sized must not mark the paycheck received. `memo`: the
    request-scoped cache, as on _spend_rows. `include_pending` also
    admits deposits the feed still shows as pending — only for asking
    whether a paycheck is already in the AVAILABLE balance, never as
    evidence that it was received (a pending row can still be withdrawn)."""
    key = _memo_key("inflow_rows", since, until, excluded, include_pending)
    if memo is not None and key in memo:
        return memo[key]
    from . import links
    excluded = list(excluded or []) + links.shadow_ids(conn)
    excl_sql = "AND NOT (t.account_id = ANY(%s))" if excluded else ""
    params: tuple = ((since, until, list(excluded)) if excluded
                     else (since, until))
    pending_sql = "" if include_pending else "AND t.pending = 0"
    out = conn.execute(
        f"""SELECT t.id AS txn_id, t.date, -t.amount AS amount,
                   {merchant_sql.RAW_KEY} AS payee, t.name,
                   -- the merchant the ledger files the row under, so an
                   -- income series follows its payer like a bill does
                   t.merchant_id::text AS merchant_id
            FROM transactions t
                 JOIN accounts a ON a.id = t.account_id
            WHERE t.removed = 0 {pending_sql} AND t.amount < 0
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
    phantom paycheck tomorrow. A paycheck due TODAY is different: its day
    has not passed, so it is re-dated to tomorrow like a bill due today.

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
    # A paycheck due today that the feed already shows as a PENDING deposit
    # is, at many banks, already inside the available balance the forecast
    # starts from; re-dating it to tomorrow would walk the same deposit in
    # a second time. Only the payday re-date asks this — posted deposits
    # stay the evidence of receipt everywhere else, because a pending row
    # can still be withdrawn. Matched on copies so the posted assignment
    # above is untouched.
    pending_hit: set[int] = set()
    if income and any(
            o["due"] == today and o["txn"] is None and not o["floored"]
            for o in occs[n_prev:n_cur]):
        pend_rows = _inflow_rows(conn, since, today + dt.timedelta(days=1),
                                 excluded=excluded_account_ids(cfg),
                                 memo=memo, include_pending=True)
        shadow = [dict(o, txn=None) for o in occs]
        match_occurrences(pend_rows, shadow)
        pending_hit = {i for i in range(n_prev, n_cur)
                       if shadow[i]["txn"] is not None}
        # ...but only when the walk starts from an available balance. A
        # bank that reports none leaves the forecast on the current
        # balance, which pending credits are not in: dropping the re-date
        # then hides the paycheck for the day instead of avoiding a double
        # count. The start account is the forecast's own pick.
        if pending_hit:
            from . import forecast
            if not forecast._checking_start(conn, cfg)[1]:
                pending_hit = set()
    for i in range(n_prev, len(occs)):
        occ = occs[i]
        if occ["floored"] or occ["txn"] is not None:
            continue
        due = occ["due"]
        if income and due == today and i in pending_hit:
            continue
        if i < n_cur and (not income or (
                due == today and occ["bill"]["anchor"] <= today)):
            # an unpaid occurrence due today or earlier lands tomorrow —
            # the walk starts tomorrow, so a date of today is no date at
            # all. Income takes this only for TODAY, and only while the
            # series itself still names today (or earlier) as its next
            # payday: a paycheck due today that has not posted yet is still
            # coming, and dropping it while today's bills stay reserved
            # makes the runway and next_paycheck short by a whole paycheck.
            # A series whose next payday has already moved past today has
            # had today's deposit recorded — today's occurrence is then only
            # a back-projection. Earlier missed paychecks stay dropped (see
            # the docstring).
            due = max(due, today + dt.timedelta(days=1))
        if today < due <= end:
            out.append({"due": due, "planned": occ["planned"],
                        "payee": occ["bill"]["payee"], "occ": occ})
    out.sort(key=lambda r: r["due"])
    if memo is not None:
        memo[key] = out
    return out


# ---- cash events: bonus + investment funding ---------------------------

# Words an institution's name shares with every other institution's: they say
# nothing about who sent a deposit, the bank's text seldom carries them, and
# requiring them would miss the institution's own transfers ('FIDELITY
# MONEYLINE' from 'Fidelity Investments').
_INSTITUTION_FILLER = {
    "bank", "banks", "brokerage", "capital", "company", "financial",
    "group", "investment", "investments", "invest", "management",
    "securities", "services", "trust", "wealth", "advisors", "advisor",
    "funds", "fund", "markets", "holdings", "national", "association",
}

# Connector and product words in the marketing form of a name ('Raymond
# James Client Access', 'Empower Retirement', a payroll bill's 'Payroll'):
# a bank descriptor never prints them, so they are dropped before the brand
# is read, even from the full phrase.
_BRAND_NOISE = {
    "the", "of", "and", "from", "by", "for", "co", "inc", "llc", "corp",
    "ltd", "plc", "na", "assoc", "associates", "retirement", "plan",
    "plans", "client", "clients", "access", "netbenefits", "edge",
    "investing", "online", "center", "portal", "participant", "workplace",
    "benefits", "account", "accounts", "ira", "roth", "payroll", "salary",
    "paycheck", "wages", "direct", "deposit", "dep", "ach", "transfer",
    "xfer",
}

# Words that are never a brand on their own: common given names that lead
# or make up brokerage names, and ordinary words any deposit may carry. A
# brand made only of these is matched on its whole phrase ('raymond james',
# 'morgan stanley'), never on one of its words — 'ZELLE FROM CHARLES
# WILSON' is not Schwab and 'JAMES AUTO REPAIR' is not Raymond James.
_NOT_A_BRAND_ALONE = {
    "charles", "edward", "james", "raymond", "morgan", "stanley", "john",
    "thomas", "william", "robert", "george", "franklin", "lincoln",
    "jones", "smith", "allen", "mason", "jackson", "nelson", "harris",
    "russell", "scott", "grant", "price", "rowe", "wells", "lynch",
    "trade", "first", "united", "american", "state", "street", "principal",
    "interactive", "brokers", "public", "global", "mutual", "life",
    "insurance", "northern", "southern", "eastern", "western", "central",
    "pacific", "atlantic", "union", "federal", "savings", "credit", "home",
    "prime", "select", "premier", "liberty", "freedom", "security",
    "standard", "guardian", "pioneer", "saint", "health", "system",
    "clinic", "labs", "city", "county", "york", "new",
}


def _brand_norm(text: str) -> str:
    """_text_norm, with an asterisk inside a word JOINED rather than split:
    'E*TRADE' and 'ETRADE' are one word on both sides of the match."""
    return _text_norm(re.sub(r"(?<=[a-z0-9])\*(?=[a-z0-9])", "",
                             (text or "").lower()))


def brand_phrases(name: str) -> set[str]:
    """The word-boundary-padded phrases that name `name`'s brand in a bank
    descriptor; a text names the brand when any one of them is in its
    _brand_norm form.

    The name is cut at a product suffix (' - NetBenefits', ' from Morgan
    Stanley') into segments, each read as a brand of its own. A segment
    contributes each distinctive word that may stand alone, and its whole
    phrase — with and without the generic institution words — so a brand
    made only of given names or ordinary words still matches when the bank
    prints it in full. A name with nothing distinctive left yields nothing:
    an alert that stays quiet beats one that fires on every Zelle."""
    out: set[str] = set()
    low = (name or "").lower()
    for seg in re.split(r"\s+-\s+|\s+from\s+|[|(),/]", low):
        words = [w for w in _brand_norm(seg).split()
                 if w not in _BRAND_NOISE]
        core = [w for w in words if w not in _INSTITUTION_FILLER]
        for w in core:
            if len(w) >= 3 and w not in _NOT_A_BRAND_ALONE \
                    and w not in STOP_TOKENS:
                out.add(f" {w} ")
        for phrase in (words, core):
            if len(phrase) >= 2:
                out.add(" " + " ".join(phrase) + " ")
        if "*" in seg:
            # the bank may print the asterisk as a space: 'E TRADE'
            spaced = [w for w in _text_norm(seg).split()
                      if w not in _BRAND_NOISE]
            if len(spaced) >= 2:
                out.add(" " + " ".join(spaced) + " ")
    return out


# Brokerage brands that a bank descriptor prints on their own ('SCHWAB
# BROKERAGE MONEYLINK' for Charles Schwab, 'ETRADE SECURITIES' for E*TRADE
# from Morgan Stanley). Each is a coined or unmistakable brand word — no
# given name, surname or ordinary word — so it may stand for the
# institution alone. It only ever applies to an institution whose own name
# carries the word.
_BRAND_ALIASES = {
    "schwab", "fidelity", "vanguard", "etrade", "empower", "wealthfront",
    "betterment", "robinhood", "merrill", "ameritrade", "webull", "tiaa",
    "firstrade", "tastytrade", "tastyworks", "wealthsimple", "nuveen",
    "blackrock", "invesco", "pimco",
}


def institution_matchers(name: str) -> set:
    """What a deposit's text must carry to be recognised as coming from the
    investment institution `name`. Each matcher is one of:

    * a frozenset of words: the institution's FULL normalised name — every
      word left once noise and generic institution words are dropped — all
      present as whole words, in any order ('Charles Schwab' needs both
      'charles' and 'schwab'; 'Franklin Templeton' both 'franklin' and 'templeton');
    * a padded phrase that must appear as written, for a name made only of
      given names and ordinary words ('raymond james', 'morgan stanley'):
      such a name read in any order is a person ('JAMES RAYMOND');
    * a padded brand alias from _BRAND_ALIASES, when the institution's name
      carries one ('schwab' alone for Charles Schwab).

    There is no single-word fallback. A multi-word brand whose words are
    not in the alias table — 'Franklin Templeton', 'Northwestern Mutual', 'M1
    Finance' — is matched only by its full phrase, so a deposit naming one
    of its words ('TEMPLETON HARDWARE', 'MUTUAL AID SOCIETY', 'FINANCE
    CHARGE') is never read as money pulled from investments. A real
    transfer the bank prints with fewer words is missed instead: the alert
    staying quiet is the cheaper mistake."""
    out: set = set()
    low = (name or "").lower()
    for seg in re.split(r"\s+-\s+|\s+from\s+|[|(),/]", low):
        words = [w for w in _brand_norm(seg).split()
                 if w not in _BRAND_NOISE]
        core = [w for w in words if w not in _INSTITUTION_FILLER]
        distinctive = [w for w in core
                       if len(w) >= 3 and w not in _NOT_A_BRAND_ALONE
                       and w not in STOP_TOKENS]
        if distinctive:
            out.add(frozenset(core))
        else:
            for phrase in (words, core):
                if len(phrase) >= 2:
                    out.add(" " + " ".join(phrase) + " ")
        for w in core:
            if w in _BRAND_ALIASES:
                out.add(f" {w} ")
        if "*" in seg:
            # the bank may print the asterisk as a space: 'E TRADE'
            spaced = [w for w in _text_norm(seg).split()
                      if w not in _BRAND_NOISE]
            if len(spaced) >= 2:
                out.add(" " + " ".join(spaced) + " ")
    return out


def _matches(matchers, text: str) -> bool:
    """Whether `text` carries any matcher of institution_matchers (padded
    phrases, as brand_phrases returns, are matched the same way)."""
    norm = _brand_norm(text)
    have = None
    for m in matchers:
        if isinstance(m, frozenset):
            if have is None:
                have = set(norm.split())
            if m <= have:
                return True
        elif m in norm:
            return True
    return False


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
    # the employer is recognised by its brand, the same way an institution
    # is: a payroll bill named for an employer that leads with a given name
    # must not read a large Zelle from someone of that name as a bonus
    pay_brand = brand_phrases(pay["payee"] or "") if pay else set()

    # Each investment institution is recognised by its full name or a
    # curated brand alias (institution_matchers), as whole words of the
    # inflow's text — never by one ordinary word or surname of it: 'Franklin
    # Templeton' must not claim a Zelle from someone named Templeton.
    inst_matchers: set = set()
    for r in conn.execute(
            """SELECT DISTINCT i.institution_name
               FROM accounts a JOIN items i ON i.id = a.item_id
               WHERE a.type = 'investment'""").fetchall():
        inst_matchers |= institution_matchers(r["institution_name"] or "")

    def _names(phrases: set[str], text: str) -> bool:
        return _matches(phrases, text)

    def _hit(text: str) -> bool:
        return _matches(inst_matchers, text)

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
        elif _names(pay_brand, name) and r["inflow"] > paycheck * 1.75:
            bonus.append((day, r["inflow"]))

    ytd = sum(r["inflow"] for r in inflows(month_start.replace(month=1, day=1))
              if _hit((r["name"] or "").lower()) and r["inflow"] < LARGE)
    return {"bonus": bonus, "funding_mtd": funding_mtd, "large_mtd": large_mtd,
            "funding_mtd_total": sum(a for _, a in funding_mtd),
            "funding_ytd_total": ytd}


def recurring_load(conn, today: dt.date) -> dict:
    """What the recurring schedule costs and brings in, per month, plus the
    week ahead — the Bills page's headline.

    Deliberately the SAME evened_monthly_load the month plan uses
    (`avg_bills` above and in month_status), not a fresh sum of cadences.
    A page that says "$3,000/mo bills" beside a Budget page saying something
    else for the identical schedule makes both untrustworthy. Two
    methodologies behind one number always cost more than they save: a tile
    that sums the raw ledger beside a figure that excludes transfers reads an
    owner draw as a loss.
    """
    cfg = load_config(conn)
    caps, disabled = cfg.get("occurrence_caps"), cfg.get("disabled_bills")
    # the month plan's bill set as well as its method: a hand-tracked
    # card-payment bill is left out while live card debt exists, because
    # the card machinery already pays that debt (drop_card_pay_bills) —
    # counting it here would put it in the headline, the week ahead and
    # every month snapshot twice over
    bills = drop_card_pay_bills(
        conn, _recurring_bills(conn, caps=caps, disabled=disabled), cfg=cfg)
    income = _recurring_bills(conn, caps=caps, disabled=disabled, income=True)
    envelopes = _envelope_bills(conn, disabled=disabled)

    bills_mo = (evened_monthly_load(bills, today)
                + sum(e["monthly"] for e in envelopes))
    income_mo = evened_monthly_load(income, today)
    # The week ahead is what is still OWED in it, by the rule every cash
    # surface reads (upcoming_bill_occurrences): an occurrence already paid
    # — rent autopaid on the 28th for the 1st — is out of checking and not
    # due at all, and an unpaid one whose day has passed is owed now, so it
    # counts (re-dated to tomorrow, as the forecast books it). Summing the
    # raw schedule instead puts paid rent in "due this week" while the
    # runway beside it reserves nothing. Envelopes have no due dates and
    # contribute nothing here.
    week = upcoming_bill_occurrences(conn, cfg, today,
                                     today + dt.timedelta(days=7))
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
    end, with every day elapsed (expected = the budget's paced share,
    the whole budget unless the ledger began inside the month). Live,
    the day in progress has not elapsed yet (see `frac` below).

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
    # The month is paced by pace_window (specs/short-ledger-pacing.md):
    # expected-to-date is the budget over the FINISHED days since the
    # later of the 1st and the ledger's first day — today is not finished.
    # The today-ledger hands out today's allowance from midnight
    # (remaining ÷ days left INCLUDING today); counting today as spent
    # here would make an on-pace household with nothing bought yet this
    # morning read UNDER by exactly one day's budget, and the email's
    # verdict and its left-to-spend tile would contradict each other. The
    # days before the ledger are no data, not days the household spent
    # nothing on: the month may spend the budget's share from the pace
    # start to month end (`share`), live and closed alike, so a closed
    # first month judges what its last live day judged.
    pace = pace_window(conn, today, excluded=excluded_account_ids(cfg),
                       closed=closed)
    share = pace["share"]
    frac = pace["elapsed_days"] / days_in_month
    # the plan surplus accrues on the calendar: the money is the month's
    # whether or not the ledger saw its first days
    cal_frac = pace["calendar_days"] / days_in_month

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
    # LAST month's occurrences join the candidates first, as decoys — the
    # same rule upcoming_bill_occurrences applies. A bill due the 30th and
    # posted three days late on the 3rd belongs to LAST month's occurrence;
    # without the decoy it is too far from this month's own occurrence to
    # match anything and lands in the verdict as a variable purchase. Cash
    # basis: a decoy paid INSIDE this month is this month's fixed spend; an
    # unpaid decoy (or one settled last month) is dropped right after
    # matching — it is last month's problem, never this month's expectation
    # or "overdue".
    py, pm = ((today.year, today.month - 1) if today.month > 1
              else (today.year - 1, 12))
    occs = month_occurrences(bills, py, pm)
    n_prev = len(occs)
    occs += month_occurrences(bills, today.year, today.month)
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
    # A companion is spending in the month it is DATED, like every other
    # row. An occurrence whose main charge settled before this month began
    # is skipped below (and the decoys among them dropped right here), but
    # a fee that rode with it can post on the 1st or 2nd: matching already
    # took that row out of the variable side, so unless it is counted here
    # it is counted in no month at all.
    settled_fees = sum(
        c["amount"] for o in occs
        if o["txn"] is not None and as_date(o["txn"]["date"]) < month_start
        for c in o.get("companions") or []
        if as_date(c["date"]) >= month_start)
    occs = ([o for o in occs[:n_prev] if o["txn"] is not None
             and as_date(o["txn"]["date"]) >= month_start]
            + occs[n_prev:n_this]
            + [o for o in occs[n_this:] if o["txn"] is not None])
    mark_withdrawn(conn, occs, today,
                   since=month_start - dt.timedelta(days=PREPAY_MATCH_DAYS),
                   cfg=cfg, memo=memo)
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
    # recognized at payment, like the charge it rode with
    fixed_actual += settled_fees
    fixed_expected_td += settled_fees
    # THIS month's own schedule — every occurrence due inside the month that
    # is an obligation (paid, or unpaid and unfloored), wherever its cash
    # moved. The dynamic variable scale reads this, never cash-basis
    # fixed_month: an early autopay of next month's bill (or a late payment
    # of last month's) moves cash between months, and letting it move the
    # scale would shrink this month's food/other budgets — flipping a
    # household UNDER to OVER with no change in what it spent — while the
    # month that actually owes the bill gets nothing back.
    scheduled_month = 0.0
    drifts: dict[str, float] = {}
    overdue_unpaid = []
    # bills pinned to the Today page/email get a per-bill month summary —
    # the aggregate `fixed` bucket can't say whether THIS bill is paid
    pinned: dict[str, dict] = {}

    def _pin(bill):
        return pinned.setdefault(bill["payee"], {
            "payee": bill["payee"], "planned": 0.0, "paid": 0.0,
            "paid_count": 0, "occ_count": 0, "next_due": None,
            "overdue": False, "withdrawn_on": None})

    for occ in occs:
        if ((occ["due"].year, occ["due"].month) == (today.year, today.month)
                and (occ["txn"] is not None or not occ["floored"])):
            scheduled_month += occ["planned"]
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
            # …but the month's CASH is only the rows dated in it: a fee
            # posted on the 30th before a charge on the 1st was last
            # month's spending (that month saw it as a stray purchase), and
            # counting it here as well would count it twice. The drift and
            # the pinned card still read the whole payment — the bill's
            # price did not change because a fee landed a day early.
            fixed_actual += occ["txn"]["amount"] + sum(
                c.get("amount", 0.0) for c in occ.get("companions") or []
                if as_date(c["date"]) >= month_start)
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
                    # the card speaks for its earliest unpaid occurrence
                    p["withdrawn_on"] = (occ["withdrawn_on"].isoformat()
                                         if occ["withdrawn_on"]
                                         and occ["due"] <= today else None)
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
        scheduled_month += st["bill"]["monthly"]

    buckets = {"food": [], "other": []}
    for r in variable_rows:
        buckets["food" if r["is_food"] else "other"].append(r)
    for st in env_states:
        for r in st["overflow_rows"]:
            buckets["food" if r["is_food"] else "other"].append(r)

    def total(rs):
        return sum(r["amount"] for r in rs)

    # Expected-to-date covers the elapsed days (`frac`) plus whatever of
    # TODAY's allowance has been used so far. The actual side holds every
    # row through today, so without the second term a household that
    # spends exactly the allowance the today-ledger hands out (remaining at
    # midnight ÷ days left, today included — todayview.allowances) adds a
    # full day to actual and nothing to expected, and reads OVER BUDGET
    # while its tile says $0 left today. Only the part of today's spend up
    # to the allowance counts: spending past it is over, spending under it
    # is not yet "ahead" (the day is not over), a refund today never earns
    # extra room, and a morning with nothing bought sits exactly where
    # yesterday ended. A closed month has no day in progress.
    # The allowance is the tile's own: the paced share left at midnight
    # over the days left, today included.
    days_left_live = max(1, pace["days_left"])

    def spent_on_today(rs):
        return sum(r["amount"] for r in rs if as_date(r["date"]) == today)

    def to_date(month_budget, actual, rs):
        base = month_budget * frac
        if closed:
            return base
        t = spent_on_today(rs)
        allowance = (max(0.0, month_budget * share - (actual - t))
                     / days_left_live)
        return base + max(0.0, min(t, allowance))

    # Dynamic variable budget: the month's variable budget is what income
    # leaves after this month's fixed-bill SCHEDULE (scheduled_month — not
    # the cash-basis fixed_month above), split in the configured ratio. SHRINK-ONLY — heavy-bill months compress budgets so the plan
    # breaks even; light months stay capped (surplus = savings).
    food_budget, other_budget = cfg["food_monthly"], cfg["other_monthly"]
    income = active_monthly_income(cfg)
    scale = None
    if cfg.get("dynamic_variable_budget", True) and income:
        available = income - scheduled_month
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
        "food": {"actual": total(buckets["food"]),
                 "expected": to_date(food_budget, total(buckets["food"]),
                                     buckets["food"]),
                 "month_budget": food_budget, "rows": buckets["food"]},
        "other": {"actual": total(buckets["other"]),
                  "expected": to_date(other_budget, total(buckets["other"]),
                                      buckets["other"]),
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
        v["children"] = []
        for c in kids:
            c_exp = to_date(c["monthly"], c["actual"], c["rows"])
            v["children"].append(
                {"name": c["name"], "actual": c["actual"],
                 "month_budget": c["monthly"], "expected": c_exp,
                 "variance": c["actual"] - c_exp, "rows": c["rows"]})
        # display decomposition: parent actual = raw − carved-out dollars;
        # the parent's own budget stays the user-set number, its REMAINING
        # budget (after the children's shares) is shown alongside (spec §1)
        rem_budget = max(0.0, v["month_budget"]
                         - sum(c["monthly"] for c in kids))
        v["display_actual"] = v["actual"] - carved[pname]
        v["remaining_budget"] = rem_budget
        kid_ids = {id(r) for c in kids for r in c["rows"]}
        v["display_expected"] = to_date(
            rem_budget, v["display_actual"],
            [r for r in v["rows"] if id(r) not in kid_ids])

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
    # The same holds for a ledger only days old: both are pace_window's
    # `settled`.
    if verdict == "UNDER BUDGET" and not pace["settled"]:
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
                daily_actual = v["actual"] / pace["seen_days"]
                daily_budget = v["month_budget"] / days_in_month
                by_merchant: dict[str, list] = {}
                billish: dict[str, float] = {}
                for r in v["rows"]:
                    if r["category"] == "envelope overflow":
                        billish[r["payee"]] = billish.get(r["payee"], 0.0) + r["amount"]
                        continue
                    txt = f"{r['payee'] or ''} {r['name'] or ''}".lower()
                    toks = _tokens(txt)
                    # a receipt's line is never a bill charge (store_part)
                    hit = None if store_part(r) else next(
                        (bl for bl in bills
                         if _bill_matches(bl, toks, txt, row_merchant_id(r))
                         and amount_fits(_bands_of(bl), r["amount"],
                                         r.get("date"))), None)
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
    # today and every day after it — the tiles' own count (a closed
    # month reads as if from `today`, as it always has)
    days_left_m = (days_in_month - today.day + 1 if closed
                   else max(1, pace["days_left"]))

    def _spenders(rows_, k=3):
        by_m: dict[str, list] = {}
        tail: dict[str, float] = {}
        for r in rows_:
            if r["category"] == "envelope overflow":
                tail[r["payee"]] = tail.get(r["payee"], 0.0) + r["amount"]
                continue
            txt = f"{r['payee'] or ''} {r['name'] or ''}".lower()
            toks = _tokens(txt)
            hit = None if store_part(r) else next(
                (bl for bl in bills
                 if _bill_matches(bl, toks, txt, row_merchant_id(r))
                 and amount_fits(_bands_of(bl), r["amount"],
                                 r.get("date"))), None)
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
        # what is left is left of the paced share, the tiles' own figure
        spendable = month_budget * share
        left = spendable - spent
        # the tiles' own rate and day count (daily_rate), so "$X/day keeps
        # it green" and the recovery cap are the number the tile shows
        dr = daily_rate(
            spendable, spent,
            0.0 if closed else spent_on_today(rows_),
            days_left_m)
        rate = dr["rate"]
        top = _spenders(rows_)
        daily_actual = spent / pace["seen_days"]
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
                    f"${spendable:,.0f} left"
                    + (f" (${max(0.0, rate):,.0f}/day)" if left > 0 else "")
                    + (f". Biggest: {_top_text(top[:2])}." if top else "."))
        else:
            tone = "on"
            text = (f"is on pace — ${max(0.0, left):,.0f} left, "
                    f"${max(0.0, rate):,.0f}/day keeps it green"
                    + (f". Biggest: {_top_text(top[:2])}." if top else "."))
        return {"name": name, "tone": tone, "text": text,
                "spent": round(spent, 2), "budget": round(month_budget, 2),
                "spendable": round(spendable, 2),
                # floored to the cent: the recovery cap floors this again
                # and must never round up past what the rate allows
                "rate": math.floor(rate * 100 + 1e-6) / 100,
                "rate_days": dr["rate_days"],
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
        # the days the caps cover: each bucket's rate spreads over its own
        # rate_days (the days after today once today has spend); the
        # shorter count never promises a day the rate was not spread over
        rec_days = days_left_m
        for e in why_entries:
            if e["budget"] <= 0 and e["spent"] <= 0:
                continue
            rec_days = min(rec_days, e["rate_days"])
            # floored: rounding up would break the sentence's promise
            cap = int(e["rate"] + 1e-9)
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
            recovery = (f"To get back on track over the last {rec_days} "
                        f"day{'s' if rec_days != 1 else ''}: "
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
    avg_bills = (evened_monthly_load(bills, today)
                 + sum(e["monthly"] for e in envelopes))
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
    # A goal on a business's account is the business's commitment, paid
    # from its own checking, so it stays out of every household figure
    # here (savings.household_goals).
    from . import savings as _savings
    goals_plan = _savings.monthly_plan_total(cfg, mode="monthly",
                                             conn=conn)
    plan_surplus = (income - avg_bills - cfg["food_monthly"]
                    - cfg["other_monthly"] - goals_plan
                    if income else None)
    # Sweep availability, month to date: the plan surplus accrued to
    # today (the surplus is a monthly figure, so it becomes real a day at
    # a time) plus however far variable spending sits under its own pace
    # — overspending eats the surplus, underspending adds to it. Floored
    # at zero: a month that didn't work out has nothing to sweep. This is
    # the one number every "$Y available so far this month" surface reads.
    sweep_plan = _savings.monthly_plan_total(cfg, mode="sweep", conn=conn)
    sweep_available = None
    if plan_surplus is not None and sweep_plan > 0.005:
        sweep_available = round(max(0.0, plan_surplus * cal_frac - variance), 2)
    # posted evidence for the sweep line and the satisfied-month check —
    # the same matched-transfer evidence goal progress runs on
    sweep_posted = 0.0
    if sweep_plan > 0.005:
        for g in _savings.household_goals(conn, cfg):
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
        "variable_scale": scale, "scheduled_month": scheduled_month,
        "irregular_bills": irregular,
        "events": cash_events(conn, month_start, today + dt.timedelta(days=1)),
        "reasons": reasons, "why": why, "buckets": b, "drifts": drifts,
        # the fourth slot is the date the bank feed withdrew the charge
        # (None when it never posted); readers of the first three are
        # unaffected
        "overdue_unpaid": [(o["bill"]["payee"], o["planned"], o["due"].isoformat(),
                            o["withdrawn_on"].isoformat()
                            if o.get("withdrawn_on") else None)
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
        # the verdict's pace (pace_window), for every other surface that
        # paces the month: the whole window, and its figures by name — the
        # finished days expected-to-date counts, the finished days of
        # ledger, whether the read is settled enough to call the household
        # under, and the day the pace runs from
        "pace": pace,
        "pace_days": pace["elapsed_days"],
        "ledger_days": pace["ledger_days"],
        "pace_settled": pace["settled"],
        "pace_start": pace["pace_start"],
        # the month's spendable is the budget's share from the pace start
        # to month end: the hero's "$X of $Y left" reads these, and the
        # tiles hand out the same remainder a day at a time, so the hero
        # equals the tiles times the days left
        "month_share": share,
        "variable_budget_paced": (b["food"]["month_budget"]
                                  + b["other"]["month_budget"]) * share,
        "month_remaining_paced": ((b["food"]["month_budget"]
                                   + b["other"]["month_budget"]) * share
                                  - variable_actual),
        # the part of variable_actual dated today: a pace taken over the
        # ELAPSED days (the forecast's) must leave it out, or it divides
        # day-many days of spend by day-minus-one
        "variable_today": (0.0 if closed else
                           spent_on_today(b["food"]["rows"])
                           + spent_on_today(b["other"]["rows"])),
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


def paced_daily(st: dict) -> float | None:
    """Variable spend per day at the verdict's pace (pace_window): spend
    through yesterday over the finished days month_status counted. Today
    has not finished, so its spending stays out of the numerator as it
    stays out of the days. The days run from the later of the 1st and the
    ledger's first day — so a household that started mid-month projects
    and forecasts at the rate the verdict judges it by. None when no day
    has finished yet (the plan is the only rate there is); callers use the
    plan too while the window is not `settled`."""
    days = st["pace_days"]
    if days < 1:
        return None
    return (st["variable_actual"] - (st.get("variable_today") or 0.0)) / days


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

    `bucket` is 'food', 'other', 'fixed' (the Bills row: the month's bill
    payments and envelope swipes, the rows the fixed bucket owns), or a
    custom bucket's configured name. Returns {"bucket", "label", "amount",
    "txn_ids"} — or None when the name is not a bucket of this month's
    plan, which is the caller's 400.

    A parent tile with carve-outs shows its DISPLAY number (its own rows
    minus the children's), because the children have tiles of their own;
    the row set matches, so the listing and the number stay the same
    money. Envelope overflow contributes the purchase it was carved from:
    the slice has no row of its own, and the ledger lists whole
    transactions — so a split purchase's full amount appears there while
    the tile counts only the part beyond the envelope.
    """
    b = status["buckets"]
    if bucket == "fixed":
        rows, amount, label = b["fixed"]["rows"], b["fixed"]["actual"], "Bills"
    elif bucket in ("food", "other"):
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


def budgets_set(conn) -> bool:
    """Has the household a budget of its own? Read from the stored
    settings row, not the loaded document: under a month of history the
    loader fills in a provisional seed that is never saved, and a seed is
    the app's guess, not a plan the household made. The Today page, the
    app and the daily email all gate their verdict on this one answer."""
    row = conn.execute(
        "SELECT config->'food_monthly' AS f, config->'other_monthly' AS o "
        "FROM tenant_settings").fetchone()
    if not row:
        return False

    def amount(v) -> float:
        try:
            return float(v or 0)
        except (TypeError, ValueError):
            return 0.0
    return bool(amount(row["f"]) or amount(row["o"]))
