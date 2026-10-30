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

# Which credit cards count against personal headroom, as one predicate over
# `accounts a` taking the excluded-account list as its single parameter.
# The card_debt sum, the payoff events and the Today page's runway tile all
# splice THIS fragment: the tile and the cash chart render side by side, so
# a card one of them stops counting has to vanish from the other in the
# same breath, or the page argues with itself about the same household.
#   - the Accounts page's "excl" toggle, dropped exactly as every report
#     drops those accounts' rows (reporting.excluded_accounts_sql)
#   - business cards, out of PERSONAL runway unless entities are combined
#   - linked shadow sources, so a card fed by two connections schedules one
#     payment for its one real balance, not one per account row
CARD_SCOPE_SQL = """
      AND NOT (a.id = ANY(%s))
      AND (NULLIF((SELECT current_setting('app.combine_entities', true)),'')='true'
           OR a.entity_id IS NULL)
      AND (NULLIF((SELECT current_setting('app.shadow_ids', true)), '') IS NULL
           OR NOT (a.id = ANY(string_to_array(
                   (SELECT current_setting('app.shadow_ids', true)), ','))))
"""


def _checking_balance(conn, cfg: dict) -> float | None:
    return _checking_start(conn, cfg)[0]


def _checking_start(conn, cfg: dict) -> tuple[float | None, bool, bool]:
    """The runway's starting checking balance, its available/current
    flag and its unset flag — checking_pick without the account id."""
    return checking_pick(conn, cfg)[1:]


def checking_pick(conn, cfg: dict) -> tuple[str | None, float | None, bool, bool]:
    """The household's primary checking account as every surface honours
    it — the id the runway starts from, then the runway's starting
    checking balance; whether the bank reported
    it as an AVAILABLE balance (True) or the walk fell back to the current
    balance (False) — a pending deposit is inside the first and not the
    second, which the payday re-date has to know; and whether the balance
    is UNSET (True: a checking row exists but nobody has given it a
    balance yet).

    None when the household has no checking account the runway may start
    from (a card-only or savings-only household); 0.0 with unset=True when
    checking accounts exist but none has a balance yet (a files-only
    import). That is an unknown, not a $0 balance: read as $0, every card
    balance and bill becomes a "short today" alarm about an account that
    does not exist. The surfaces read `unset` from the row itself rather
    than guessing from what other accounts the household has: an imported
    checking with no balance beside a linked savings account (or beside a
    linked checking the household excluded) is just as unknown.

    Per-tenant primary checking: explicit config wins, else the
    largest-balance depository checking account. An account the household
    excluded on the Accounts page is not its checking account — pinned or
    biggest — for the same reason its cards are out of the runway: the
    runway would then start from money every other figure on the page
    ignores, and an old high-balance account kept linked would hide the
    household actually running out of cash."""
    acct_id = cfg.get("checking_account_id")
    # a pinned account that has since become a linked group's shadow (its
    # source went stale or errored and the healthy twin took over) would
    # feed a frozen balance into every runway number — honour the live
    # failover the rest of the money math already follows; an excluded one
    # is money every other figure ignores. pinned_account_live drops both.
    # the one rule for a pin the forecast honours (pin_is_live): the
    # checklist, the wizard and the auto-pin read the same answer
    if acct_id and not pinned_account_live(conn, cfg, acct_id):
        acct_id = None
    if acct_id:
        # a pinned account that was since assigned to a business entity
        # is business money — the personal runway must not start from it
        # unless the household combines entities (the auto-pick below
        # applies the same rule)
        # …and a pin on an account whose bank was since disconnected (a
        # relink whose adoption did not land, a reaped Item) is a frozen
        # balance: the auto-pick below takes over until it is re-pinned
        row = conn.execute(
            """SELECT COALESCE(balance_available, balance_current) AS bal,
                      balance_available IS NOT NULL AS has_avail
               FROM accounts WHERE id = %s""", (acct_id,)).fetchone()
        if row and row["bal"] is not None:
            return acct_id, row["bal"], bool(row["has_avail"]), False
    # Only a row that carries a balance may win the pick: every file
    # import (Mint, QIF, OFX, a competitor export) creates a checking row
    # nobody has set a balance on, and sorting it as $0 would let it beat a
    # real overdrawn account and silence every cash alarm.
    # the same eligibility the wizard's pick and the auto-pin use (no
    # excluded, business, shadow, hidden or disconnected-bank account)
    eligible = eligible_checking_ids(conn, cfg)
    rows = conn.execute(
        """SELECT id, COALESCE(balance_available, balance_current) AS bal,
                  balance_available IS NOT NULL AS has_avail
           FROM accounts WHERE id = ANY(%s)
           ORDER BY COALESCE(balance_available, balance_current) IS NULL,
                    COALESCE(balance_current, 0) DESC LIMIT 1""",
        (eligible,)).fetchall() if eligible else []
    if not rows:
        return None, None, False, False
    row = rows[0]
    if row["bal"] is not None:
        return row["id"], row["bal"], bool(row["has_avail"]), False
    # checking accounts exist but none carries a balance yet: that is a
    # balance the household has not set, not a missing account — $0 here
    # lets the surfaces ask for today's balance instead of for an account
    return row["id"], 0.0, False, True


def eligible_checking_ids(conn, cfg: dict) -> list[str]:
    """The accounts the household could anchor its cash forecast on: a
    depository checking account that is not excluded, not a business
    entity's (unless entities are combined) and not a linked group's
    shadow. The one rule: _checking_start's auto-pick picks among these."""
    from . import links
    excluded = set(budget.excluded_account_ids(cfg))
    shadows = set(links.shadow_ids(conn))
    # the combined-entities choice read from the settings document, so a
    # worker's or an import's connection answers the same as a request's
    combined = bool(cfg.get("combine_entities"))
    rows = conn.execute(
        """SELECT a.id FROM accounts a
           LEFT JOIN items i ON i.id = a.item_id
           WHERE a.type = 'depository' AND a.subtype = 'checking'
             AND a.user_removed_at IS NULL
             -- a disconnected or restored-shell BANK's accounts are history,
             -- not a place the forecast can anchor; a manual or file-import
             -- account carries the same status after a restore and is live
             AND NOT (i.aggregator IN ('plaid','mx','simplefin','simplefin-org')
                      AND COALESCE(i.status, '') IN ('archived', 'restored'))
             AND (%s OR a.entity_id IS NULL)
           ORDER BY COALESCE(a.balance_current, 0) DESC, a.id""",
        (combined,)).fetchall()
    return [r["id"] for r in rows
            if r["id"] not in excluded and r["id"] not in shadows]


def pinned_account_live(conn, cfg: dict, acct_id: str) -> bool:
    """Does the forecast honour a pin on `acct_id`? The pin may sit on any
    account type (the Accounts editor offers the switch on every row), so
    the rule is not "eligible checking" but: the row is there and not
    hidden, its bank is not a disconnected or restored shell, and it is
    personal money unless the household combines entities. A pin that
    fails here is kept in the settings but anchors nothing: the checklist
    reads it as unset and the auto-pin may replace it.

    An account the household excluded from its budget, or one that is a
    linked group's shadow (its healthy twin carries the balance), anchors
    nothing either — checking_pick drops both — so the checklist and the
    Accounts page must not call it the primary checking."""
    from . import links
    if (acct_id in set(budget.excluded_account_ids(cfg))
            or acct_id in set(links.shadow_ids(conn))):
        return False
    row = conn.execute(
        """SELECT 1 FROM accounts a LEFT JOIN items i ON i.id = a.item_id
           WHERE a.id = %s AND a.user_removed_at IS NULL
             AND NOT (i.aggregator IN ('plaid','mx','simplefin','simplefin-org')
                      AND COALESCE(i.status, '') IN ('archived', 'restored'))
             AND (%s OR a.entity_id IS NULL)""",
        (acct_id, bool(cfg.get("combine_entities")))).fetchone()
    return row is not None


def _pin_facts(conn, cfg: dict, acct_id: str) -> dict | None:
    """What decides whether a pin on `acct_id` anchors anything: None when
    no row carries the id, else its standing on each rule."""
    from . import links
    row = conn.execute(
        """SELECT a.user_removed_at IS NOT NULL AS hidden, a.entity_id,
                  COALESCE(a.balance_available, a.balance_current) IS NULL
                    AS no_balance,
                  (i.aggregator IN ('plaid','mx','simplefin','simplefin-org')
                   AND COALESCE(i.status, '') IN ('archived', 'restored'))
                    AS bank_gone
           FROM accounts a LEFT JOIN items i ON i.id = a.item_id
           WHERE a.id = %s""", (acct_id,)).fetchone()
    if row is None:
        return None
    return {"hidden": bool(row["hidden"]),
            "business": bool(row["entity_id"])
                        and not cfg.get("combine_entities"),
            "bank_gone": bool(row["bank_gone"]),
            "no_balance": bool(row["no_balance"]),
            "excluded": acct_id in set(budget.excluded_account_ids(cfg)),
            "shadow": acct_id in set(links.shadow_ids(conn))}


def pin_refusal(conn, cfg: dict, acct_id: str) -> str | None:
    """Why the household may not pin `acct_id` as its primary checking —
    the sentence every pin door answers with — or None when it may. The
    same rules pinned_account_live applies, so a pin that is saved is one
    the forecast honours (a balance aside: a row nobody has set one on is
    pinnable, and its balance is the next thing asked)."""
    f = _pin_facts(conn, cfg, acct_id)
    if f is None:
        return "account not found"
    if f["bank_gone"]:
        return ("that account's bank is disconnected — reconnect it, or "
                "pick a live checking account")
    if f["business"]:
        return ("a business account cannot be the household's primary "
                "checking — move it back to personal first, or turn on "
                "the combined view")
    if f["excluded"]:
        return ("an account excluded from the budget cannot be the "
                "household's primary checking — include it first")
    if f["hidden"] or f["shadow"]:
        return ("the cash forecast skips this account (it is hidden, or "
                "the backup source of a linked pair) — pin the account it "
                "is linked to, or show it again first")
    return None


def pin_unused_reason(conn, cfg: dict, anchor: str | None) -> str | None:
    """Why the saved pin is not the account the forecast starts from
    (`anchor`, checking_pick's id), in the words the Accounts page shows
    beside it; None when there is no pin or it is the anchor. Read from
    the rules checking_pick applies, so the page never guesses."""
    pin = cfg.get("checking_account_id")
    if not pin or pin == anchor:
        return None
    f = _pin_facts(conn, cfg, pin)
    if f is None:
        return "account removed"
    for key, words in (("excluded", "excluded"), ("hidden", "hidden"),
                       ("shadow", "backup source"),
                       ("bank_gone", "bank disconnected"),
                       ("business", "business account"),
                       ("no_balance", "no balance set")):
        if f[key]:
            return words
    return "not a live account"


def pin_is_live(conn, cfg: dict) -> bool:
    """Is the household's pinned primary checking one the forecast honours
    (pinned_account_live)? The one answer the checklist, the wizard and
    the auto-pin read."""
    pin = cfg.get("checking_account_id")
    return bool(pin) and pinned_account_live(conn, cfg, pin)


def pickable_checking_ids(conn, cfg: dict) -> list[str]:
    """The accounts the household is ASKED to choose between: the eligible
    checking accounts that carry a balance, largest first. A closed
    account kept for its history (a Mint or bank-export import, a manual
    row nobody set a balance on) is eligible — the forecast may still fall
    through to it — but it is no answer to "which account does the cash
    forecast anchor on?", so it is not offered next to a live one. When
    no eligible account carries a balance yet (a files-only household's
    first import) they are all offered, so one is still pinned by itself
    and two are still a question."""
    ids = eligible_checking_ids(conn, cfg)
    if len(ids) < 2:
        return ids
    rows = conn.execute(
        """SELECT id FROM accounts WHERE id = ANY(%s)
             AND COALESCE(balance_available, balance_current) IS NOT NULL""",
        (ids,)).fetchall()
    with_balance = {r["id"] for r in rows}
    return [i for i in ids if i in with_balance] or ids


def pin_row_gone(conn, acct_id: str) -> bool:
    """Is the pinned account's row gone for good — deleted, or removed by
    the household? A row that still exists on an archived, restored or
    tokenless bank Item is NOT gone: a reconnect adopts it onto the new
    Item and carries the pin with it (sync/adopt), so the pin must wait
    there, dead but kept, rather than be moved to another bank."""
    row = conn.execute(
        "SELECT user_removed_at IS NOT NULL AS removed FROM accounts WHERE id = %s",
        (acct_id,)).fetchone()
    return row is None or bool(row["removed"])


def auto_pin_primary_checking(conn) -> str | None:
    """Pin the household's primary checking when the choice is obvious:
    no pin yet and exactly one pickable checking account. With two or
    more the household is asked (the wizard's connect step, the Today
    checklist); with none there is nothing to pin. A pin the forecast
    honours is never moved. A pin the forecast no longer honours is
    replaced by the account the forecast already falls through to (the
    largest balance) only when its row is gone for good — no row carries
    the id (a pin carried over from an export whose accounts were
    re-created, or left by a purge), or the row was removed by the
    household. A dead pin whose row still exists (its bank
    disconnected, reaped or restored as a shell) is kept — even when one
    other account is the only pickable one: the reconnect's adoption
    carries it to the new Item, and moving it meanwhile would lose the
    household's choice for good. Returns the id pinned, else None."""
    with budget.config_txn(conn) as cfg:
        pin = cfg.get("checking_account_id")
        if pin and pin_is_live(conn, cfg):
            return None
        # a dead pin whose row survives waits for the reconnect — however
        # many other accounts are pickable, one included
        if pin and not pin_row_gone(conn, pin):
            return None
        ids = pickable_checking_ids(conn, cfg)
        if not ids or (len(ids) > 1 and not pin):
            return None
        cfg["checking_account_id"] = ids[0]
        return ids[0]


def standing_surplus(conn, st: dict, today: dt.date, *,
                     memo: dict | None = None, cfg: dict | None = None,
                     bill_rows: list | None = None,
                     closed: bool = False) -> float | None:
    """The plan surplus of an ordinary month, from a month_status `st`.

    month_status's plan_surplus is built from that month's own income —
    what landed plus the checks still due — so a three-paycheck month, a
    tax refund or a check that has not arrived yet moves it. That is right
    for the month itself and wrong for the months after it. The standing
    figure swaps the month's income for what the household's income series
    bring in on average (budget.income_actual_monthly, the Bills page's
    headline), keeping the same bills, budgets and savings plan. When the
    month's income was the budgeted figure already (no income series), it
    is standing as it is. None when `st` has no plan surplus.

    A closed month is read under the schedule and config it was judged
    under (`bill_rows`, `cfg`: its snapshot), so archiving a series today
    cannot rewrite a past year's figure. A closed month with no frozen
    schedule has no series of its own to average, and today's series do
    not speak for it (a job that started later would count at its
    scheduled rate, one that ended would vanish); its standing income is
    instead what the ledger realised (_realised_income_average)."""
    ps = st.get("plan_surplus")
    if ps is None:
        return None
    if (st.get("income_received") is None
            and st.get("income_expected") is None):
        return ps
    if cfg is None:
        cfg = budget.load_config(conn, memo=memo)
    if closed and bill_rows is None:
        avg = _realised_income_average(conn, cfg, today, memo=memo)
        if avg is None:
            return ps
        return round(ps - float(st.get("income") or 0.0) + avg, 2)
    series = [b for b in budget._recurring_bills(
                  conn, caps=cfg.get("occurrence_caps"),
                  disabled=cfg.get("disabled_bills"), income=True, memo=memo,
                  rows=bill_rows)
              if (b["recurrence"] or {}).get("frequency")]
    avg = budget.income_actual_monthly(
        conn, today, series, memo=memo, bill_rows=bill_rows,
        excluded=budget.excluded_account_ids(cfg))["monthly"]
    if avg <= 0:
        return ps
    return round(ps - float(st.get("income") or 0.0) + avg, 2)


# A realised average needs a few whole months behind it before it says more
# than the month itself does — the same floor income_actual_monthly sets
# before a series' deposits outrank its schedule.
REALISED_INCOME_MIN_MONTHS = 3


def _realised_income_average(conn, cfg: dict, today: dt.date, *,
                             memo: dict | None = None) -> float | None:
    """The income a closed month's household realised per month: the
    average, over the twelve months through `today`'s month, of each
    whole month's income exactly as month_status read it for a month
    with no frozen schedule (budget._month_income, closed) — the deposits
    that landed, each check counted for the month it pays. Nothing is
    projected, so a series that started later adds no month it did not
    pay, and one that ended keeps the months it did.

    Months before the ledger's first whole month are no data, not months
    of no income, and are left out; so is a month the reader has nothing
    for. None when fewer than REALISED_INCOME_MIN_MONTHS whole months are
    left — the month's own surplus is then the best figure there is."""
    first = budget.ledger_start(conn, budget.excluded_account_ids(cfg))
    if first is None:
        return None
    if first.day > 1:
        first = (first.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    y, m = today.year, today.month
    got = []
    for _ in range(12):
        start = dt.date(y, m, 1)
        if start < first:
            break
        end = start.replace(day=calendar.monthrange(y, m)[1])
        _, received, _ = budget._month_income(conn, cfg, end, closed=True,
                                              memo=memo)
        if received is not None:
            got.append(received)
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    if len(got) < REALISED_INCOME_MIN_MONTHS:
        return None
    return sum(got) / len(got)


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


def _paycheck_amount(conn, bill: dict, today: dt.date,
                     cap: float | None = None, *,
                     claims: dict[str, list[dict]] | None = None) -> float:
    """Median of the last 3 real deposits for this income series (falls back
    to the configured amount). Wide 0.5–2× band.

    `bill` is the processed series record (budget._recurring_bills). Its
    deposits are the ones it claims among every income series
    (budget._claim_income_rows) — a sibling whose name shares a first word
    keeps its own checks, so a pension is never sized from a payroll that
    happens to fall inside its band. `claims` is that grouping when the
    caller already holds it.

    `cap` = the active income-scenario biweekly take-home: when the
    retirement-saving toggle flips, real deposits lag the new reality by 2-3
    paychecks, so the estimate is capped at min(median, scenario)."""
    row_amount = bill["amount"]
    if claims is None:
        claims = budget._claim_income_rows(
            bills._income_rows(conn, today),
            budget._income_claimants(conn, [bill]))
    events = bills._events(claims.get(bill["payee"], []))
    core = [e["amount"] for e in events
            if 0.5 * row_amount <= e["amount"] <= 2 * row_amount]
    est = statistics.median(core[-3:]) if len(core) >= 2 else row_amount
    return est if cap is None else min(est, cap)


def _series_cadence(rec: dict) -> str | None:
    """An income series' recurrence in the income scenario's cadence words
    (budget.CADENCE_MONTHLY's keys); None when it is none of them."""
    freq = str(rec.get("frequency") or "").upper()
    try:
        interval = max(1, int(rec.get("interval") or 1))
    except (TypeError, ValueError):
        interval = 1
    if freq == "WEEKLY":
        return {1: "weekly", 2: "biweekly"}.get(interval)
    if freq == "DAILY":
        return {7: "weekly", 14: "biweekly"}.get(interval)
    if freq == "MONTHLY" and interval == 1:
        return ("semimonthly" if len(rec.get("byMonthDay") or []) >= 2
                else "monthly")
    return None


def _group_by_payer(series: list[dict]) -> list[list[dict]]:
    """Income series grouped by who pays them. A payer is its merchant
    identity — series that share a resolved merchant id are one payer —
    and only a series with no merchant identity falls back to the payee's
    key word: "State Pension" and "State Example Co Payroll" share a first
    word and nothing else, and treating them as one payer would cap a pension
    at a payroll scenario's take-home."""
    groups: list[tuple[set, list[dict]]] = []
    by_key: dict[str, list[dict]] = {}
    for b in series:
        ids = set(b.get("ids") or ())
        if not ids:
            by_key.setdefault(b.get("key") or b["payee"], []).append(b)
            continue
        # series that share any merchant id join one payer, merging every
        # group the new series bridges
        merged_ids, merged = set(ids), [b]
        for g in groups:
            if g[0] & ids:
                merged_ids |= g[0]
                merged = g[1] + merged
        groups = [g for g in groups if not g[0] & ids]
        groups.append((merged_ids, merged))
    return [g[1] for g in groups] + list(by_key.values())


def _scenario_caps(income_bills: list[dict], cadence: str,
                   take_home: float) -> dict[str, float]:
    """Per-deposit caps, by payee, for the income series an income scenario
    describes. The scenario's take-home is ONE paycheck at its own cadence,
    so it caps the largest series paid at that cadence and nothing else —
    an empty answer when no series runs at it, because a take-home figure
    for one cadence says nothing about income paid at another.

    Semimonthly is the exception: detection rarely stores a 1st-and-15th
    payroll as one series reading semimonthly — it stores the two paydays
    as two MONTHLY series of the same payer, and a scenario that matched
    nothing there would silently stop reaching the forecast. So with no
    semimonthly series, the payer with two or more MONTHLY series (the
    most money among such payers) is the one described: its two largest series are each one of the two
    deposits (capped at the take-home), and when no payer has two, the
    largest lone monthly series is paid the whole month at once (capped at
    two take-homes)."""
    same = [b for b in income_bills
            if _series_cadence(b.get("recurrence") or {}) == cadence]
    if same:
        return {max(same, key=lambda b: b["amount"])["payee"]: take_home}
    if cadence != "semimonthly":
        return {}
    monthly = [b for b in income_bills
               if _series_cadence(b.get("recurrence") or {}) == "monthly"]
    if not monthly:
        return {}
    payers = _group_by_payer(monthly)
    # a payer with two monthly paydays is the pair the scenario names; a
    # lone monthly series (a pension) is chosen only when no pair exists
    group = max(payers,
                key=lambda g: (len(g) >= 2, sum(b["amount"] for b in g)))
    group = sorted(group, key=lambda b: b["amount"], reverse=True)[:2]
    per = (take_home if len(group) == 2
           else take_home * budget.CADENCE_MONTHLY["semimonthly"])
    return {b["payee"]: per for b in group}


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
    # no checking account: the walk still runs (the calendar and the cash
    # chart read its dated events and month-to-month deltas, which do not
    # depend on the start), but the result says so, and every surface that
    # would quote a balance or a shortfall from it stays quiet instead
    no_checking = checking_bal is None
    if no_checking:
        checking_bal = 0.0
    # the runway must drop an excluded account's balances and payoff events
    # exactly as every report drops its rows, or the cash chart's forecast
    # tail pays off a card the actuals beside it never counted
    excluded = budget.excluded_account_ids(cfg)
    card_debt = conn.execute(
        f"""SELECT COALESCE(SUM(GREATEST(a.balance_current, 0)), 0) AS d
           FROM accounts a WHERE a.type = 'credit' {CARD_SCOPE_SQL}
        """, (excluded,)).fetchone()["d"]
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
            f"""SELECT COALESCE(a.display_name, a.name) AS name,
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
                 {CARD_SCOPE_SQL}
            """, (excluded,)).fetchall():
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
    # The walk starts from household-only balances, so a goal on a
    # business's account (paid from the business's own checking) is not
    # walked out of it.
    first = today.replace(day=1)
    hh_goals = _savings.household_goals(conn, cfg)
    for g in hh_goals:
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
                # goal (no destination account — match words alone
                # cannot tell the savings leg from the checking leg) has
                # no ledger to check; the conservative read is still-owed.
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
    # lower configured paycheck applies immediately — but only for the ONE
    # series the scenario describes. Its take-home is a single paycheck at
    # its own cadence; capping every income series with it would walk a
    # $3,000 monthly pension in at a $2,000 biweekly paycheck and end the
    # runway short.
    _scen = budget.active_income_scenario(cfg)
    pay_cap = (_scen.get("take_home") if _scen else None) or None
    pay_caps = (_scenario_caps(
        budget._recurring_bills(conn, caps=cfg.get("occurrence_caps"),
                                disabled=cfg.get("disabled_bills"),
                                income=True, memo=memo),
        str(_scen.get("cadence") or "biweekly"), float(pay_cap))
        if pay_cap is not None else {})
    # Paychecks go through the same occurrence rule as bills, deposit side:
    # one that already posted is in the live starting balance, so walking
    # it in again on payday would count the same money twice for the days
    # between the deposit and the due date.
    amounts: dict[str, float] = {}
    paychecks = budget.upcoming_bill_occurrences(conn, cfg, today, end,
                                                 income=True, memo=memo)
    # every deposit to the one series it belongs to, once for the walk
    claims = (budget._claim_income_rows(
        bills._income_rows(conn, today),
        budget._income_claimants(conn, [p["occ"]["bill"] for p in paychecks]))
        if paychecks else {})
    for p in paychecks:
        payee = p["payee"]
        if payee not in amounts:
            amounts[payee] = _paycheck_amount(
                conn, p["occ"]["bill"], today, cap=pay_caps.get(payee),
                claims=claims)
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
    _sweep_goals = [g for g in hh_goals
                    if _savings.goal_mode(g) == "sweep"
                    and float(g.get("monthly_plan") or 0) > 0]
    if _sweep_goals and st.get("plan_surplus") is not None:
        # the sweep lands at month END, so the current month's cap is the
        # surplus PROJECTED to that point (the standing surplus corrected
        # by today's pace variance — spending on-plan from here realizes
        # exactly it), not the smaller month-to-date accrual the Today
        # line quotes as "available so far"
        cur_surplus = max(0.0, st["plan_surplus"] - (st.get("variance") or 0))
        # a future month is not this one: this month's surplus is built
        # from its own deposits (a third paycheck, a tax refund, a check
        # that has not landed yet), so the months after it take the
        # standing figure from the averaged income instead
        std_surplus = max(0.0, standing_surplus(conn, st, today, memo=memo)
                          or 0.0)
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
    # the verdict's own pace (budget.pace_window, via paced_daily): spend
    # through yesterday over the finished days since the later of the 1st
    # and the ledger's first day — so the forecast burns at the rate the
    # verdict judges. Until the window is settled (the month's first days,
    # a ledger days old: the verdict's own test) the plan stands in for it.
    _paced = budget.paced_daily(st)
    pace_daily = (_paced if _paced is not None and st["pace_settled"]
                  else plan_daily)
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
        "no_checking": no_checking,
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
