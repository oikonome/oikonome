"""60-day cash-position forecast: when does headroom bottom out?

The primary checking account is per-tenant config `checking_account_id`,
falling back to the largest-balance depository checking account — never a
hardcoded institution.

Projects HEADROOM (checking − all card debt) forward day by day:
  − every bill occurrence on its due date (a weekend or bank holiday
    rolled to the business day after — bill_landing)
  − envelope bills spread evenly (what's LEFT of each pool ÷ days left
    in its period — the walk starts from live checking, which this
    period's envelope spend has already left)
  − variable spending at the PLAN pace and the CURRENT month pace
  − monthly-mode savings plans as scheduled outflows; sweep-mode plans
    only at each month end and only up to that month's own realized
    surplus (capped so the trough never moves — a sweep goal can never
    make the forecast read worse than having no goal)
  + paycheck occurrences (median of last 3 real deposits, capped by the
    active income-scenario biweekly), a weekend or bank-holiday payday
    walked in on the business day before unless the series' own deposits
    say otherwise (payday_landing)

Card-payoff scenarios:
  plan/pace   — full balance paid on each card's statement due date
                (conservative; no/stale due date ⇒ tomorrow)
  pace_now    — cash after card balances: checking less everything owed
                on cards, day by day (the quiet warning's walk)
  pace_today  — pay all cards today, then your usual (the selector's
                "Pay all cards today")
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
import functools
import hashlib
import json
import logging
import math
import os
import re
import statistics
import threading

from .. import localtime
from . import bills, budget, business_days
from .compat import as_date

HORIZON_DAYS = 60
# The forecast's model, kept with every night it records
# (specs/forecast-grading.md): bumped in the same commit as any change to
# an estimator (the pace, card timing, amounts, the band), so grading and
# the benchmark can tell one model's misses from another's.
FORECAST_MODEL = "w5"

log = logging.getLogger(__name__)

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


# A card's pending charges (specs/today-figures.md, "Pending card charges
# leave checking at the next statement"): its live pending rows with money
# out, less a hold a live posted row already names as its own — the
# settlement's link (live_txn), so a feed that posted the charge before it
# retired the hold still counts it once, in the posted balance.
_CARD_PENDING_SQL = """
    SELECT t.account_id, SUM(t.amount) AS pending
      FROM transactions t
     WHERE t.removed = 0 AND COALESCE(t.pending, 0) = 1 AND t.amount > 0
       AND t.account_id = ANY(%s)
       AND NOT EXISTS (
           SELECT 1 FROM transactions s
            WHERE s.removed = 0 AND COALESCE(s.pending, 0) = 0
              AND s.account_id = t.account_id AND s.id <> t.id
              AND (s.pending_transaction_id = t.id
                   OR (s.pending_transaction_id IS NULL
                       AND s.raw->>'pending_transaction_id' = t.id)))
     GROUP BY t.account_id"""


# A hold's settlement is dated on or after the hold; this much earlier is
# read too, for a feed that back-dates the posted row
PENDING_SETTLE_REACH_DAYS = 31


def yesterday_balances(conn, cards: list[dict], checking_id: str | None,
                       today: dt.date, checking_bal: float | None = None
                       ) -> tuple[str, dict, float | None]:
    """The cards' posted balances and checking's balance as they stood at
    the end of yesterday — today's less every posted row dated today
    (checking's plus them: its rows are spending positive) — with a
    key over them and the issuers' facts: (key, {card id: posted}, checking
    or None). What the forecast learns from balances reads these, never
    today's, so a cached fact and a fresh one are the same one."""
    ids = [c["id"] for c in cards] + ([checking_id] if checking_id else [])
    rows = ({r["account_id"]: float(r["s"]) for r in conn.execute(
        "SELECT account_id, SUM(amount) AS s FROM transactions "
        "WHERE removed = 0 AND COALESCE(pending, 0) = 0 AND date = %s "
        "AND account_id = ANY(%s) GROUP BY account_id",
        (today, ids)).fetchall()} if ids else {})
    posted = {c["id"]: round(c["posted"] - rows.get(c["id"], 0.0), 2)
              for c in cards}
    # checking: the walk's own starting balance (checking_pick: available,
    # else current), as it stood before today's rows; a row dated after
    # today is in no bank balance yet, so only today's are undone
    chk = None
    if checking_id and checking_bal is not None:
        chk = round(float(checking_bal) + rows.get(checking_id, 0.0), 2)
    vals = [f"{k}:{v}" for k, v in sorted(posted.items())] + [f"chk:{chk}"]
    vals.append(conn.execute(
        "SELECT COALESCE(md5(string_agg(account_id || ':' || raw::text, '|'"
        " ORDER BY account_id)), '') AS h FROM liabilities").fetchone()["h"])
    key = hashlib.md5("|".join(vals).encode(),
                      usedforsecurity=False).hexdigest()
    return key, posted, chk


def card_pending(conn, card_ids: list) -> dict[str, float]:
    """Each card's pending charges (_CARD_PENDING_SQL's rule): its live
    pending rows with money out, less a hold a live posted row names as
    its own (pending_transaction_id, or the feed's raw one). Read as two
    indexed passes — the holds, then the posted rows dated from a month
    before the oldest hold that name one — instead of a correlated scan
    of every posted row per hold, which on a long ledger was most of a
    second."""
    if not card_ids:
        return {}
    holds = conn.execute(
        """SELECT id, account_id, amount, date FROM transactions
            WHERE removed = 0 AND COALESCE(pending, 0) = 1 AND amount > 0
              AND account_id = ANY(%s)
            ORDER BY account_id, date, id""", (card_ids,)).fetchall()
    if not holds:
        return {}
    since = (min(as_date(h["date"]) for h in holds)
             - dt.timedelta(days=PENDING_SETTLE_REACH_DAYS))
    named = {(r["account_id"], r["ref"]) for r in conn.execute(
        """SELECT account_id, COALESCE(pending_transaction_id,
                                       raw->>'pending_transaction_id') AS ref
             FROM transactions
            WHERE removed = 0 AND COALESCE(pending, 0) = 0
              AND account_id = ANY(%s) AND date >= %s
              AND COALESCE(pending_transaction_id,
                           raw->>'pending_transaction_id') IS NOT NULL""",
        (card_ids, since)).fetchall()}
    out: dict[str, float] = {}
    for h in holds:
        if (h["account_id"], h["id"]) in named:
            continue
        out[h["account_id"]] = out.get(h["account_id"], 0.0) + float(
            h["amount"])
    return out


def card_balances(conn, excluded: list, *,
                  with_empty: bool = False) -> list[dict]:
    """Every card in the runway's scope (CARD_SCOPE_SQL) the cash timeline
    pays: {id, name, posted, pending, debt, due, stmt, issued, paid_on,
    paid}. `posted` is the
    balance the issuer reports, `pending` the card's pending charges (on
    no statement yet) beyond any credit the balance holds, `debt` the two
    together (posted never below $0; a card in credit owes its pending
    charges only past that credit);
    `due` the issuer's next due date as sent, `stmt` its last statement
    balance when it is a number; `issued`, `paid_on` and `paid` the
    issuer's last statement date and last payment (statement_owed), None
    when unreadable. Cards with nothing owed are left out unless
    `with_empty` (the card timing reads every walked card: a card paid
    off today is still charged tomorrow); the walk pays the ones owing
    more than half a dollar."""
    cards = conn.execute(
        f"""SELECT a.id, COALESCE(a.display_name, a.name) AS name,
                  COALESCE(a.balance_current, 0) AS bal,
                  l.raw ->> 'next_payment_due_date' AS due,
                  -- cast only what is a number: a restored ZIP can
                  -- carry "N/A" here, and a bare ::double precision
                  -- on it aborts this query, and with it every Today
                  -- load and nightly email for the household
                  CASE WHEN l.raw ->> 'last_statement_balance'
                            ~ '^-?[0-9]+(\\.[0-9]+)?$'
                       THEN (l.raw ->> 'last_statement_balance')::double precision
                  END AS stmt,
                  l.raw ->> 'last_statement_issue_date' AS issued,
                  CASE WHEN l.raw ->> 'minimum_payment_amount'
                            ~ '^-?[0-9]+(\\.[0-9]+)?$'
                       THEN (l.raw ->> 'minimum_payment_amount')::double precision
                  END AS minimum,
                  l.raw ->> 'last_payment_date' AS paid_on,
                  CASE WHEN l.raw ->> 'last_payment_amount'
                            ~ '^-?[0-9]+(\\.[0-9]+)?$'
                       THEN (l.raw ->> 'last_payment_amount')::double precision
                  END AS paid
           FROM accounts a LEFT JOIN liabilities l ON l.account_id = a.id
           WHERE a.type = 'credit' {CARD_SCOPE_SQL}
           ORDER BY a.id""", (excluded,)).fetchall()
    if not cards:
        return []
    pending = card_pending(conn, [c["id"] for c in cards])
    out = []
    for c in cards:
        bal = float(c["bal"] or 0)
        posted = max(bal, 0.0)
        # a card in credit absorbs its pending charges: it owes only what
        # is left of them beyond the credit
        pend = round(max(bal + pending.get(c["id"], 0.0), 0.0) - posted, 2)
        if not with_empty and posted <= 0 and pend <= 0:
            continue
        out.append({"id": c["id"], "name": c["name"], "posted": posted,
                    "pending": pend, "debt": posted + pend,
                    "due": c["due"], "stmt": c["stmt"],
                    "issued": _day_or_none(c["issued"]),
                    "paid_on": _day_or_none(c["paid_on"]),
                    "paid": c["paid"], "minimum": c["minimum"]})
    return out


def card_debt_total(conn, excluded: list) -> float:
    """The household's card debt: every in-scope card's posted balance and
    pending charges (card_balances' `debt`), summed once. The one figure
    the cash card ("card debt"), the runway and the cash notice built on
    it all read (specs/today-figures.md, "The notice says it pays every
    card")."""
    return round(sum(c["debt"] for c in card_balances(conn, excluded)), 2)


def _day_or_none(v) -> dt.date | None:
    try:
        return as_date(v) if v else None
    except ValueError:
        return None


def statement_owed(stmt: float, issued: dt.date | None,
                   paid_on: dt.date | None, paid: float | None) -> float:
    """What of a card's last statement is still owed at its due date
    (specs/today-figures.md, "A statement already paid is not scheduled
    again"): the issuer's last payment counts against the statement when
    it came on or after the statement's issue date — all of it when it
    covers the statement, the rest otherwise. A payment before the issue
    date paid an earlier statement; a missing date or amount is no
    payment."""
    if (issued is None or paid_on is None or paid is None
            or paid_on < issued or paid <= 0):
        return stmt
    return max(0.0, round(stmt - paid, 2))


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
                    COALESCE(balance_current, 0) DESC, id LIMIT 1""",
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


# The account type the Accounts editor offers "primary checking" on: the
# type eligible_checking_ids picks among. A pin saved on any other type is
# still honoured (pinned_account_live), and its switch stays on screen
# while it is on.
PIN_OFFERED_TYPE = "depository"


def pin_offered(account_type: str | None) -> bool:
    """Is "primary checking" offered on an account of this type?"""
    return (account_type or "") == PIN_OFFERED_TYPE


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
           WHERE a.type = %s AND a.subtype = 'checking'
             AND a.user_removed_at IS NULL
             -- a disconnected or restored-shell BANK's accounts are history,
             -- not a place the forecast can anchor; a manual or file-import
             -- account carries the same status after a restore and is live
             AND NOT (i.aggregator IN ('plaid','mx','simplefin','simplefin-org')
                      AND COALESCE(i.status, '') IN ('archived', 'restored'))
             AND (%s OR a.entity_id IS NULL)
           ORDER BY COALESCE(a.balance_current, 0) DESC, a.id""",
        (PIN_OFFERED_TYPE, combined)).fetchall()
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


def pickable_checking_ids(conn, cfg: dict, *,
                          in_use: bool = True) -> list[str]:
    """The accounts the household is ASKED to choose between: the eligible
    checking accounts that carry a balance, largest first. A closed
    account kept for its history (a Mint or bank-export import, a manual
    row nobody set a balance on) is eligible — the forecast may still fall
    through to it — but it is no answer to "which account does the cash
    forecast anchor on?", so it is not offered next to a live one. When
    no eligible account carries a balance yet (a files-only household's
    first import) they are all offered, so one is still pinned by itself
    and two are still a question. Among several that carry a balance,
    accounts with no transaction in the last 90 days give way to ones
    that have — while at least two of those remain, so the narrowing only
    ever shortens a question and never answers it: a list cut to one would
    be pinned without asking."""
    ids = eligible_checking_ids(conn, cfg)
    if len(ids) < 2:
        return ids
    rows = conn.execute(
        """SELECT id FROM accounts WHERE id = ANY(%s)
             AND COALESCE(balance_available, balance_current) IS NOT NULL""",
        (ids,)).fetchall()
    with_balance = {r["id"] for r in rows}
    live = [i for i in ids if i in with_balance] or ids
    # in_use=False: the list as the forecast itself ranks it, for the pin
    # that replaces one whose account is gone — that pin goes to the
    # account the forecast already falls through to, used lately or not
    if len(live) < 2 or not in_use:
        return live
    # An account that carries a balance can still be one nobody uses: a
    # closed account kept at $0 for its history, an old import with its
    # last statement balance. Where some of the candidates have moved money
    # in the last 90 days and others have not, only the ones in use are a
    # real answer to "which account do your bills run through".
    active = {r["account_id"] for r in conn.execute(
        """SELECT DISTINCT account_id FROM transactions
           WHERE account_id = ANY(%s) AND removed = 0
             AND date >= CURRENT_DATE - 90""", (live,)).fetchall()}
    in_use = [i for i in live if i in active]
    return in_use if len(in_use) >= 2 else live


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
        ids = pickable_checking_ids(conn, cfg, in_use=False)
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
    bring in on average (budget.income_monthly_figure, the Bills page's
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
    avg = budget.income_monthly_figure(
        conn, cfg, today, memo=memo, bill_rows=bill_rows)["monthly"]
    if avg <= 0:
        return ps
    return round(ps - float(st.get("income") or 0.0) + avg, 2)


def plan_savings_suggestion(conn, st: dict, today: dt.date, *,
                            memo: dict | None = None,
                            cfg: dict | None = None) -> int | None:
    """The monthly savings the planner suggests for the SAVED plan, from a
    live month_status `st`: what an ordinary month leaves (standing_surplus
    — never this month's own income, which a refund or a third paycheck
    moves) with the plan's own "Savings" goal put back, since that goal is
    the figure being suggested. Decided by budget.suggested_savings; None
    with no income to plan on."""
    from . import savings
    left = standing_surplus(conn, st, today, memo=memo, cfg=cfg)
    if left is None:
        return None
    own = sum(float(g.get("monthly_plan") or 0)
              for g in st.get("plan_goals") or []
              if savings.is_plan_savings_goal(g))
    return budget.suggested_savings(left + own)


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
    # never a paycheck the walk leaves out
    if up:
        stopped = stopped_paychecks(conn, cfg, today, memo=memo)
        up = [o for o in up if o["payee"] not in stopped]
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

    Semimonthly is the exception: detection stores a 1st-and-15th payroll
    as one series reading semimonthly, but a household can have entered the
    two paydays by hand as two MONTHLY series of the same payer, and a
    scenario that matched nothing there would silently stop reaching the
    forecast. So with no semimonthly series, the payer with two or more MONTHLY series (the
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


# A paycheck that follows last year's pattern (specs/today-figures.md, "A
# paycheck follows last year's pattern"): a series with a year of deposits
# whose last year shows a step that held for PAYCHECK_STEP_RUN deposits in a
# row is walked at the current amount times last year's ratio, held in
# PAYCHECK_RATIO_CLAMP.
PAYCHECK_PATTERN_MIN_DAYS = 365
PAYCHECK_STEP_MIN = 0.01
PAYCHECK_STEP_RUN = 2
PAYCHECK_RATIO_CLAMP = (0.85, 1.15)
PAYCHECK_CADENCE_TOLERANCE = 0.25
# a deposit under this share of the series' median deposit is no paycheck
# (a reimbursement the payer sends beside the checks): it is read neither
# as a step nor as the deposit beside one
PAYCHECK_STRAY_SHARE = 0.5
# the first check of a year below the steady checks just before it by at
# least MIN and at most MAX is a step on its own: deductions that reset
# with the year take it, and it may last one check. Steady: the
# PAYCHECK_YEAR_START_RUN checks before the year turn (fewer where the
# history has fewer, never under two) agree within PAYCHECK_STEP_MIN.
# A cut beyond MAX is something else (unpaid leave), not a reset.
PAYCHECK_YEAR_START_MIN = 0.03
PAYCHECK_YEAR_START_MAX = 0.10
PAYCHECK_YEAR_START_RUN = 3


def paycheck_pattern_amount(current: float, deposits: list, payday: dt.date,
                            today: dt.date) -> tuple[float, bool]:
    """The amount the forecast walks in for one future paycheck of a
    series — the one function. (amount, estimated): `current` (the walk's
    own per-deposit estimate, _paycheck_amount) unless all hold:
    - the series' deposits (`deposits`, [{date, amount}]) reach back
      PAYCHECK_PATTERN_MIN_DAYS (a new job is not read);
    - last year's cadence is today's: the median gap between deposits
      around a year ago is within PAYCHECK_CADENCE_TOLERANCE of the median
      gap of the last 90 days (a series that went from monthly to
      biweekly is not read);
    - last year's deposit at the payday's point in the year (the deposit
      nearest the payday a year earlier, within half a gap) differs from
      last year's deposit at today's point by at least PAYCHECK_STEP_MIN,
      and the deposit beside it (before or after) is within
      PAYCHECK_STEP_MIN of it: a step that held for PAYCHECK_STEP_RUN
      deposits, not one bonus check — or, for a January payday, that
      deposit is the first of its year, also in January, the checks just
      before it (up to PAYCHECK_YEAR_START_RUN, at least two) agree with
      one another within PAYCHECK_STEP_MIN (a steady pay, not hours that
      vary), and it is below their mean by PAYCHECK_YEAR_START_MIN to
      PAYCHECK_YEAR_START_MAX: the year-start dip, which may last one
      check. Its ratio is that deposit over that mean.
    Today's year-ago deposit is the base only when a deposit beside it
    agrees within PAYCHECK_STEP_MIN; a bonus check or a one-check dip there
    is no level, and the base is the median of it and its neighbours.
    A deposit under PAYCHECK_STRAY_SHARE of the series' median deposit is
    left out of all of it (no paycheck, however it was claimed).
    Then: current x (that deposit / today's year-ago deposit), the ratio
    held in PAYCHECK_RATIO_CLAMP (an early-year withholding drop or a
    wage-maximum rise moves a check by a few percent; a bonus or a job
    change by more), to the cent. The ratio carries over, never last
    year's level, so a raise since then is kept."""
    deps = sorted(((as_date(r["date"]), float(r["amount"]))
                   for r in deposits if float(r["amount"]) > 0),
                  key=lambda x: (x[0], x[1]))
    if deps:
        floor = PAYCHECK_STRAY_SHARE * statistics.median(a for _d, a in deps)
        deps = [x for x in deps if x[1] >= floor]
    if (not deps or current <= 0 or deps[0][0] > today - dt.timedelta(
            days=PAYCHECK_PATTERN_MIN_DAYS)):
        return current, False

    def gaps(lo: dt.date, hi: dt.date) -> float | None:
        ds = [d for d, _a in deps if lo <= d <= hi]
        g = [(b - a).days for a, b in zip(ds, ds[1:])]
        return statistics.median(g) if g else None

    now_gap = gaps(today - dt.timedelta(days=90), today)
    year = dt.timedelta(days=365)
    base_on, target_on = today - year, payday - year
    then_gap = gaps(min(base_on, target_on) - dt.timedelta(days=45),
                    max(base_on, target_on) + dt.timedelta(days=45))
    if (not now_gap or not then_gap
            or abs(then_gap - now_gap) > PAYCHECK_CADENCE_TOLERANCE * now_gap):
        return current, False
    reach = max(3, then_gap / 2)

    def nearest(on: dt.date) -> int | None:
        i = min(range(len(deps)), key=lambda k: abs((deps[k][0] - on).days))
        return i if abs((deps[i][0] - on).days) <= reach else None

    bi, ti = nearest(base_on), nearest(target_on)
    if bi is None or ti is None or bi == ti:
        return current, False
    base, target = deps[bi][1], deps[ti][1]
    # the base is a level only when a neighbour agrees with it; one bonus
    # check or a one-check dip there is read as the checks around it
    if not any(0 <= j < len(deps) and j != ti
               and abs(deps[j][1] / base - 1) < PAYCHECK_STEP_MIN
               for j in (bi - 1, bi + 1)):
        base = statistics.median(a for _d, a in deps[max(0, bi - 1):bi + 2])
    ratio = target / base
    if abs(ratio - 1) < PAYCHECK_STEP_MIN:
        return current, False
    held = any(0 <= j < len(deps) and j != bi
               and abs(deps[j][1] / target - 1) < PAYCHECK_STEP_MIN
               for j in (ti - 1, ti + 1))
    if not held:
        ratio = _year_start_ratio(deps, ti, payday)
        if ratio is None:
            return current, False
    ratio = min(max(ratio, PAYCHECK_RATIO_CLAMP[0]), PAYCHECK_RATIO_CLAMP[1])
    return round(current * ratio, 2), True


def _year_start_ratio(deps: list, ti: int, payday: dt.date) -> float | None:
    """paycheck_pattern_amount's year-start step: deps[ti] (last year's
    check at a January payday's point) over the mean of the steady checks
    just before the year turned, when it is the first check of its year
    and that dip is a deduction reset's size; else None."""
    t_day, target = deps[ti]
    if payday.month != 1 or t_day.month != 1 or ti < 2:
        return None
    before = deps[max(0, ti - PAYCHECK_YEAR_START_RUN):ti]
    if before[-1][0].year >= t_day.year:
        return None
    amts = [a for _d, a in before]
    if max(amts) > min(amts) * (1 + PAYCHECK_STEP_MIN):
        return None
    ratio = target / statistics.mean(amts)
    if not 1 - PAYCHECK_YEAR_START_MAX <= ratio <= 1 - PAYCHECK_YEAR_START_MIN:
        return None
    return ratio


# A single large purchase the forecast does not burn every day
# (specs/today-figures.md, "A one-off is not a pace"): the purchase a
# merchant's largest charge in the pace window makes (one_off_purchase),
# at least ONE_OFF_FACTOR times the window's median day of variable
# spending and at least ONE_OFF_MIN, its largest charge at least
# ONE_OFF_LEAD_SHARE of it, from a merchant with no habit of purchases
# that size — no earlier charge of at least ONE_OFF_PRIOR_SHARE of it in
# the ONE_OFF_LOOKBACK_DAYS before, and not two in the ONE_OFF_HABIT_DAYS.
ONE_OFF_FACTOR = 5
ONE_OFF_MIN = 200.0
ONE_OFF_LOOKBACK_DAYS = 180
ONE_OFF_HABIT_DAYS = 365
ONE_OFF_PRIOR_SHARE = 1 / 3
ONE_OFF_LEAD_SHARE = 0.5


def one_off_charges(conn, st: dict, today: dt.date) -> list[dict]:
    """The month's one-offs: window_one_offs over the verdict's pace
    window (pace start through yesterday, the days budget.paced_daily
    divides by) and its variable rows. The forecast reads its own window
    (forecast_pace); this is the month's view of the same rule."""
    start = st["pace_start"]
    rows = [r for k in ("food", "other") for r in st["buckets"][k]["rows"]
            if start <= as_date(r["date"]) < today]
    return window_one_offs(conn, rows, start, st["pace_days"])


def _cents(x) -> int:
    """A dollar figure in whole cents, the unit a one-off is decided in."""
    return int(round(float(x) * 100))


# rows of one merchant this close together may be one purchase (a deposit
# and the balance, a diagnostic and the repair)
ONE_OFF_PURCHASE_SPAN_DAYS = 7


def one_off_purchase(charges: list[dict]) -> tuple[dict, list[dict]]:
    """The outlier a merchant's charges in a window may hold: (its largest
    charge, the charges that make the purchase with it). The purchase is
    the largest charge, every charge of similar size (at least half of
    it), and — only when the merchant has no charge in the window outside
    ONE_OFF_PURCHASE_SPAN_DAYS of the largest (it appears for this
    purchase alone) — the smaller charges beside it in that span. A
    merchant's everyday charges are never part of it: a deli visited every
    week keeps its visits in the pace when one large order leaves.
    `charges` carry `cents`, `date`, `txn_id`; amounts in cents."""
    lead = max(charges, key=lambda r: (r["cents"], str(r["date"]),
                                       r["txn_id"]))
    on = as_date(lead["date"])
    span = dt.timedelta(days=ONE_OFF_PURCHASE_SPAN_DAYS)
    near = [c for c in charges if abs(as_date(c["date"]) - on) <= span]
    alone = len(near) == len(charges)
    part = [c for c in charges
            if c is lead or c["cents"] * 2 >= lead["cents"]
            or (alone and abs(as_date(c["date"]) - on) <= span)]
    return lead, part


def window_one_offs(conn, rows: list[dict], start: dt.date,
                    days: int, *, habit_of=None,
                    with_parts: bool = False) -> list[dict]:
    """The variable purchases a pace taken over a window leaves out. The
    rows judged are the pace's own numerator — every variable row on a
    finished day of the window (`start` and the `days` after it), posted
    or pending alike, since the numerator counts both. Each merchant ("the
    same merchant" as the anomaly watch asks it: the row's merchant, else
    its descriptor) is judged by the one purchase its largest charge makes
    (one_off_purchase): that charge, the charges of similar size beside
    it, and — for a merchant that appears for this purchase alone — the
    smaller charges within ONE_OFF_PURCHASE_SPAN_DAYS of it (a diagnostic
    before the repair, a deposit before the furniture). Its everyday
    charges are never part of it and stay in the pace. The purchase is
    left out when it passes the floor (ONE_OFF_FACTOR x the median of
    those days' variable spending, a day with none counting as $0, and at
    least ONE_OFF_MIN), when the largest charge carries at least
    ONE_OFF_LEAD_SHARE of it (one purchase, not a run of large ones), and
    when the merchant has no habit of purchases that size: a charge before
    the window counts as one only when it is at least ONE_OFF_PRIOR_SHARE
    of the purchase — one in ONE_OFF_LOOKBACK_DAYS, or two in
    ONE_OFF_HABIT_DAYS; small regular visits are no habit for a purchase
    ten times their size. A furniture order or a car repair is real money
    gone, already out of the starting balance; it is not a rate the
    household spends at every day for the next two months. Returns
    [{txn_id, payee, amount, date}] largest first, one per merchant: the
    largest charge's id, payee and date, the purchase's total (with
    `with_parts`, also `txn_ids`: every charge of the purchase)."""
    from . import merchant_sql
    if not rows or days < 1:
        return []
    # decided in whole cents: a tie (two equal rows, a row exactly half or
    # a third of a total) must not turn on float noise in a sum
    per_day: dict[dt.date, int] = {}
    for r in rows:
        d = as_date(r["date"])
        per_day[d] = per_day.get(d, 0) + _cents(r["amount"])
    median = statistics.median(
        [per_day.get(start + dt.timedelta(days=i), 0)
         for i in range(days)])
    floor = max(_cents(ONE_OFF_MIN), ONE_OFF_FACTOR * median)
    # a split purchase's parts share one charge: judge the charge. An
    # envelope's overflow slice (txn_id None) is no charge at all: it is
    # the envelope's overspend, spending the household does at its usual
    # pace, so it stays in the pace (and in the median day above); the
    # purchase it was carved from is the envelope's, never a variable row.
    whole: dict[str, dict] = {}
    for r in rows:
        if r["txn_id"] is None:
            continue
        w = whole.setdefault(r["txn_id"], dict(r, cents=0))
        w["cents"] += _cents(r["amount"])
    by_merchant: dict[str, list[dict]] = {}
    for r in whole.values():
        key = (r.get("merchant_id")
               or "raw:" + (r["payee"] or "").strip().lower())
        by_merchant.setdefault(str(key), []).append(r)
    out = []
    for group in by_merchant.values():
        # the outlier and what makes the purchase with it, never the
        # merchant's everyday charges (one_off_purchase)
        lead, part = one_off_purchase(group)
        total = sum(r["cents"] for r in part)
        # the lead carries at least ONE_OFF_LEAD_SHARE (a half) of it
        if total < floor or lead["cents"] * 2 < total:
            continue
        on = as_date(lead["date"])
        m = (lead["payee"] or "").strip().lower()
        if habit_of is not None:
            half, year = habit_of(lead, on, start, total)
        else:
            # an earlier charge of at least ONE_OFF_PRIOR_SHARE (a third)
            # of the total, compared in cents
            habit = conn.execute(
                f"""SELECT COUNT(*) FILTER (WHERE date >= %s) AS half,
                          COUNT(*) AS year
                   FROM transactions
                   WHERE removed = 0 AND date < %s AND date >= %s
                     AND ROUND(amount * 100) * 3 >= %s
                     AND (LOWER(BTRIM({merchant_sql.RAW_KEY_UNALIASED})) = %s
                          OR merchant_id = %s::uuid)""",
                (on - dt.timedelta(days=ONE_OFF_LOOKBACK_DAYS), start,
                 on - dt.timedelta(days=ONE_OFF_HABIT_DAYS),
                 total, m, lead.get("merchant_id"))).fetchone()
            half, year = habit["half"], habit["year"]
        if half >= 1 or year >= 2:
            continue
        lead = dict(lead, amount=total / 100)
        o = {"txn_id": lead["txn_id"],
             "payee": merchant_sql.shown_payee(lead),
             "amount": round(total / 100, 2),
             "date": on.isoformat()}
        if with_parts:
            o["txn_ids"] = sorted(str(r["txn_id"]) for r in part)
        out.append(o)
    out.sort(key=lambda o: -o["amount"])
    return out


# What the forecast learns from history — the heavier-month factor, each
# card's timing and the payday habit, the bills' paying accounts and past
# payments, the usual pace, and each closed month's variable rows — moves
# only with a write to what it reads. It is kept per household in the
# worker, the way reporting keeps a closed month's fixed total
# (_FIXED_MONTH_CACHE): under a stamp of everything it reads (the bills,
# the config and the ledger tables, reporting._fixed_inputs_stamp; every
# dated ledger write, reporting._fixed_month_probe; the issuers' facts in
# liabilities) and the household's day. Any sync, edit or new day misses;
# the same day with no write is served from memory, by every request.
class _Kept:
    """A per-household store with bounds: an entry unused for
    KEPT_IDLE_S is dropped, and past KEPT_HOUSEHOLDS households the least
    recently used goes first. Every read touches its entry."""

    def __init__(self, households: int):
        from collections import OrderedDict
        self.max = households
        self.items: "OrderedDict[str, tuple[float, object]]" = OrderedDict()
        self.lock = threading.Lock()

    def get(self, tid: str):
        import time
        with self.lock:
            hit = self.items.get(tid)
            if hit is None:
                return None
            if time.monotonic() - hit[0] > KEPT_IDLE_S:
                del self.items[tid]
                return None
            self.items[tid] = (time.monotonic(), hit[1])
            self.items.move_to_end(tid)
            return hit[1]

    def put(self, tid: str, value) -> None:
        import time
        now = time.monotonic()
        with self.lock:
            self.items[tid] = (now, value)
            self.items.move_to_end(tid)
            for k in [k for k, (t, _v) in self.items.items()
                      if now - t > KEPT_IDLE_S]:
                del self.items[k]
            while len(self.items) > self.max:
                self.items.popitem(last=False)

    def __len__(self):
        return len(self.items)

    def clear(self) -> None:
        with self.lock:
            self.items.clear()


# an entry unused this long is dropped; at most this many households kept
KEPT_IDLE_S = 24 * 3600.0
KEPT_HOUSEHOLDS = 200
_HISTORY = _Kept(KEPT_HOUSEHOLDS)
_LEARNED_LOCK = threading.Lock()
# (tenant, year, month) -> (stamp, variable rows) for a closed month
_CLOSED_ROWS = _Kept(KEPT_HOUSEHOLDS)
# a build reads closed months this far back at most (the heavier month's
# year and its pace window); older ones are not kept
CLOSED_KEPT_MONTHS = 16


def forget_learned() -> None:
    """Drop every household's kept facts and forecasts, and stop reading
    the stored rows this process wrote before now (a test that swaps one
    of the functions they are computed by calls it first). A row another
    process wrote is still read: it was learned by the same rules."""
    global _FORGET_EPOCH
    with _LEARNED_LOCK:
        _HISTORY.clear()
        _CLOSED_ROWS.clear()
        _BUILT.clear()
        _FORGET_EPOCH += 1


# What the forecast learned, stored per household per day
# (forecast_learned; specs/today-figures.md, "What the forecast learned is
# stored for the day"). The facts are a pure function of the ledger
# through yesterday and the day, so the row the nightly step wrote serves
# every worker and every restart, and a build that reads it equals one
# that learns again. LEARNED_RULE_VERSION names the rules a row was
# learned by: a row written under another number is never read.
#
# A row is stamped with the code that learned it (code_identity): any
# deploy learns once again, lazily, on the household's first build after
# it — one recompute per household per deploy, never a row learned by
# other rules. LEARNED_RULE_VERSION stays as a manual escape hatch (bump it
# to set every row aside without a deploy).
LEARNED_RULE_VERSION = 2
# a household keeps its newest stored days only: the build of a day reads
# that day's row, and a late worker may still build yesterday's
LEARNED_KEEP_DAYS = 1
# the worker's own copy of a household's facts is a short layer over the
# stored row: one request's builds and the page's next few share it
LEARNED_KEPT_S = 600.0
# forget_learned's count: a stored row this process wrote before the
# newest forget is not read again (`written_by`)
_FORGET_EPOCH = 0


_CODE_IDENTITY: str | None = None


def code_identity() -> str:
    """The running code, as a stored row records it: the release the app
    reports (OIKONOME_VERSION, the install's git describe) when it names
    one exact release; otherwise — a development checkout ("dev"), a tree
    with no git ("unknown"), a deploy with local edits ("…-dirty"), none at
    all — that word with a digest of the engine's source beside it, so two
    different trees never share an identity."""
    global _CODE_IDENTITY
    if _CODE_IDENTITY is None:
        v = (os.environ.get("OIKONOME_VERSION") or "").strip()
        if v and v not in ("dev", "unknown") and not v.endswith("-dirty"):
            _CODE_IDENTITY = v
        else:
            import pathlib
            h = hashlib.sha256()
            here = pathlib.Path(__file__).resolve().parent
            for f in sorted(here.glob("*.py")):
                h.update(f.name.encode())
                h.update(f.read_bytes())
            _CODE_IDENTITY = f"{v or 'dev'}+src:{h.hexdigest()[:16]}"
    return _CODE_IDENTITY


def _stored_stamp(stamp: str) -> str:
    """The stamp a stored row carries: the history's, and the code's."""
    return f"{stamp}|{code_identity()}"


def _writer() -> str:
    return f"{os.getpid()}:{_FORGET_EPOCH}"


def _forgotten(written_by: str | None) -> bool:
    """Did this process write the row before its newest forget_learned?"""
    pid, _, epoch = (written_by or "").partition(":")
    return (pid == str(os.getpid()) and epoch.isdigit()
            and int(epoch) < _FORGET_EPOCH)


# The stored encoding of a learned fact: JSON, with every Python type a
# fact holds coming back as the same type. A dict is {"m": [k, v, ...]}
# (its keys may be dates or tuples, and its order is kept), a tuple
# {"t": [...]}, a set {"s": [...]}, a frozenset {"z": [...]}; a date
# "@d<iso>", a datetime "@t<iso>", a Decimal "@D<text>", a UUID "@u<text>",
# a float JSON cannot say exactly (infinite, NaN, minus zero) "@f<repr>",
# and a string that begins with "@" gets one more. Anything else is not
# stored (the fact is learned again by the next build that needs it).
def encode_learned(v):
    import decimal
    import uuid
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        if math.isfinite(v) and not (v == 0.0 and math.copysign(1.0, v) < 0):
            return v
        return "@f" + repr(v)
    if isinstance(v, str):
        return "@" + v if v.startswith("@") else v
    if isinstance(v, dt.datetime):
        return "@t" + v.isoformat()
    if isinstance(v, dt.date):
        return "@d" + v.isoformat()
    if isinstance(v, decimal.Decimal):
        return "@D" + str(v)
    if isinstance(v, uuid.UUID):
        return "@u" + str(v)
    if isinstance(v, list):
        return [encode_learned(x) for x in v]
    if isinstance(v, tuple):
        return {"t": [encode_learned(x) for x in v]}
    if isinstance(v, frozenset):
        return {"z": [encode_learned(x) for x in v]}
    if isinstance(v, set):
        return {"s": [encode_learned(x) for x in v]}
    if isinstance(v, dict):
        out = []
        for k, x in v.items():
            out.append(encode_learned(k))
            out.append(encode_learned(x))
        return {"m": out}
    raise TypeError(f"a learned fact cannot hold {type(v).__name__}")


def decode_learned(v):
    """encode_learned's inverse."""
    if isinstance(v, str):
        if not v.startswith("@"):
            return v
        tag, body = v[1:2], v[2:]
        if tag == "@":
            return v[1:]
        if tag == "d":
            return dt.date.fromisoformat(body)
        if tag == "t":
            return dt.datetime.fromisoformat(body)
        if tag == "f":
            return float(body)
        if tag == "D":
            import decimal
            return decimal.Decimal(body)
        if tag == "u":
            import uuid
            return uuid.UUID(body)
        raise ValueError(f"unknown learned tag {tag!r}")
    if isinstance(v, list):
        return [decode_learned(x) for x in v]
    if isinstance(v, dict):
        (tag, items), = v.items()
        if tag == "m":
            it = iter(items)
            return {_hashable(decode_learned(k)): decode_learned(x)
                    for k, x in zip(it, it)}
        vals = [decode_learned(x) for x in items]
        if tag == "t":
            return tuple(vals)
        if tag == "z":
            return frozenset(vals)
        if tag == "s":
            return set(vals)
        raise ValueError(f"unknown learned container {tag!r}")
    return v


def _hashable(k):
    if isinstance(k, list):
        return tuple(_hashable(x) for x in k)
    return k


class _Learned:
    """One household's learned facts for a day, as the worker holds them:
    the stamp they were learned under, the facts, the names learned in
    this worker and not yet stored, and when the entry was made (it is
    read again from the stored row after LEARNED_KEPT_S)."""
    __slots__ = ("stamp", "day", "facts", "unstored", "made")

    def __init__(self, stamp: str, day: dt.date, facts: dict):
        import time
        self.stamp, self.day, self.facts = stamp, day, facts
        self.unstored: set[str] = set()
        self.made = time.monotonic()


def _stored_facts(conn, day: dt.date, stamp: str) -> dict:
    """The facts the household's stored row for `day` holds, when it was
    learned under `stamp` by these rules; else {}."""
    try:
        # in a savepoint of its own: an instance whose migrations have not
        # created the table yet still forecasts (it learns every build)
        with conn.transaction():
            row = conn.execute(
                "SELECT rule_version, stamp, written_by, facts::text AS facts "
                "FROM forecast_learned WHERE as_of = %s", (day,)).fetchone()
    except Exception as e:                       # noqa: BLE001
        log.info("stored forecast facts not read: %s", e)
        return {}
    if row is None:
        return {}
    why = ("rules" if row["rule_version"] != LEARNED_RULE_VERSION
           else "code" if not row["stamp"].endswith(
               "|" + code_identity())
           else "history" if row["stamp"] != _stored_stamp(stamp)
           else "forgotten" if _forgotten(row["written_by"]) else None)
    if why is not None:
        # the reason a stored row was set aside, for anyone timing a cold
        # worker (a row another code wrote is learned again, by design)
        log.info("stored forecast facts for %s not read: %s", day, why)
        return {}
    try:
        raw = json.loads(row["facts"])
        return {name: decode_learned(v) for name, v in raw.items()}
    except (ValueError, TypeError) as e:
        log.warning("stored forecast facts unreadable, learned again: %s", e)
        return {}


def _learned_entry(conn, cfg: dict, today: dt.date) -> tuple[str, "_Learned"]:
    """The household's learned facts for `today` under the history it has
    now: the worker's copy while it is young and its stamp holds, else the
    stored row's, else nothing yet."""
    import time
    tid, stamp = history_stamp(conn, cfg, today)
    hit = _HISTORY.get(tid)
    if (hit is not None and hit.stamp == stamp
            and time.monotonic() - hit.made <= LEARNED_KEPT_S):
        return tid, hit
    with _LEARNED_LOCK:
        hit = _HISTORY.get(tid)
        if (hit is not None and hit.stamp == stamp
                and time.monotonic() - hit.made <= LEARNED_KEPT_S):
            return tid, hit
    entry = _Learned(stamp, today, _stored_facts(conn, today, stamp))
    with _LEARNED_LOCK:
        hit = _HISTORY.get(tid)
        if hit is not None and hit.stamp == stamp and hit.unstored:
            # learned here meanwhile and not stored yet: keep it
            entry.facts = {**entry.facts, **hit.facts}
            entry.unstored = set(hit.unstored)
        _HISTORY.put(tid, entry)
    return tid, entry


def store_learned(conn, today: dt.date, *, tid: str | None = None) -> bool:
    """Write the household's learned facts for `today` when this worker
    learned any it has not stored (the first build of a day, or the first
    after a write dated before today). Never raises: a row that cannot be
    written is learned again by the next build. Whether a row was written."""
    tid = tid or _tenant(conn)
    entry = _HISTORY.get(tid)
    if entry is None or entry.day != today or not entry.unstored:
        return False
    with _LEARNED_LOCK:
        facts = dict(entry.facts)
        names = set(entry.unstored)
    # the history read again now, past every memo: a write that landed
    # while the facts were being learned (from another connection, which
    # this checkout's memo cannot see) means they may be the new history's
    # under the old stamp — they are not stored, and this worker forgets
    # them (the pattern of reporting._fixed_month_stamps)
    held = getattr(conn, "_memo", None)
    try:
        if hasattr(conn, "_memo"):
            conn._memo = None
        _t, now_stamp = history_stamp(
            conn, budget.load_config(conn), today)
    except Exception as e:                       # noqa: BLE001
        log.warning("forecast facts not stored: %s", e)
        return False
    finally:
        if hasattr(conn, "_memo"):
            conn._memo = held
    if now_stamp != entry.stamp:
        with _LEARNED_LOCK:
            if _HISTORY.get(tid) is entry:
                _HISTORY.items.pop(tid, None)
        return False
    enc = {}
    for name, v in facts.items():
        try:
            enc[name] = encode_learned(v)
        except TypeError as e:
            log.info("forecast fact %s not stored: %s", name.split(":")[0], e)
    try:
        text = json.dumps(enc, separators=(",", ":"), allow_nan=False)
        with conn.transaction():
            # the day's row replaced, and the days past keeping dropped.
            # Two plain statements naming their one table, not an upsert:
            # the connection drops only the answers read from a table a
            # statement names, and an upsert's wording reads to it as a
            # schema change that drops them all — the page's other reads
            # would run twice. A second worker storing the same day at the
            # same moment loses the insert; its facts are the same ones.
            conn.execute(
                "DELETE FROM forecast_learned WHERE as_of = %s OR as_of < %s",
                (today, today - dt.timedelta(days=LEARNED_KEEP_DAYS)))
            conn.execute(
                """INSERT INTO forecast_learned
                       (as_of, rule_version, stamp, written_by, facts)
                   VALUES (%s, %s, %s, %s, %s::json)""",
                (today, LEARNED_RULE_VERSION, _stored_stamp(entry.stamp),
                 _writer(), text))
    except Exception as e:                       # noqa: BLE001
        log.warning("forecast facts not stored: %s", e)
        return False
    with _LEARNED_LOCK:
        entry.unstored -= names
    return True


def _tenant(conn) -> str:
    return str(conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"])


def learned_stamp(conn, cfg: dict, today: dt.date) -> tuple[str, str]:
    """(tenant, stamp) the learned facts are kept under: the day and a
    fingerprint of every input they read. One per checkout."""
    key = ("forecast.learned_stamp", today)
    hit = getattr(conn, "memo_get", lambda k: None)(key)
    if hit is not None:
        return hit
    from . import reporting
    liab = conn.execute(
        "SELECT COALESCE(md5(string_agg(account_id || ':' || raw::text, '|'"
        " ORDER BY account_id)), '') AS h FROM liabilities").fetchone()["h"]
    # the balances the walk starts from (the closed-month stamp leaves them
    # out on purpose: a pull rewrites them; here they are inputs)
    bals = conn.execute(
        "SELECT COALESCE(md5(string_agg(id || ':' || COALESCE("
        "balance_current::text, '') || ':' || COALESCE("
        "balance_available::text, ''), '|' ORDER BY id)), '') AS h "
        "FROM accounts").fetchone()["h"]
    stamp = hashlib.md5(
        "|".join((today.isoformat(), reporting._fixed_inputs_stamp(conn, cfg),
                  reporting._fixed_month_probe(conn), liab, bals)).encode(),
        usedforsecurity=False).hexdigest()
    out = (_tenant(conn), stamp)
    if hasattr(conn, "memo_put"):
        conn.memo_put(key, out, *budget._LEDGER_TABLES, "bills",
                      "liabilities", "ledger_writes")
    return out


# Settings a send, a delivery, a mute or the outbound pause writes, which
# no learned fact reads: a send stamp in the settings must not make the
# day's facts be learned again (history_stamp). A key is listed here only
# when nothing the forecast learns from reads it.
NOT_LEARNED_SETTINGS = frozenset({"savings_goal_state", "outbound_paused",
                                  "demo_read_only"})
NOT_LEARNED_PREFIXES = ("email_", "sms_", "push_", "notify_", "webhook")


def learned_config(cfg: dict) -> dict:
    """The settings the learned facts may read (history_stamp)."""
    return {k: v for k, v in cfg.items()
            if k not in NOT_LEARNED_SETTINGS
            and not k.startswith(NOT_LEARNED_PREFIXES)}


def history_stamp(conn, cfg: dict, today: dt.date) -> tuple[str, str]:
    """(tenant, stamp) of the history through yesterday: the day, the
    bills, config and undated ledger tables (reporting._fixed_inputs_stamp)
    and every ledger row dated before today with what hangs off it. A
    charge synced today, or a balance moving, leaves it unchanged; a row
    dated yesterday re-filed today moves it. One per checkout."""
    key = ("forecast.history_stamp", today)
    hit = getattr(conn, "memo_get", lambda k: None)(key)
    if hit is not None:
        return hit
    from . import reporting
    rows = conn.execute(
        """SELECT (SELECT count(*) || ':' || COALESCE(sum(xmin::text::bigint), 0)
                     FROM transactions WHERE date < %s) AS t,
                  (SELECT count(*) || ':' || COALESCE(sum(xmin::text::bigint), 0)
                     FROM transaction_splits) AS s,
                  (SELECT count(*) || ':' || COALESCE(sum(xmin::text::bigint), 0)
                     FROM manual_categories) AS m,
                  (SELECT count(*) || ':' || COALESCE(sum(xmin::text::bigint), 0)
                     FROM reimbursements) AS r""", (today,)).fetchone()
    stamp = hashlib.md5(
        "|".join((today.isoformat(),
                  reporting._inputs_stamp(conn, learned_config(cfg),
                                          settings_row=False),
                  rows["t"], rows["s"], rows["m"], rows["r"])).encode(),
        usedforsecurity=False).hexdigest()
    out = (_tenant(conn), stamp)
    if hasattr(conn, "memo_put"):
        conn.memo_put(key, out, *budget._LEDGER_TABLES, "bills")
    return out


def learned(conn, cfg: dict, today: dt.date, name: str, compute):
    """`compute()` — a fact the forecast learned, which reads the ledger as
    it stood at the end of yesterday and nothing dated today ("what the
    forecast learned is yesterday's; what it sees today is today's") — kept
    per household under history_stamp, which a write dated before today
    moves and a write dated today does not. A cached fact and a fresh one
    are therefore the same by construction. A name may carry a key of its
    own after a colon (the balances it read, "timing:<key>"); only the
    newest such entry of a name is kept. Read-only to callers.

    Kept in two layers: the household's stored row for the day
    (forecast_learned, written by the nightly forecast step and by
    store_learned after a build that learned something), and the worker's
    copy of it for LEARNED_KEPT_S. A row learned under another stamp (a
    write dated before today since) or other rules is not read."""
    _tid, entry = _learned_entry(conn, cfg, today)
    facts = entry.facts
    if name in facts:
        return facts[name]
    val = compute()
    with _LEARNED_LOCK:
        head = name.split(":", 1)[0] + ":"
        if ":" in name:
            for k in [k for k in facts if k.startswith(head)]:
                del facts[k]
        facts[name] = val
        entry.unstored.add(name)
    return val


def _closed_month_rows(conn, y: int, m: int, memo: dict | None,
                       today: dt.date | None = None) -> list:
    """The variable rows (Food, Everything else) of a closed month as
    budget.month_status buckets them under today's config and schedule,
    kept per household while nothing the month reads is written: the
    bills, the config and the undated ledger tables
    (reporting._fixed_inputs_stamp) and the dated rows of every month its
    walk reads (reporting's reach). With `today`, the month is read as the
    ledger stood at the end of yesterday: a month whose matching reaches
    into today's month matches no row dated today or later
    (month_status's rows_until), so a payment arriving today re-files none
    of its rows until tomorrow — the forecast's closed months are
    yesterday's too."""
    from . import reporting
    cfg = budget.load_config(conn, memo=memo)
    stamps = reporting._fixed_month_stamps(conn)
    first = dt.date(y, m, 1)
    # the months a closed month's walk reads, as reporting defines them:
    # the prepay window before it, and an annual envelope's whole year
    has_annual = any(e.get("period_months", 1) > 1
                     for e in budget._envelope_bills(conn, memo=memo))
    stop = reporting._fixed_month_stop(first, has_annual)
    # the bill matching of a closed month reads rows through the month
    # after it plus the prepay window (month_occurrence_pass): only a
    # month whose matching reaches today is capped, and read again each day
    after = budget._month_after(y, m)
    reach_end = (budget._month_after(after.year, after.month)
                 + dt.timedelta(days=budget.PREPAY_MATCH_DAYS))
    cap = today if today is not None and reach_end > today else None
    stamp = "|".join((reporting._fixed_inputs_stamp(conn, cfg),
                      reporting._fixed_month_digest(
                          stamps, reporting._fixed_month_reach(first), stop),
                      f"until:{cap}"))
    tid = _tenant(conn)
    months = _CLOSED_ROWS.get(tid) or {}
    hit = months.get((y, m))
    if hit is not None and hit[0] == stamp:
        return hit[1]
    ms = budget.month_status(conn, dt.date(y, m, calendar.monthrange(y, m)[1]),
                             closed=True, memo=memo, rows_until=cap)
    rows = [r for k in ("food", "other") for r in ms["buckets"][k]["rows"]]
    # only the months a build can read are kept: the newest
    # CLOSED_KEPT_MONTHS of the household's
    kept = dict(months)
    kept[(y, m)] = (stamp, rows)
    for k in sorted(kept)[:-CLOSED_KEPT_MONTHS]:
        del kept[k]
    _CLOSED_ROWS.put(tid, kept)
    return rows


# The forecast's daily pace (specs/today-figures.md, "The forecast's pace is
# the last 60 days"): the household's variable spending over the
# TRAILING_PACE_DAYS finished days before today, less that window's
# one-offs, per finished day. Under budget.MIN_RATE_DAYS finished days of
# ledger the plan's daily stands in for it.
TRAILING_PACE_DAYS = 60


def trailing_variable_rows(conn, today: dt.date, *, st: dict | None = None,
                           memo: dict | None = None
                           ) -> tuple[list[dict], dt.date, int]:
    """The variable rows (Food, Everything else and their carve-outs,
    posted and pending) of the TRAILING_PACE_DAYS finished days before
    `today`, cut at the ledger's first day: (rows, first day, finished
    days). Each month the window touches is bucketed by
    budget.month_status under TODAY's live config and schedule — a
    deliberate live read for a past month: a charge today's schedule
    calls a bill is walked as that bill, so it is never pace, whatever
    the month's frozen schedule said. A closed month is read as closed
    (`closed=True`), the running month is `st` when the caller has it.
    Today's spending is never in the window."""
    return variable_rows_since(conn, today,
                               today - dt.timedelta(days=TRAILING_PACE_DAYS),
                               st=st, memo=memo)


def variable_rows_since(conn, today: dt.date, start: dt.date, *,
                        st: dict | None = None, memo: dict | None = None
                        ) -> tuple[list[dict], dt.date, int]:
    """trailing_variable_rows over the finished days from `start` (cut at
    the ledger's first day) through yesterday."""
    cfg = budget.load_config(conn, memo=memo)
    first = budget.ledger_start(conn, budget.excluded_account_ids(cfg))
    if first is None or first >= today:
        return [], today, 0
    start = max(start, first)
    days = (today - start).days
    rows: list[dict] = []
    yesterday = today - dt.timedelta(days=1)
    for y, m in budget.months_spanned(start, yesterday):
        if (y, m) == (today.year, today.month):
            # as of yesterday (forecast_pace passes it); never today's
            # buckets, whose matching reads today's rows
            ms = (st if st is not None
                  else status_as_of_yesterday(conn, today, memo))
            rows += [r for k in ("food", "other")
                     for r in ms["buckets"][k]["rows"]
                     if start <= as_date(r["date"]) < today]
        else:
            rows += [r for r in _closed_month_rows(conn, y, m, memo, today)
                     if start <= as_date(r["date"]) < today]
    return rows, start, days


def status_as_of_yesterday(conn, today: dt.date,
                           memo: dict | None) -> dict | None:
    """The running month bucketed as of yesterday (budget.month_status
    of yesterday: rows through yesterday only, an annual envelope's
    earlier claims too — rows_until), once a build; None on the 1st, when
    yesterday's month is closed."""
    if today.day == 1:
        return None
    key = ("forecast.status_as_of_yesterday", today)
    if memo is not None and key in memo:
        return memo[key]
    out = budget.month_status(conn, today - dt.timedelta(days=1), memo=memo,
                              rows_until=today)
    if memo is not None:
        memo[key] = out
    return out


def plan_daily_as_of_yesterday(conn, today: dt.date,
                               memo: dict | None) -> float:
    """The plan's variable budget per day (Food plus Everything else over
    the month's days) as it stood at the end of yesterday: the month of
    yesterday read as of yesterday (status_as_of_yesterday; on the 1st,
    last month's plan). A learned fact like every other the pace reads: a
    dynamic budget scales the plan to what this month's income leaves after
    its bills, so a paycheck landing today would otherwise move it."""
    st_y = status_as_of_yesterday(conn, today, memo)
    if st_y is None:
        key = ("forecast.status_of_last_month", today)
        st_y = (memo or {}).get(key)
        if st_y is None:
            st_y = budget.month_status(conn, today - dt.timedelta(days=1),
                                       memo=memo, rows_until=today)
            if memo is not None:
                memo[key] = st_y
    b = st_y["buckets"]
    return ((b["food"]["month_budget"] + b["other"]["month_budget"])
            / st_y["days_in_month"])


def forecast_pace(conn, today: dt.date, *, st: dict | None = None,
                  memo: dict | None = None) -> dict:
    """The daily pace the forecast walks burn, before the envelope drip —
    the one function that decides it. {daily, basis, since, days,
    one_offs, plan_daily}: `basis` "trailing" (the last
    TRAILING_PACE_DAYS finished days' variable rows less their one-offs,
    over the finished days in the window) or "plan" (under MIN_RATE_DAYS
    finished days of ledger: the plan's daily); `since` the window's first
    day, `days` its finished days; `one_offs` what was left out
    (window_one_offs). Floored at a quarter of the plan's daily: a quiet
    stretch is not a trend.

    Only the forecast reads this. The verdict, the tiles, the hero and
    the month lens keep the month to date (budget.paced_daily)."""
    plan_daily = plan_daily_as_of_yesterday(conn, today, memo)
    # the window's rows as the ledger stood at the end of yesterday: the
    # running month bucketed as of yesterday, so a row dated today (a
    # bill's payment arriving) re-files nothing until tomorrow
    st_y = status_as_of_yesterday(conn, today, memo)
    rows, since, days = trailing_variable_rows(conn, today, st=st_y,
                                               memo=memo)
    if days < budget.MIN_RATE_DAYS:
        return {"daily": max(plan_daily, plan_daily * 0.25), "basis": "plan",
                "since": since, "days": days, "one_offs": [],
                "plan_daily": plan_daily, "rows": rows}
    parts = window_one_offs(conn, rows, since, days, with_parts=True)
    one_offs = [{k: v for k, v in o.items() if k != "txn_ids"}
                for o in parts]
    total = sum(r["amount"] for r in rows) - sum(o["amount"]
                                                 for o in one_offs)
    return {"daily": max(total / days, plan_daily * 0.25),
            "basis": "trailing", "since": since, "days": days,
            "one_offs": one_offs, "plan_daily": plan_daily, "rows": rows,
            "shares": pace_shares(conn, rows, parts, memo=memo)}


# A heavier month (specs/today-figures.md, "A heavier month is drawn beside
# the expected one"): the ratio the household's own 30-day spending
# (HEAVY_WINDOW_DAYS) ran above the pace it had going in, exceeded in about
# one window in five (HEAVY_PERCENTILE) over the last HEAVY_LOOKBACK_DAYS;
# held in HEAVY_CLAMP, HEAVY_DEFAULT on under HEAVY_MIN_HISTORY_DAYS of
# ledger. The interim band: a later change replaces it with one drawn from
# the forecast's own graded errors.
HEAVY_LOOKBACK_DAYS = 365
HEAVY_WINDOW_DAYS = 30
HEAVY_PERCENTILE = 0.80
HEAVY_MIN_HISTORY_DAYS = 180
HEAVY_CLAMP = (1.05, 2.00)
HEAVY_DEFAULT = 1.25


def heavy_month_factor(conn, today: dt.date, *, st: dict | None = None,
                       memo: dict | None = None,
                       paid: dict | None = None) -> dict:
    """How much heavier than its usual pace a heavier stretch runs for this
    household — the one function the heavier-month walks read: {factor,
    measured (windows counted), basis "household" | "default"}. For each
    day d of the last HEAVY_LOOKBACK_DAYS with HEAVY_WINDOW_DAYS of ledger
    after it and TRAILING_PACE_DAYS before it: all the variable spending
    of the 30 days from d (one-offs included: the heavier month is the
    month with the repair in it) over the forecast's pace as of d — the
    same variable rows (variable_rows_since: the months' buckets, the
    running one as of yesterday) and the same one-off rule
    (window_one_offs) forecast_pace reads, floored as it is. The factor is
    the HEAVY_PERCENTILE of those ratios, so the 30 days after a day ran
    above that day's pace x factor in one stretch in five, by
    construction; held in HEAVY_CLAMP. HEAVY_DEFAULT with under
    HEAVY_MIN_HISTORY_DAYS of ledger. `paid` is unused (kept for the
    callers' one read of bill payments)."""
    key = ("forecast.heavy_month_factor", today)
    if memo is not None and key in memo:
        return memo[key]
    plan_daily = plan_daily_as_of_yesterday(conn, today, memo)
    st_y = status_as_of_yesterday(conn, today, memo)
    rows, since, _days = variable_rows_since(
        conn, today,
        today - dt.timedelta(days=HEAVY_LOOKBACK_DAYS + TRAILING_PACE_DAYS),
        st=st_y, memo=memo)
    ratios = heavy_ratios(conn, rows, since, today, plan_daily)
    cfg = budget.load_config(conn, memo=memo)
    first = budget.ledger_start(conn, budget.excluded_account_ids(cfg))
    if (first is None or (today - first).days < HEAVY_MIN_HISTORY_DAYS
            or not ratios):
        out = {"factor": HEAVY_DEFAULT, "measured": len(ratios),
               "basis": "default"}
    else:
        out = {"factor": min(max(_percentile(ratios, HEAVY_PERCENTILE),
                                 HEAVY_CLAMP[0]), HEAVY_CLAMP[1]),
               "measured": len(ratios), "basis": "household"}
    if memo is not None:
        memo[key] = out
    return out


def _percentile(vals: list[float], q: float) -> float:
    vals = sorted(vals)
    k = q * (len(vals) - 1)
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def habit_lookup(conn, since: dt.date, until: dt.date):
    """window_one_offs' habit question answered from one read instead of
    one per merchant per window: every live charge dated [since, until),
    by its merchant and by its raw descriptor key — the same rows the
    one-off rule's own statement counts."""
    from . import merchant_sql
    by: dict[str, list[tuple[dt.date, int, str]]] = {}
    for r in conn.execute(
            f"""SELECT id, date, ROUND(amount * 100) AS c,
                      LOWER(BTRIM({merchant_sql.RAW_KEY_UNALIASED})) AS k,
                      merchant_id::text AS mid
                 FROM transactions
                WHERE removed = 0 AND amount > 0
                  AND date >= %s AND date < %s
                ORDER BY date, id""", (since, until)).fetchall():
        row = (as_date(r["date"]), int(r["c"]), r["id"])
        by.setdefault("k:" + (r["k"] or ""), []).append(row)
        if r["mid"]:
            by.setdefault("m:" + r["mid"], []).append(row)

    def habit_of(lead: dict, on: dt.date, start: dt.date, total: int):
        seen = {}
        for key in ("k:" + (lead.get("payee") or "").strip().lower(),
                    "m:" + str(lead.get("merchant_id") or "")):
            for d, c, tid in by.get(key, []):
                if (on - dt.timedelta(days=ONE_OFF_HABIT_DAYS) <= d < start
                        and c * 3 >= total):
                    seen[tid] = d
        half = sum(1 for d in seen.values()
                   if d >= on - dt.timedelta(days=ONE_OFF_LOOKBACK_DAYS))
        return half, len(seen)
    return habit_of


def heavy_ratios(conn, rows: list[dict], since: dt.date, today: dt.date,
                 plan_daily: float) -> list[float]:
    """heavy_month_factor's ratios: for each as-of day d of the last
    HEAVY_LOOKBACK_DAYS with HEAVY_WINDOW_DAYS after it and
    TRAILING_PACE_DAYS before it inside [since, today), all the variable
    spending of the HEAVY_WINDOW_DAYS from d per day, over the forecast's
    pace as of d: its 60 days' rows less their one-offs, per day, floored
    at a quarter of the plan's daily (forecast_pace's own rule). The
    one-offs of each window are window_one_offs' decision, taken from
    day totals and per-merchant prefix sums (_WindowOneOffs) so a year of
    windows is one pass; tested equal to window_one_offs window by
    window, ties included. Sums in whole cents."""
    n, w = TRAILING_PACE_DAYS, HEAVY_WINDOW_DAYS
    a = max(since + dt.timedelta(days=n),
            today - dt.timedelta(days=HEAVY_LOOKBACK_DAYS))
    last = today - dt.timedelta(days=w)
    if a > last:
        return []
    habit_of = (habit_lookup(conn, a - dt.timedelta(
        days=n + ONE_OFF_HABIT_DAYS), last) if conn is not None
        else (lambda *x: (0, 0)))
    one_offs = _WindowOneOffs(rows, a - dt.timedelta(days=n), today,
                              habit_of)
    ratios: list[float] = []
    d = a
    while d <= last:
        start = d - dt.timedelta(days=n)
        past = one_offs.total(start, d) - one_offs.cents(start, n)
        pace = max(past / 100 / n, plan_daily * 0.25)
        if pace > 0:
            ratios.append((one_offs.total(d, d + dt.timedelta(days=w))
                           / 100 / w) / pace)
        d += dt.timedelta(days=1)
    return ratios


class _WindowOneOffs:
    """window_one_offs' decision for many windows over one set of rows:
    the window's median day from day totals, a merchant's total from its
    prefix sums, and only the merchants with a charge large enough to
    lead a one-off (half the lowest floor) judged at all."""

    def __init__(self, rows: list[dict], base: dt.date, end: dt.date,
                 habit_of):
        import bisect
        self.bisect = bisect
        self.base = base
        span = (end - base).days + 1
        self.day = [0] * span
        whole: dict[str, dict] = {}
        for r in rows:
            i = (as_date(r["date"]) - base).days
            if not 0 <= i < span:
                continue
            c = _cents(r["amount"])
            self.day[i] += c
            if r.get("txn_id") is None:
                continue
            ch = whole.setdefault(r["txn_id"], dict(r, cents=0, i=i))
            ch["cents"] += c
        self.pre = [0]
        for v in self.day:
            self.pre.append(self.pre[-1] + v)
        self.by_key: dict[str, list[dict]] = {}
        for ch in whole.values():
            key = str(ch.get("merchant_id")
                      or "raw:" + (ch["payee"] or "").strip().lower())
            self.by_key.setdefault(key, []).append(ch)
        self.key_pre: dict[str, tuple[list[int], list[int]]] = {}
        for key, chs in self.by_key.items():
            chs.sort(key=lambda c: c["i"])
            idx = [c["i"] for c in chs]
            pre = [0]
            for c in chs:
                pre.append(pre[-1] + c["cents"])
            self.key_pre[key] = (idx, pre)
        lead_min = _cents(ONE_OFF_MIN) // 2
        self.big = sorted(((c["i"], key, c) for key, chs in
                           self.by_key.items() for c in chs
                           if c["cents"] >= lead_min), key=lambda b: b[0])
        self.big_i = [b[0] for b in self.big]
        self.habit_of = habit_of

    def total(self, lo: dt.date, hi: dt.date) -> int:
        a = max(0, (lo - self.base).days)
        b = max(0, min(len(self.day), (hi - self.base).days))
        return self.pre[b] - self.pre[a]

    def cents(self, start: dt.date, days: int) -> int:
        """The cents window_one_offs leaves out of [start, start + days)."""
        a = (start - self.base).days
        b = a + days
        median = statistics.median(self.day[a:b])
        floor = max(_cents(ONE_OFF_MIN), ONE_OFF_FACTOR * median)
        bs = self.bisect
        keys = {k for _i, k, _c in
                self.big[bs.bisect_left(self.big_i, a):
                         bs.bisect_left(self.big_i, b)]}
        out = 0
        for key in keys:
            idx, _pre = self.key_pre[key]
            lo, hi = bs.bisect_left(idx, a), bs.bisect_left(idx, b)
            chs = self.by_key[key][lo:hi]
            lead, part = one_off_purchase(chs)
            total = sum(c["cents"] for c in part)
            if total < floor or lead["cents"] * 2 < total:
                continue
            half, year = self.habit_of(lead, as_date(lead["date"]), start,
                                       total)
            if half >= 1 or year >= 2:
                continue
            out += total
        return out


SLOW_TO_STEPS = 14


def slow_to_rate(low_at, pace: float) -> dict:
    """The largest daily variable spending at which a walk stays at or
    above $0 through its horizon — decided against the walk itself
    (`low_at(rate)` is the walk's lowest balance at that rate), so card
    timing, bills and paydays are all in it: {rate, scheduled}. A
    bisection over [0, pace] of SLOW_TO_STEPS steps, floored to whole
    dollars. When even $0 a day dips (`scheduled`), the dip comes from
    what is scheduled, not from spending: rate is None, and no spending
    advice is given. A rate under $1 is no advice either."""
    if low_at(0.0) < 0:
        return {"rate": None, "scheduled": True}
    lo, hi = 0.0, max(pace, 0.0)
    for _ in range(SLOW_TO_STEPS):
        mid = (lo + hi) / 2
        if low_at(mid) >= 0:
            lo = mid
        else:
            hi = mid
    rate = float(int(lo))
    return {"rate": rate if rate >= 1 else None, "scheduled": rate < 1}


def walked_card_ids(conn, excluded: list) -> list[str]:
    """Every credit account the cash walk pays (CARD_SCOPE_SQL)."""
    return [r["id"] for r in conn.execute(
        f"""SELECT a.id FROM accounts a
           WHERE a.type = 'credit' {CARD_SCOPE_SQL} ORDER BY a.id""",
        (excluded,)).fetchall()]


def pace_shares(conn, rows: list[dict], one_offs: list[dict], *,
                memo: dict | None = None) -> dict[str, float]:
    """Where the pace's spending was charged: {"checking": share,
    <card id>: share, ...}, summing to 1, from the window's rows less its
    one-offs' charges (forecast_pace). A row on a walked card (CARD_SCOPE_SQL) is
    that card's; every other row — checking, another depository account,
    an account the walk does not pay — is "checking", whose share leaves
    checking the day it is spent. An envelope's overflow slice (no charge
    of its own) is spread pro rata. No rows: all "checking"."""
    cfg = budget.load_config(conn, memo=memo)
    cards = set(walked_card_ids(conn, budget.excluded_account_ids(cfg)))
    # a one-off's charges (window_one_offs with_parts) are left out; the
    # merchant's everyday charges stay
    out_ids = {str(t) for o in one_offs
               for t in (o.get("txn_ids") or [o["txn_id"]])}
    keyed = [r for r in rows if r.get("txn_id") is not None]
    ids = sorted({str(r["txn_id"]) for r in keyed})
    acct = ({r["id"]: r["account_id"] for r in conn.execute(
        "SELECT id, account_id FROM transactions WHERE id = ANY(%s) "
        "ORDER BY id", (ids,)).fetchall()} if ids else {})
    by: dict[str, float] = {}
    for r in keyed:
        if str(r["txn_id"]) in out_ids:
            continue
        a = acct.get(str(r["txn_id"]))
        k = a if a in cards else "checking"
        by[k] = by.get(k, 0.0) + r["amount"]
    total = sum(v for v in by.values() if v > 0)
    if total <= 0:
        return {"checking": 1.0}
    shares = {k: v / total for k, v in by.items() if v > 0}
    shares.setdefault("checking", 0.0)
    return shares


# Which account pays a scheduled bill (specs/today-figures.md, "Card
# spending leaves checking when the household pays the card"): the account
# that paid the majority of the bill's last three matched payments in the
# last PAYING_ACCOUNT_LOOKBACK_DAYS (a tie: the most recent).
PAYING_ACCOUNT_LOOKBACK_DAYS = 400
PAYING_ACCOUNT_LAST = 3


CHECKING = "checking"


def bill_paying_accounts(conn, cfg: dict, today: dt.date, bills_: list,
                         *, memo: dict | None = None, raw: bool = False,
                         paid: dict | None = None
                         ) -> dict[str, str | None]:
    """{payee: card id | None} for scheduled bills and envelopes (the
    dicts budget._recurring_bills / _envelope_bills return): the walked
    card (CARD_SCOPE_SQL) that paid the majority of the bill's last
    PAYING_ACCOUNT_LAST matched payments within PAYING_ACCOUNT_LOOKBACK_DAYS
    before today — a tie goes to the most recent. A payment matches by the
    bill's own matcher (its merchant identity, else its words) and, for an
    occurrence bill, inside its amount bands. None — walked from checking,
    as before — when the bill has no matched payment in the window, or the
    account that paid it is not a card the walk pays (checking or another
    depository account, an excluded, business, shadow or closed card).
    `raw`: CHECKING for a bill paid from such an account, None only for
    no payment found (an envelope with none is split like the pace).
    """
    if not bills_:
        return {}
    excluded = budget.excluded_account_ids(cfg)
    cards = set(walked_card_ids(conn, excluded))
    key = ("forecast.bill_paying_accounts", today, raw,
           tuple(sorted(str(b["payee"]) for b in bills_)),
           tuple(sorted(cards)))
    if memo is not None and key in memo:
        return dict(memo[key])
    # `paid`: the bills' payments already read (bill_payments, a longer
    # look-back is cut to this one)
    since = today - dt.timedelta(days=PAYING_ACCOUNT_LOOKBACK_DAYS)
    paid = ({k: [x for x in v if x[0] >= since] for k, v in paid.items()}
            if paid is not None else
            bill_payments(conn, today, bills_, PAYING_ACCOUNT_LOOKBACK_DAYS,
                          memo=memo))
    out: dict[str, str | None] = {}
    for b in bills_:
        found = [a for _d, _amt, a in paid.get(b["payee"], [])
                 ][:PAYING_ACCOUNT_LAST]
        if not found:
            out[b["payee"]] = None
            continue
        counts: dict[str, int] = {}
        for a in found:
            counts[a] = counts.get(a, 0) + 1
        top = max(counts.values())
        # newest first: the first account with the top count is the most
        # recent of a tie
        pick = next(a for a in found if counts[a] == top)
        out[b["payee"]] = pick if pick in cards else (
            CHECKING if raw else None)
    if memo is not None:
        memo[key] = dict(out)
    return out


def bill_payments(conn, today: dt.date, bills_: list, lookback_days: int,
                  *, memo: dict | None = None) -> dict[str, list[tuple]]:
    """Each bill's matched payments in the `lookback_days` before today,
    newest first: {payee: [(date, amount, account id)]}. A payment matches
    by the bill's own matcher (its merchant identity, else its words) and,
    for an occurrence bill, inside its amount bands; an envelope (no
    bands) by its matcher alone."""
    key = ("forecast.bill_payments", today, lookback_days,
           tuple(sorted(str(b["payee"]) for b in bills_)))
    if memo is not None and key in memo:
        return memo[key]
    since = today - dt.timedelta(days=lookback_days)
    rows = conn.execute(
        f"""SELECT t.id, t.date, t.amount, t.account_id,
                  t.merchant_id::text AS merchant_id,
                  {budget.MATCH_PAYEE_SQL} AS mpayee, t.name,
                  COALESCE(t.category_override, t.category_primary, '')
                      AS category
             FROM transactions t
            WHERE t.removed = 0 AND t.amount > 0
              AND t.date >= %s AND t.date < %s
            ORDER BY t.date DESC, t.id DESC""", (since, today)).fetchall()
    # each row's words once, and an index of the rows by merchant and by
    # word: a bill with an identity reads only its merchants' rows, a bill
    # matched by words only the rows carrying one of its words; a phrase
    # or an alternatives matcher reads them all, as before
    texts: list[tuple[str, set]] = []
    by_mid: dict[str, list[int]] = {}
    by_tok: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        text = budget.match_text(r["mpayee"], r["name"])
        tk = budget._tokens(text)
        texts.append((text, tk))
        if r["merchant_id"]:
            by_mid.setdefault(str(r["merchant_id"]), []).append(i)
        for t in tk:
            by_tok.setdefault(t, []).append(i)
    import bisect
    by_amt = sorted(range(len(rows)), key=lambda i: float(rows[i]["amount"]))
    amts = [float(rows[i]["amount"]) for i in by_amt]
    out: dict[str, list[tuple]] = {}
    claimed: set[int] = set()
    for b in bills_:
        bands = b.get("bands") if "bands" in b else None
        ids = b.get("ids")
        if ids is not None:
            cand = sorted({i for m in ids for i in by_mid.get(str(m), [])})
        elif bands is not None:
            # only the rows inside one of its amount bands can be its
            cand = sorted({by_amt[j] for ref, tol, _u in bands
                           for j in range(bisect.bisect_left(amts, ref - tol),
                                          bisect.bisect_right(amts,
                                                              ref + tol))})
        elif (b.get("matcher") is None and not b.get("merchant_phrase")
              and budget._match_tokens(b)):
            # a token-subset matcher: every match carries each of its words
            cand = sorted(min((by_tok.get(t, []) for t in
                               budget._match_tokens(b)), key=len))
        else:
            cand = range(len(rows))
        got: list[tuple] = []
        for i in cand:
            r = rows[i]
            amt = float(r["amount"])
            on = as_date(r["date"])
            if bands is not None and not budget._in_bands(bands, amt, on):
                continue
            text, tk = texts[i]
            if budget._bill_matches(b, tk, text, r["merchant_id"]):
                got.append((on, amt, r["account_id"]))
                claimed.add(i)
        out[b["payee"]] = got
    _category_claims(rows, bills_, out, claimed)
    if memo is not None:
        memo[key] = out
    return out


def _category_claims(rows: list, bills_: list, out: dict,
                     claimed: set[int]) -> None:
    """A category pool's payments (an envelope with match_cat), as the
    month's own claimer (budget._claim_envelope) gives them: the rows of
    its category that no bill or envelope claims by its merchant or words,
    the first such pool in list order taking each. Added to `out` newest
    first, so its paying account is read from where its rows were paid."""
    cat_pools = [b for b in bills_ if b.get("match_cat")]
    if not cat_pools:
        return
    for i, r in enumerate(rows):
        if i in claimed:
            continue
        cat = budget._cat_norm(r["category"]) if r["category"] else ""
        if not cat:
            continue
        pool = next((b for b in cat_pools if b["match_cat"] == cat), None)
        if pool is None:
            continue
        out[pool["payee"]].append(
            (as_date(r["date"]), float(r["amount"]), r["account_id"]))
    for b in cat_pools:
        out[b["payee"]].sort(key=lambda x: x[0], reverse=True)


# A seasonal bill (specs/today-figures.md, "A seasonal bill is estimated
# from last year"): a monthly bill whose payments cover
# SEASONAL_MIN_COVERED of the last SEASONAL_COVER_MONTHS months and vary
# (coefficient of variation of the last 12 at least SEASONAL_MIN_CV).
SEASONAL_COVER_MONTHS = 16
SEASONAL_MIN_COVERED = 15
SEASONAL_MIN_CV = 0.08
SEASONAL_RATIO_CLAMP = (0.80, 1.25)
SEASONAL_AMOUNT_CLAMP = (0.5, 2.0)
SEASONAL_LOOKBACK_DAYS = 31 * (SEASONAL_COVER_MONTHS + 1)
# one read of every bill's payments serves every reader: the longest
# look-back any of them needs (the seasonal months, the heavier year)
PAYMENTS_LOOKBACK_DAYS = max(SEASONAL_LOOKBACK_DAYS, 365 + 60 + 1)


def _month_back(y: int, m: int, k: int) -> tuple[int, int]:
    y2, m2 = divmod(m - 1 - k, 12)
    return y + y2, m2 + 1


def occurrence_amount(bill: dict, history: list[tuple], due: dt.date,
                      today: dt.date, *, hand_set: bool = False
                      ) -> tuple[float, bool]:
    """The amount the forecast walks for one occurrence of a spend bill
    due `due` — the one function. (amount, estimated): the bill's amount,
    unless the bill is monthly (one date a month), not set by hand inside
    its grace (`hand_set`), its matched payments (`history`, bill_payments)
    cover SEASONAL_MIN_COVERED of the SEASONAL_COVER_MONTHS months before
    today's — the occurrence's month a year ago, the three months before
    today's and the same three a year earlier among them — and the last
    12 payments vary (coefficient of variation at least SEASONAL_MIN_CV).
    Then: last year's payment for the occurrence's month times r, r = the
    last three months' payments over the same three months a year earlier,
    held in SEASONAL_RATIO_CLAMP; the estimate held in
    SEASONAL_AMOUNT_CLAMP times the bill's amount; to the cent."""
    amount = float(bill["amount"])
    rec = bill.get("recurrence") or {}
    if (hand_set or str(rec.get("frequency") or "").upper() != "MONTHLY"
            or int(rec.get("interval") or 1) != 1
            or len(rec.get("byMonthDay") or [None]) > 1):
        return amount, False
    by_month: dict[tuple[int, int], float] = {}
    for d0, amt, *_ in history:
        k = (d0.year, d0.month)
        by_month[k] = by_month.get(k, 0.0) + amt
    months = [_month_back(today.year, today.month, k)
              for k in range(1, SEASONAL_COVER_MONTHS + 1)]
    if sum(1 for k in months if k in by_month) < SEASONAL_MIN_COVERED:
        return amount, False
    trail = months[:3]
    twins = [(y - 1, m) for y, m in trail]
    target = (due.year - 1, due.month)
    if any(k not in by_month for k in trail + twins + [target]):
        return amount, False
    last12 = [a for _d, a, *_ in history[:12]]
    mean = statistics.fmean(last12) if last12 else 0.0
    if (len(last12) < 12 or mean <= 0
            or statistics.pstdev(last12) / mean < SEASONAL_MIN_CV):
        return amount, False
    before = sum(by_month[k] for k in twins)
    if before <= 0:
        return amount, False
    r = min(max(sum(by_month[k] for k in trail) / before,
                SEASONAL_RATIO_CLAMP[0]), SEASONAL_RATIO_CLAMP[1])
    est = min(max(by_month[target] * r, amount * SEASONAL_AMOUNT_CLAMP[0]),
              amount * SEASONAL_AMOUNT_CLAMP[1])
    return round(est, 2), True


# When a card's charges leave checking (specs/today-figures.md, "Card
# spending leaves checking when the household pays the card").
CARD_LAG_DAYS = 90
CARD_LAG_MIN_HISTORY_DAYS = 60
CARD_LAG_MIN_PAYMENTS = 2
CARD_LAG_MIN_CHARGES = 300.0
# a due date with no statement date: the statement closed this many days
# before it
CARD_GRACE_DAYS = 25
# a card whose lag is within this many days of a due-date payer's is paid
# at its statements' due dates
CARD_STATEMENT_SLACK_DAYS = 7


def _add_months(d: dt.date, k: int) -> dt.date:
    """`d` plus `k` months, the day clamped to the month's length."""
    y, m = divmod(d.month - 1 + k, 12)
    y += d.year
    m += 1
    return dt.date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def statement_of(timing: dict, d: dt.date) -> tuple[dt.date, dt.date] | None:
    """(closing day, due day) of the statement a charge dated `d` is on:
    the first statement closing on or after `d`, statement k closing on
    the last statement date plus k months and due on the next due date
    plus k months. None without a cycle."""
    issued, due = timing.get("issued"), timing.get("due")
    if issued is None or due is None:
        return None
    return _statement_of(issued, due, d)


@functools.lru_cache(maxsize=65536)
def _statement_of(issued: dt.date, due: dt.date,
                  d: dt.date) -> tuple[dt.date, dt.date]:
    """statement_of on its inputs (a walk asks it once a charge, for the
    same few cycles and days)."""
    # a first guess from the months between, then step to the first close
    k = (d.year - issued.year) * 12 + d.month - issued.month
    while _add_months(issued, k) < d:
        k += 1
    while _add_months(issued, k - 1) >= d:
        k -= 1
    return _add_months(issued, k), _add_months(due, k)


def _card_lag(conn, card: dict, today: dt.date,
              payments: list[dict]) -> float | None:
    """How long a dollar charged to the card stays on it: its mean posted
    balance over the last CARD_LAG_DAYS days over its mean daily charges.
    None under CARD_LAG_MIN_HISTORY_DAYS of the card's rows, under
    CARD_LAG_MIN_PAYMENTS payments or CARD_LAG_MIN_CHARGES charged in the
    window."""
    since = today - dt.timedelta(days=CARD_LAG_DAYS)
    first = conn.execute(
        "SELECT MIN(date) AS d FROM transactions WHERE removed = 0 "
        "AND account_id = %s", (card["id"],)).fetchone()
    if (not first or not first["d"]
            or as_date(first["d"]) > today - dt.timedelta(
                days=CARD_LAG_MIN_HISTORY_DAYS)):
        return None
    if sum(1 for p in payments if p["account_id"] == card["id"]
           and as_date(p["date"]) >= since) < CARD_LAG_MIN_PAYMENTS:
        return None
    rows = conn.execute(
        "SELECT date, amount FROM transactions WHERE removed = 0 "
        "AND COALESCE(pending, 0) = 0 AND account_id = %s AND date >= %s "
        "AND date < %s ORDER BY date, id", (card["id"], since, today)
    ).fetchall()
    charged = sum(float(r["amount"]) for r in rows
                  if float(r["amount"]) > 0 and as_date(r["date"]) < today)
    if charged < CARD_LAG_MIN_CHARGES:
        return None
    after: dict[dt.date, float] = {}
    for r in rows:
        after[as_date(r["date"])] = after.get(as_date(r["date"]), 0.0) + float(
            r["amount"])
    # the posted balance at the end of each day of the window: today's
    # less every posted row dated after that day
    bal = card["posted"] - sum(v for d0, v in after.items() if d0 >= today)
    total = 0.0
    d = today - dt.timedelta(days=1)
    while d >= since:
        total += max(bal, 0.0)
        bal -= after.get(d, 0.0)
        d -= dt.timedelta(days=1)
    return round(total / charged, 1)


# A reported due date more than a cycle after the last statement date
# belongs to the statement still open: once a due date passes, an issuer
# reports the next cycle's while the last statement date is the old one.
CARD_CYCLE_MAX_DAYS = 35


def card_cycle(issued: dt.date | None, due: dt.date | None
               ) -> tuple[dt.date | None, dt.date | None, dt.date | None]:
    """(last statement date, its own due date, the due date the issuer
    reports) — the one place a statement is paired with its due date. A
    due date with no statement date: the statement closed CARD_GRACE_DAYS
    before it. A reported due date more than CARD_CYCLE_MAX_DAYS after the
    last statement date is the OPEN statement's: the closed one's was a
    month earlier (and has passed). No due date: no cycle."""
    if due is None:
        return None, None, None
    if issued is None or issued >= due:
        return due - dt.timedelta(days=CARD_GRACE_DAYS), due, due
    if (due - issued).days > CARD_CYCLE_MAX_DAYS:
        return issued, _add_months(due, -1), due
    return issued, due, due


# How each card is paid (specs/today-figures.md, "Each card is paid its own
# way"): statement autopay when AUTOPAY_MIN_OF of its last AUTOPAY_STATEMENTS
# closed statements were paid by credits within AUTOPAY_DAYS of their due
# date adding up to the balance at close (within max($1, AUTOPAY_TOLERANCE));
# paid in rounds after payday when PAYDAY_HABIT_SHARE of its own payment
# dollars over PAYDAY_HABIT_MONTHS came within PAYDAY_HABIT_DAYS after a
# household payday; lag when its lag is at most CARD_LAG_MAX_DAYS and its
# payments cover CARD_PAYDOWN_SHARE of its charges; a carried balance when
# the lag says it is paid down more slowly than that.
AUTOPAY_STATEMENTS = 3
AUTOPAY_MIN_OF = 2
AUTOPAY_DAYS = 3
AUTOPAY_TOLERANCE = 0.01
CARD_LAG_MAX_DAYS = 45
CARD_PAYDOWN_SHARE = 0.80
CARRIED_PAYMENT_MONTHS = 6
# a round is paid once this share of the household's usual gap between
# rounds has passed since the last one
ROUND_GAP_SHARE = 0.8
# a card's own gap is read from at least this many of its payments
ROUND_GAP_MIN_PAYMENTS = 4


def autopaid_statements(conn, card: dict, issued: dt.date, due: dt.date,
                        today: dt.date, credits: list[dict]
                        ) -> tuple[int, int, int]:
    """(autopaid, judged, paid in full) over the card's last
    AUTOPAY_STATEMENTS closed
    statements whose due date has passed: a statement counts as autopaid
    when the card's payment credits within AUTOPAY_DAYS of its due date add
    up to its balance at close (the posted balance less every posted row
    after the close) within max($1, AUTOPAY_TOLERANCE of it); paid in full
    when the card's payments between the close and AUTOPAY_DAYS after the
    due date reach it, within AUTOPAY_TOLERANCE. Past due dates step by
    month from the paired one (card_cycle); an issuer that dates the due a
    fixed grace after the close drifts by up to three days across a short
    month, which AUTOPAY_DAYS holds. A statement with no balance is not
    judged."""
    rows = conn.execute(
        "SELECT date, amount FROM transactions WHERE removed = 0 "
        "AND COALESCE(pending, 0) = 0 AND account_id = %s AND date > %s "
        "AND date < %s ORDER BY date, id",
        (card["id"], _add_months(issued, -AUTOPAY_STATEMENTS - 1), today)
    ).fetchall()
    mine = [(as_date(c["date"]), float(c["amount"])) for c in credits
            if c["account_id"] == card["id"]]
    paid = judged = full = 0
    k = 0
    while judged < AUTOPAY_STATEMENTS and k > -(AUTOPAY_STATEMENTS + 2):
        close, due_k = _add_months(issued, k), _add_months(due, k)
        k -= 1
        if due_k + dt.timedelta(days=AUTOPAY_DAYS) >= today:
            continue
        bal = card["posted"] - sum(float(r["amount"]) for r in rows
                                   if as_date(r["date"]) > close)
        if bal <= 1.0:
            continue
        judged += 1
        got = sum(a for d, a in mine
                  if abs((d - due_k).days) <= AUTOPAY_DAYS)
        if abs(got - bal) <= max(1.0, AUTOPAY_TOLERANCE * bal):
            paid += 1
        # paid in full by its due date, by whatever payments
        by_due = sum(a for d, a in mine
                     if close < d <= due_k + dt.timedelta(days=AUTOPAY_DAYS))
        if by_due >= bal * (1 - AUTOPAY_TOLERANCE) - 1.0:
            full += 1
    return paid, judged, full


def card_timing(conn, cards: list[dict], today: dt.date, *,
                habit: dict | None = None) -> dict[str, dict]:
    """Each walked card's timing — {card id: {name, issued, due, lag,
    mode, stmt_lag, ...}} — the facts card_leaves_checking reads, and how
    the household pays the card (`mode`), decided per card:
    - "autopay": at least AUTOPAY_MIN_OF of its last closed statements
      were paid at their due date in full (autopaid_statements); paid at
      each statement's due date;
    - "rounds": the household pays after payday (`habit`, a paycheck
      series) and PAYDAY_HABIT_SHARE of this card's own payment dollars
      over PAYDAY_HABIT_MONTHS came within PAYDAY_HABIT_DAYS after a
      payday, over PAYDAY_HABIT_MIN_PAYMENTS payments;
    - "lag": a measured lag (_card_lag) of at most CARD_LAG_MAX_DAYS,
      shorter than a due-date payer's by more than CARD_STATEMENT_SLACK_DAYS,
      with payments covering CARD_PAYDOWN_SHARE of its charges;
    - "carried": a measured lag, but a longer one or payments short of
      that share — the balance is carried, and the walk pays what the
      household has been paying (`payment`, `payment_day`);
    - "statement": a cycle and no other evidence; "charge": neither.
    The cycle is card_cycle's. The decision is a function of the ledger
    and the day alone: no memory of an earlier day's mode (two workers, a
    restart and a rebuild after a write decide alike)."""
    from . import reporting
    since = today - dt.timedelta(days=31 * CARRIED_PAYMENT_MONTHS)
    # the ledger as it stood at the end of yesterday: credits through
    # yesterday, balances (`cards[*].posted`) as of then
    yesterday = today - dt.timedelta(days=1)
    credits = [p for p in reporting.card_credit_inflows(conn, since,
                                                        yesterday)
               if not p["pending"]]
    payments = [p for p in credits
                if as_date(p["date"]) >= today - dt.timedelta(
                    days=CARD_LAG_DAYS)]
    paydays = (habit or {}).get("paydays") or []
    # a card with nothing owed and no row in the window (a closed or idle
    # card) has nothing to learn from: no per-card reads for it
    active = {r["account_id"] for r in conn.execute(
        "SELECT DISTINCT account_id FROM transactions WHERE removed = 0 "
        "AND account_id = ANY(%s) AND date >= %s AND date < %s "
        "ORDER BY account_id",
        ([c["id"] for c in cards], since, today)).fetchall()} if cards else set()
    out: dict[str, dict] = {}
    for c in cards:
        try:
            due = as_date(c["due"]) if c.get("due") else None
        except ValueError:
            due = None
        issued, due, _open_due = card_cycle(c.get("issued"), due)
        if c["id"] not in active and c["debt"] <= 0:
            out[c["id"]] = {"name": c["name"],
                            "issued": issued if due else None, "due": due,
                            "lag": None,
                            "mode": "statement" if due else "charge",
                            "stmt_lag": ((due - issued).days + 15)
                            if due is not None else None,
                            "autopaid": 0, "judged": 0, "payday_share": 0.0,
                            "payment": None, "payment_day": None,
                            "gap": None, "last_paid": None}
            continue
        lag = _card_lag(conn, c, today, payments)
        stmt_lag = ((due - issued).days + 15) if due is not None else None
        mine = [p for p in credits if p["account_id"] == c["id"]]
        autopaid, judged, in_full = (
            autopaid_statements(conn, c, issued, due, today, credits)
            if due is not None else (0, 0, 0))
        # every one of its last statements paid in full by its due date:
        # whatever the lag says, the balance is not carried
        paid_down = judged >= AUTOPAY_STATEMENTS and in_full >= judged
        near = sum(float(p["amount"]) for p in mine
                   if any(0 <= (as_date(p["date"]) - d).days
                          <= PAYDAY_HABIT_DAYS for d in paydays))
        total = sum(float(p["amount"]) for p in mine)
        charged90, paid90 = _card_flows(conn, c["id"], today)
        if due is not None and judged >= AUTOPAY_MIN_OF and (
                autopaid >= AUTOPAY_MIN_OF):
            mode = "autopay"
        elif (paydays and len(mine) >= PAYDAY_HABIT_MIN_PAYMENTS
              and total > 0 and near / total >= PAYDAY_HABIT_SHARE):
            mode = "rounds"
        elif lag is not None and not paid_down and (
                lag > CARD_LAG_MAX_DAYS
                or paid90 < CARD_PAYDOWN_SHARE * charged90):
            mode = "carried"
        elif lag is not None and (
                stmt_lag is None
                or lag < stmt_lag - CARD_STATEMENT_SLACK_DAYS):
            mode = "lag"
        elif due is not None:
            mode = "statement"
        else:
            mode = "charge"
        # the card's own spacing of payments (its rounds) and its last
        paid_days = sorted({as_date(p["date"]) for p in mine})
        # read from what the card does now when that changed (habit_gap)
        card_gap = habit_gap(paid_days, today, ROUND_GAP_MIN_PAYMENTS - 1)
        pay_amt = pay_day = None
        if mode == "carried":
            by_month: dict[tuple, float] = {}
            days_: list[int] = []
            for p in mine:
                d = as_date(p["date"])
                by_month[(d.year, d.month)] = by_month.get(
                    (d.year, d.month), 0.0) + float(p["amount"])
                days_.append(d.day)
            months = [by_month.get(_month_back(today.year, today.month, k),
                                   0.0) for k in range(1, 7)]
            pay_amt = round(max(statistics.median(months),
                                float(c.get("minimum") or 0.0)), 2)
            pay_day = int(statistics.median(days_)) if days_ else (
                due.day if due else today.day)
        out[c["id"]] = {"name": c["name"], "issued": issued if due else None,
                        "due": due, "lag": lag, "mode": mode,
                        "stmt_lag": stmt_lag, "autopaid": autopaid,
                        "judged": judged,
                        "payday_share": (round(near / total, 3)
                                         if total > 0 else 0.0),
                        "payment": pay_amt, "payment_day": pay_day,
                        "gap": card_gap,
                        "last_paid": paid_days[-1] if paid_days else None}
    return out


# a payday habit is read from what the household does now: the last
# ROUND_RECENT_DAYS when they hold ROUND_RECENT_MIN rounds, else the last
# ROUND_RECENT_WIDE_DAYS, else everything learned
ROUND_RECENT_DAYS = 120
ROUND_RECENT_WIDE_DAYS = 180
ROUND_RECENT_MIN = 3


def recent_rounds(days: list[dt.date], today: dt.date) -> list[dt.date]:
    """The round days a habit is read from (ROUND_RECENT_*): the ones of
    the last 120 days when there are at least three, else of the last
    180 when there are at least three, else all of them."""
    for reach in (ROUND_RECENT_DAYS, ROUND_RECENT_WIDE_DAYS):
        got = [d for d in days if d >= today - dt.timedelta(days=reach)]
        if len(got) >= ROUND_RECENT_MIN:
            return got
    return list(days)


# a recent gap is a change of habit when it is at least this many times the
# long one, or at most its inverse; within that, the long gap (more
# payments, less noise) stands
ROUND_HABIT_CHANGE = 1.75


def habit_gap(days: list[dt.date], today: dt.date, min_gaps: int, *,
              over: int | None = None) -> float | None:
    """The usual gap between payment days (each over `over` days apart,
    PAYDAY_HABIT_DAYS by default):
    the long median, unless the recent rounds (recent_rounds) hold
    `min_gaps` gaps whose median is a change of habit (ROUND_HABIT_CHANGE)
    — a household that moved from every payday to monthly is read
    monthly, one whose spacing merely varies keeps its long gap."""
    def gaps_of(xs):
        return [(b - a).days for a, b in zip(xs, xs[1:])
                if (b - a).days > lim]
    lim = PAYDAY_HABIT_DAYS if over is None else over
    long_ = gaps_of(days)
    if len(long_) < min_gaps:
        return None
    gap = statistics.median(long_)
    recent = recent_rounds(days, today)
    near = gaps_of(recent)
    before = gaps_of([d for d in days if d < recent[0]]) if recent else []
    if len(near) >= min_gaps and before:
        # the change is judged on the means of the recent rounds and of
        # the ones before them (a spacing that alternates two and four
        # weeks has an unsteady median, not a new habit)
        m_old, m_near = statistics.mean(before), statistics.mean(near)
        if (m_near >= m_old * ROUND_HABIT_CHANGE
                or m_near * ROUND_HABIT_CHANGE <= m_old):
            return statistics.median(near)
    return gap


def business_days_after(start: dt.date, d: dt.date) -> int:
    """How many business days after `start` the day `d` is (0 on it)."""
    n, x = 0, start
    while x < d:
        x = business_days.next_business_day(x + dt.timedelta(days=1))
        if x <= d:
            n += 1
    return n


def _card_flows(conn, card_id: str, today: dt.date) -> tuple[float, float]:
    """(charged, paid) on the card over the last CARD_LAG_DAYS: posted
    money out, and posted money in."""
    r = conn.execute(
        "SELECT COALESCE(SUM(amount) FILTER (WHERE amount > 0), 0) AS c, "
        "COALESCE(-SUM(amount) FILTER (WHERE amount < 0), 0) AS p "
        "FROM transactions WHERE removed = 0 AND COALESCE(pending, 0) = 0 "
        "AND account_id = %s AND date >= %s AND date < %s",
        (card_id, today - dt.timedelta(days=CARD_LAG_DAYS), today)).fetchone()
    return float(r["c"] or 0), float(r["p"] or 0)


# A household that pays its cards right after payday (specs/today-figures.md,
# "Your usual: after payday when checking can cover it"): over the last
# PAYDAY_HABIT_MONTHS, at least PAYDAY_HABIT_SHARE of the card-payment
# dollars that left checking left within PAYDAY_HABIT_DAYS calendar days
# after one of the household's own paydays, over at least
# PAYDAY_HABIT_MIN_PAYMENTS payments, with a paycheck series that has
# PAYDAY_HABIT_MIN_DEPOSITS deposits in the window. Its cushion is the
# PAYDAY_CUSHION_PERCENTILE of checking right after its past rounds over
# PAYDAY_CUSHION_MONTHS, floored at $0 and capped at one paycheck; $0 under
# PAYDAY_CUSHION_MIN_ROUNDS rounds.
PAYDAY_HABIT_MONTHS = 6
PAYDAY_HABIT_DAYS = 4
PAYDAY_HABIT_SHARE = 0.60
PAYDAY_HABIT_MIN_PAYMENTS = 4
PAYDAY_HABIT_MIN_DEPOSITS = 4
PAYDAY_CUSHION_MONTHS = 12
PAYDAY_CUSHION_PERCENTILE = 0.20
# the floor is never above this share of a paycheck
PAYDAY_FLOOR_PAYCHECK_SHARE = 0.5
PAYDAY_CUSHION_MIN_ROUNDS = 4


def checking_card_payments(conn, since: dt.date, until: dt.date
                           ) -> list[tuple[dt.date, float]]:
    """Card payments that left the household's checking, dated [since,
    until): the rows the feed marks as a card payment, and a plain
    transfer out that pairs with a payment credit on a card (the Cash Flow
    month's own pairing, reporting.claim_card_settlement). Posted only."""
    from . import reporting
    pay_rows, xfer_rows = reporting.card_settlement_legs(conn, since, until)
    credits = [c for c in reporting.card_credit_inflows(
        conn, since - dt.timedelta(days=reporting.CARD_SETTLE_DAYS),
        until + dt.timedelta(days=reporting.CARD_SETTLE_DAYS))
        if not c["pending"]]
    out = []
    for r in pay_rows:
        if not r["pending"]:
            out.append((as_date(r["date"]), float(r["amount"])))
    for r in xfer_rows:
        if r["pending"]:
            continue
        if reporting.claim_card_settlement(credits, r["date"],
                                           float(r["amount"])) is not None:
            out.append((as_date(r["date"]), float(r["amount"])))
    return sorted(out)


def card_payday_habit(conn, cfg: dict, today: dt.date,
                      checking_bal: float | None, *,
                      memo: dict | None = None) -> dict:
    """Whether the household pays its cards right after payday — the one
    function that decides it: {payer, share, payments, rounds, cushion}.
    `share` is the card-payment dollars from checking (checking_card_
    payments) over the last PAYDAY_HABIT_MONTHS that left within
    PAYDAY_HABIT_DAYS days after one of its paydays (the deposits its
    income series claim); `payer` when that share is at least
    PAYDAY_HABIT_SHARE over PAYDAY_HABIT_MIN_PAYMENTS payments and a
    series has PAYDAY_HABIT_MIN_DEPOSITS deposits in the window.
    `cushion`: checking at the end of the last day of each past round (the
    payments after one payday) over PAYDAY_CUSHION_MONTHS, its
    PAYDAY_CUSHION_PERCENTILE, floored at $0, capped at one paycheck (the
    median deposit of the largest series); $0 under
    PAYDAY_CUSHION_MIN_ROUNDS rounds."""
    out = {"payer": False, "share": 0.0, "payments": 0, "rounds": 0,
           "cushion": 0.0, "paydays": [], "gap": None, "last_round": None,
           "delay": None}
    since = today - dt.timedelta(days=31 * PAYDAY_CUSHION_MONTHS)
    recent = today - dt.timedelta(days=31 * PAYDAY_HABIT_MONTHS)
    series = budget._recurring_bills(conn, caps=cfg.get("occurrence_caps"),
                                     disabled=cfg.get("disabled_bills"),
                                     income=True, memo=memo)
    if not series:
        return out
    claims = budget._claim_income_rows(
        bills._income_rows(conn, today, memo=memo),
        budget._income_claimants(conn, series, memo=memo))
    deposits = {p: [r for r in rows if since <= as_date(r["date"]) < today]
                for p, rows in claims.items()}
    if not any(sum(1 for r in rows if as_date(r["date"]) >= recent)
               >= PAYDAY_HABIT_MIN_DEPOSITS for rows in deposits.values()):
        return out
    days = sorted({as_date(r["date"]) for rows in deposits.values()
                   for r in rows})
    out["paydays"] = days
    pays = checking_card_payments(conn, since, today)

    def payday_of(d: dt.date) -> dt.date | None:
        near = [p for p in days if 0 <= (d - p).days <= PAYDAY_HABIT_DAYS]
        return max(near) if near else None

    recent_pays = [(d, a) for d, a in pays if d >= recent]
    total = sum(a for _d, a in recent_pays)
    near = sum(a for d, a in recent_pays if payday_of(d) is not None)
    out["payments"] = len(recent_pays)
    out["share"] = round(near / total, 3) if total > 0 else 0.0
    out["payer"] = (len(recent_pays) >= PAYDAY_HABIT_MIN_PAYMENTS
                    and out["share"] >= PAYDAY_HABIT_SHARE)
    # each past round's last day, and checking at its end: today's
    # balance plus every posted checking row after that day
    rounds: dict[dt.date, dt.date] = {}
    for d, _a in pays:
        p = payday_of(d)
        if p is not None:
            rounds[p] = max(rounds.get(p, d), d)
    out["rounds"] = len(rounds)
    if rounds:
        out["last_round"] = max(rounds.values())
        # how many business days after its payday a round is paid
        out["delay"] = round(statistics.median(
            business_days_after(p, d) for p, d in rounds.items()))
    # the gap and the cushion are read from the recent rounds
    # (recent_rounds): a household that moved from every payday to about
    # monthly is walked monthly
    if len(rounds) >= PAYDAY_CUSHION_MIN_ROUNDS:
        out["gap"] = habit_gap(sorted(rounds.values()), today, 1, over=-1)
    if len(rounds) < PAYDAY_CUSHION_MIN_ROUNDS or checking_bal is None:
        return out
    acct = checking_pick(conn, cfg)[0]
    rows = conn.execute(
        "SELECT date, amount FROM transactions WHERE removed = 0 "
        "AND COALESCE(pending, 0) = 0 AND account_id = %s AND date > %s "
        "AND date < %s ORDER BY date, id",
        (acct, min(rounds.values()), today)).fetchall()
    # the floor: the lowest checking the household let itself reach right
    # after a round over the last PAYDAY_CUSHION_MONTHS — the tolerance it
    # has shown, not what it happened to have left over
    bals = [checking_bal + sum(float(r["amount"]) for r in rows
                               if as_date(r["date"]) > last)
            for last in rounds.values()]
    big = max(deposits.values(), key=lambda rows: sum(
        float(r["amount"]) for r in rows))
    paycheck = (statistics.median(float(r["amount"]) for r in big)
                if big else 0.0)
    out["cushion"] = round(min(max(min(bals), 0.0),
                               paycheck * PAYDAY_FLOOR_PAYCHECK_SHARE), 2)
    return out


# a round card with no statement cycle, when no round is affordable, is
# paid this long after the charge
ROUND_FALLBACK_DAYS = 30
# a paycheck that landed with no round since, when there is no round in
# history to measure from, counts back this many business days
LANDED_PAYDAY_BUSINESS_DAYS = 5


def landed_paydays(conn, claims: dict, habit: dict,
                   today: dt.date) -> set[dt.date]:
    """Paychecks that already landed and whose round is still to come —
    the walk's own paydays are only the ones to come. A series' latest
    deposit (on or before today) counts when it landed on or after the
    household's last round (`habit["last_round"]`), or, with no round in
    history, within the last LANDED_PAYDAY_BUSINESS_DAYS business days;
    and no card payment has left checking since it. Its round is the next
    business day from tomorrow, under the same affordability and gap
    tests as any round."""
    last_round = habit.get("last_round")
    # step back first: previous_business_day keeps a business day as it is
    reach = today
    for _ in range(LANDED_PAYDAY_BUSINESS_DAYS):
        reach = business_days.previous_business_day(
            reach - dt.timedelta(days=1))
    landed: set[dt.date] = set()
    # a paycheck is offered its round only within the household's own
    # delay after payday plus a business day; after that the payday was
    # let pass (a skipped round), and the next payday's round is the next
    delay = habit.get("delay")
    for rows in claims.values():
        got = [as_date(r["date"]) for r in rows
               if as_date(r["date"]) <= today]
        if not got:
            continue
        last = max(got)
        if delay is not None and business_days.add_business_days(
                last, delay + 1) < today:
            continue
        if (last >= last_round) if last_round is not None else last >= reach:
            landed.add(last)
    if landed and checking_card_payments(conn, min(landed),
                                         today + dt.timedelta(days=1)):
        # a payment since the earliest: keep only paychecks after it
        paid = max(d for d, _a in checking_card_payments(
            conn, min(landed), today + dt.timedelta(days=1)))
        landed = {d for d in landed if d > paid}
    return landed


CARD_SCENARIOS = ("usual", "due")


def card_due_dates(timing: dict, today: dt.date,
                   end: dt.date) -> list[dt.date]:
    """A card's statement due dates after today through `end`, on the
    cycle card_cycle pairs (the closed statement's own due date, then a
    month on each): the real dates, not the business days a payment lands
    on. None without a cycle."""
    due = timing.get("due")
    if due is None:
        return []
    out, k = [], 0
    while (d := _add_months(due, k)) <= end:
        if d > today:
            out.append(d)
        k += 1
    return out


def card_leaves_checking(timing: dict, charge_day: dt.date, today: dt.date,
                         scenario: str = "usual") -> dt.date:
    """The day a dollar charged to the card on `charge_day` leaves
    checking — the one rule. "usual" (the household's own habit): a
    statement-mode card at the due date of the statement holding the
    charge; a lag-mode card its lag after the charge, never after that
    due date; a card with neither on the day of the charge. "due" (pay on
    the due date): every card with a cycle at that statement's due date,
    one without on the day. Rolled to the next business day; never before
    tomorrow."""
    st = statement_of(timing, charge_day)
    if scenario == "due" or timing["mode"] in ("statement", "autopay",
                                               "rounds", "carried"):
        day = st[1] if st else charge_day
    elif timing["mode"] == "lag":
        day = charge_day + dt.timedelta(days=round(timing["lag"]))
        if st:
            day = min(day, st[1])
    else:
        day = charge_day
    return max(bill_landing(day), today + dt.timedelta(days=1))


def card_debt_charges(conn, card: dict, today: dt.date) -> list[tuple]:
    """The card's debt (posted balance and pending charges) as its newest
    charges: [(charge day, amount)], walking the card's live charges back
    from the newest until they add up to the debt (the oldest taken in
    part). Debt no charge explains is dated at the last statement date,
    else today."""
    left = round(card["debt"], 2)
    out: list[tuple] = []
    if left <= 0:
        return out
    rows = conn.execute(
        """SELECT t.date, t.amount FROM transactions t
            WHERE t.removed = 0 AND t.amount > 0 AND t.account_id = %s
              AND (COALESCE(t.pending, 0) = 0 OR NOT EXISTS (
                   SELECT 1 FROM transactions s
                    WHERE s.removed = 0 AND COALESCE(s.pending, 0) = 0
                      AND s.account_id = t.account_id AND s.id <> t.id
                      AND (s.pending_transaction_id = t.id
                           OR (s.pending_transaction_id IS NULL
                               AND s.raw->>'pending_transaction_id' = t.id))))
            ORDER BY t.date DESC, t.id DESC LIMIT 2000""",
        (card["id"],)).fetchall()
    for r in rows:
        if left <= 0.005:
            break
        take = min(left, float(r["amount"]))
        out.append((as_date(r["date"]), round(take, 2)))
        left = round(left - take, 2)
    if left > 0.005:
        out.append((card.get("issued") or today, left))
    return out




# A card payment on its way (specs/today-figures.md, "A card payment on its
# way leaves checking"): the card shows the payment's credit some business
# days before the debit leaves checking. A credit of the last
# PAYMENT_IN_FLIGHT_DAYS with no checking leg yet is walked out of checking
# PAYMENT_LANDING_BUSINESS_DAYS business days after it.
PAYMENT_IN_FLIGHT_DAYS = 5
PAYMENT_LANDING_BUSINESS_DAYS = 3


def payments_on_their_way(conn, cfg: dict, today: dt.date,
                          excluded: list) -> list[dict]:
    """The card payments whose credit is on a walked card (CARD_SCOPE_SQL)
    and whose debit has not left the household's checking yet:
    [{date, amount, card, lands}] — the credit's date and amount, the
    card's name, and the day the walk takes it out of checking (the
    credit's date plus PAYMENT_LANDING_BUSINESS_DAYS business days, never
    before tomorrow, the walk's first day).

    A credit is paid for when a checking-side leg pairs with it by the
    Cash Flow month's own rule (reporting.claim_card_settlement: same
    amount to the cent, within CARD_SETTLE_DAYS, the rows marked as a card
    payment claiming first, then plain transfers out). A pending leg pairs
    only when it is already inside the balance the walk starts from
    (budget._pending_landed); a credit still pending is not in the card's
    posted balance either, so it is no payment yet."""
    from . import reporting
    cards = {r["id"]: r["name"] for r in conn.execute(
        f"""SELECT a.id, COALESCE(a.display_name, a.name) AS name
           FROM accounts a WHERE a.type = 'credit' {CARD_SCOPE_SQL}
           ORDER BY a.id""",
        (excluded,)).fetchall()}
    if not cards:
        return []
    reach = PAYMENT_IN_FLIGHT_DAYS + reporting.CARD_SETTLE_DAYS
    credits = [c for c in reporting.card_credit_inflows(
                   conn, today - dt.timedelta(days=reach), today)
               if not c["pending"]]
    if not any(c["account_id"] in cards
               and as_date(c["date"]) >= today - dt.timedelta(
                   days=PAYMENT_IN_FLIGHT_DAYS) for c in credits):
        return []
    pay_rows, xfer_rows = reporting.card_settlement_legs(
        conn, today - dt.timedelta(days=reach), today + dt.timedelta(days=1))
    landed = budget._pending_landed(conn, cfg)
    offered = set()
    for r in list(pay_rows) + list(xfer_rows):
        offered.add(r["id"])
        if r["pending"] and not landed(r):
            continue
        reporting.claim_card_settlement(credits, r["date"],
                                        float(r["amount"]))
    _claim_outside_legs(conn, credits, offered,
                        today - dt.timedelta(days=reach),
                        today + dt.timedelta(days=1))
    out = []
    for c in credits:
        on = as_date(c["date"])
        if (c["account_id"] not in cards
                or on < today - dt.timedelta(days=PAYMENT_IN_FLIGHT_DAYS)):
            continue
        lands = max(business_days.add_business_days(
                        on, PAYMENT_LANDING_BUSINESS_DAYS),
                    today + dt.timedelta(days=1))
        out.append({"date": on.isoformat(), "amount": round(c["amount"], 2),
                    "card": cards[c["account_id"]], "lands": lands})
    return out


# a balance transfer's leg on the card or loan it leaves: an account
# transfer, a card payment, or a row the issuer names one
BALANCE_TRANSFER_DETAILS = ("TRANSFER_OUT_ACCOUNT_TRANSFER",
                            "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")


def _claim_outside_legs(conn, credits: list, offered: set, since: dt.date,
                        until: dt.date) -> None:
    """payments_on_their_way's second pass: a credit no checking leg paired
    with may have been funded from an account the walk does not read. Its
    leg is a posted money-out row dated [since, until), paired by the same
    rule (reporting.claim_card_settlement), on
    - a card or loan other than the credited card, shaped like a balance
      transfer (BALANCE_TRANSFER_DETAILS, or named "balance transfer") —
      never a purchase or a payment app's charge there;
    - an excluded, shadow or business account of any other type, shaped
      like a payment (a card-payment detail, or filed TRANSFER_OUT);
    - any account but a card or loan, in a weak shape — no category at
      all (a payment imported from a CSV or a statement), or a bare loan
      payment (LOAN_PAYMENTS with no detail naming another kind of loan) —
      only to a credit on a card the row's name names (_names_card). On a
      personal depository the walk reads, such a row has already taken
      the money out of the walked balance; elsewhere it is the outside
      funding. Amount alone never pairs a weak shape.
    Never a strong-shaped row on a personal depository the walk reads
    (the first pass's to pair), nor a leg the first pass already offered
    (`offered`, row ids). A paired credit is removed from `credits`: its
    money never leaves the walked checking."""
    if not credits:
        return
    from . import reporting
    excl, ex_p = reporting.excluded_accounts_sql(conn)
    walked = {r["id"] for r in conn.execute(
        f"""SELECT t.id FROM transactions t
              JOIN accounts a ON a.id = t.account_id
             WHERE t.removed = 0 AND t.amount > 0
               AND a.type NOT IN ('credit', 'loan')
               AND t.date >= %s AND t.date < %s {excl}
               {reporting._NOT_SHADOW.format(col="t.account_id")}
               {budget.PERSONAL_ONLY_SQL}
             ORDER BY t.id""", (since, until, *ex_p)).fetchall()}
    rows = conn.execute(
        """SELECT t.id, t.date, t.amount, t.account_id, a.type,
                  COALESCE(t.name, '') AS name,
                  COALESCE(t.merchant_name, '') AS merchant,
                  COALESCE(t.category_detailed, '') AS detailed,
                  COALESCE(t.category_override, t.category_primary, '')
                      AS cat
             FROM transactions t JOIN accounts a ON a.id = t.account_id
            WHERE t.removed = 0 AND COALESCE(t.pending, 0) = 0
              AND t.amount > 0 AND t.date >= %s AND t.date < %s
            ORDER BY t.date, t.id""", (since, until)).fetchall()
    names: dict | None = None
    for r in rows:
        if r["id"] in offered:
            continue
        weak = False
        if r["type"] in ("credit", "loan"):
            shaped = (r["id"] not in walked
                      and (r["detailed"] in BALANCE_TRANSFER_DETAILS
                           or "balance transfer" in r["name"].lower()))
        else:
            strong = (r["detailed"] == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"
                      or r["cat"] == "TRANSFER_OUT")
            weak = not strong and (
                r["cat"] == "" or (r["cat"] == "LOAN_PAYMENTS"
                                   and r["detailed"] not in OTHER_LOAN_DETAILS))
            shaped = weak or (strong and r["id"] not in walked)
        if not shaped:
            continue
        if weak and names is None:
            names = _card_names(conn, {c["account_id"] for c in credits})
        # never the credited card's own charge; a weak shape only to a
        # card its name names
        others = [c for c in credits if c["account_id"] != r["account_id"]
                  and (not weak or _names_card(
                      f'{r["name"]} {r["merchant"]}',
                      names.get(c["account_id"])))]
        i = reporting.claim_card_settlement(list(others), r["date"],
                                            float(r["amount"]))
        if i is not None:
            credits[:] = [c for c in credits if c is not others[i]]
            if not credits:
                return


# a loan payment filed as another kind of loan is never a card's payment
OTHER_LOAN_DETAILS = ("LOAN_PAYMENTS_CAR_PAYMENT",
                      "LOAN_PAYMENTS_MORTGAGE_PAYMENT",
                      "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT",
                      "LOAN_PAYMENTS_PERSONAL_LOAN_PAYMENT")
# words in a card's or a bank's name that name no particular card
CARD_GENERIC_WORDS = frozenset({
    "card", "cards", "credit", "visa", "mastercard", "master", "rewards",
    "reward", "bank", "banking", "account", "checking", "savings",
    "signature", "platinum", "gold", "preferred", "cash", "back",
    "cashback", "autopay", "thank", "transfer", "loan", "loans",
    "financial", "services", "national", "association", "company",
    "mobile", "payment", "payments", "epay", "member", "union",
    "federal", "trust", "points", "miles", "travel", "business",
    "american", "express", "capital", "world", "elite", "unlimited",
    "premier", "everyday", "blue", "first", "citizens", "city", "freedom",
    "secured", "plus", "select", "premium", "classic", "standard",
    "infinite", "choice", "prime", "student", "flex", "online"})
# a card's last four as a payment names them: exactly four digits right
# after x's, a star, a hash or "ending (in)" — never a check number, and
# never the tail of a longer run of digits (a date, a reference)
_LAST_FOUR = re.compile(
    r"(?:(?<![a-z])x+|\*+|#|\bending(?:\s+in)?)\s*(\d{4})(?!\d)", re.I)
_CHECK_BEFORE = re.compile(r"(?:\bcheck|\bchk|\bck)\W*$", re.I)


# other names an issuer's own payments go by
ISSUER_ALIASES = {("american", "express"): (("amex",),)}


def _phrase(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _card_names(conn, card_ids: set) -> dict[str, tuple]:
    """{card id: (product words, last four, issuer phrases)}: the words of
    the card's own, official and display names less CARD_GENERIC_WORDS and
    less its bank's words; its last four digits; its bank's name as a
    phrase, with that issuer's other names (ISSUER_ALIASES)."""
    if not card_ids:
        return {}
    out = {}
    for r in conn.execute(
            """SELECT a.id, COALESCE(a.name, '') AS name,
                      COALESCE(a.official_name, '') AS official,
                      COALESCE(a.display_name, '') AS shown,
                      COALESCE(a.mask, '') AS mask,
                      COALESCE(i.institution_name, '') AS bank
                 FROM accounts a LEFT JOIN items i ON i.id = a.item_id
                WHERE a.id = ANY(%s) ORDER BY a.id""",
            (sorted(card_ids),)).fetchall():
        bank = _phrase(r["bank"])
        words = set(budget._match_words(" ".join(
            (r["name"], r["official"], r["shown"]))))
        mask = re.sub(r"\D", "", r["mask"])[-4:]
        phrases = ((bank,) if bank else ()) + ISSUER_ALIASES.get(bank, ())
        out[r["id"]] = (words - CARD_GENERIC_WORDS - set(bank),
                        mask if len(mask) == 4 else "", phrases)
    return out


def _names_card(text: str, card: tuple | None) -> bool:
    """A row's name names the card: one of its product words, its bank's
    whole name in order (or another name of that issuer), or its last
    four written as a card's (_LAST_FOUR), not after "check"."""
    if not card:
        return False
    words, mask, phrases = card
    if words & set(budget._match_words(text)):
        return True
    got = _phrase(text)
    for ph in phrases:
        n = len(ph)
        if any(got[i:i + n] == ph for i in range(len(got) - n + 1)):
            return True
    return bool(mask) and any(
        m.group(1) == mask and not _CHECK_BEFORE.search(text[:m.start()])
        for m in _LAST_FOUR.finditer(text))


# Paydays and bills land on business days (specs/today-figures.md,
# "Paydays and bills land on business days"). A payday rolls back at most
# three days (a Monday holiday after a weekend); the walk reads a little
# past its horizon so such a payday is not lost at the edge.
PAYDAY_ROLL_REACH = 4
# how far back a series' own deposits are read for what happened the last
# time its payday fell on a non-business day, and how far from that payday
# a deposit may sit and still be its check
PAYDAY_HISTORY_DAYS = 400
PAYDAY_HISTORY_NEAR = 3


def bill_landing(due: dt.date) -> dt.date:
    """The day a bill scheduled for `due` leaves checking: an ACH debit
    on a weekend or a bank holiday posts the business day after."""
    return business_days.next_business_day(due)


def payday_habit(bill: dict, deposits: list[dict],
                 today: dt.date) -> str | None:
    """What the series' own deposits did the last time its payday fell on
    a weekend or a bank holiday: "before", "after" or "same" — where the
    deposit nearest that payday (within PAYDAY_HISTORY_NEAR days) landed —
    or None when there is no such payday with a deposit in the last
    PAYDAY_HISTORY_DAYS."""
    days = sorted({as_date(r["date"]) for r in deposits})
    if not days:
        return None
    since = today - dt.timedelta(days=PAYDAY_HISTORY_DAYS)
    y, m = today.year, today.month
    while dt.date(y, m, 1) >= since.replace(day=1):
        for d in sorted(budget.occurrences_in_month(bill, y, m),
                        reverse=True):
            if d >= today or d < since or business_days.is_business_day(d):
                continue
            near = [x for x in days
                    if abs((x - d).days) <= PAYDAY_HISTORY_NEAR]
            if near:
                got = min(near, key=lambda x: (abs((x - d).days), x))
                return ("same" if got == d
                        else "before" if got < d else "after")
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
    return None


# A paycheck series leaves the walk only when the ledger says its money
# stopped (stopped_paychecks), never because we stopped seeing it.
# paydays since the last check, each followed by other activity on the
# accounts its checks landed in
PAYCHECK_STOPPED_PAYDAYS = 2
# how long after a payday a late check may still land
PAYCHECK_STOPPED_GRACE_DAYS = 5
# the accounts are being read: a posted row this close to yesterday
PAYCHECK_FEED_FRESH_DAYS = 7
# a deposit that may be the same check under another name: an unclaimed
# inflow of half to twice its usual size anywhere since, or one of at
# least a quarter of it this close to a payday (hours cut)
PAYCHECK_OTHER_NAME_BAND = (0.5, 2.0)
PAYCHECK_SHORT_SHARE = 0.25
PAYCHECK_SHORT_NEAR_DAYS = 3
# last year's gap counts when it began this close to a year before the
# last check
PAYCHECK_SEASON_DAYS = 31


def stopped_paychecks(conn, cfg: dict, today: dt.date, *,
                      memo: dict | None = None) -> dict[str, dt.date]:
    """_stopped_paychecks, a fact as of the end of yesterday: judged once
    a day per household and kept with the day's learned facts, so the
    build and `next_paycheck` read the same answer."""
    return learned(conn, cfg, today, "stopped",
                   lambda: _stopped_paychecks(conn, cfg, today, memo=memo))


def _stopped_paychecks(conn, cfg: dict, today: dt.date, *,
                       memo: dict | None = None) -> dict[str, dt.date]:
    """{payee: its last check} of the income series whose money stopped,
    as the ledger stood at the end of yesterday. A series stops only when
    all of these hold (_paycheck_stop_evidence):

    - it has a recurrence and at least one check it claims
      (budget._claim_income_rows) dated before `today`;
    - the accounts its last checks landed in are being read: their newest
      posted row is at most PAYCHECK_FEED_FRESH_DAYS before yesterday — a
      feed that went quiet, or a statement imported once a month, says
      nothing about the paycheck;
    - at least PAYCHECK_STOPPED_PAYDAYS of its paydays have passed since
      its last check, each with a posted row on those accounts at least
      PAYCHECK_STOPPED_GRACE_DAYS after it;
    - no deposit there since could be the same check under another name
      or amount: an inflow no income series claims — of income type or
      none, or of a type the series' own checks carry, and not one leg of
      the household's own transfer (forecast_score.own_transfer_ids) —
      of PAYCHECK_OTHER_NAME_BAND times its usual check, or of at least
      PAYCHECK_SHORT_SHARE of it within PAYCHECK_SHORT_NEAR_DAYS of a
      payday (the deposit floor of _income_rows does not apply here).
      Transfers in count too (a renamed payroll is often filed so). A
      deposit another series claims (a new job's) is that series' own
      only when it fits that series — half to twice its usual check,
      within PAYCHECK_SHORT_NEAR_DAYS of one of its paydays; otherwise it
      may still be this check;
    - its own history shows no gap at least as long beginning about a
      year before its last check (PAYCHECK_SEASON_DAYS): a school-year
      salary is not stopped in its summer.

    The walk leaves such a series out, `next_paycheck` never names it,
    and the reply says so (`stopped_paychecks`)."""
    series = budget._recurring_bills(conn, caps=cfg.get("occurrence_caps"),
                                     disabled=cfg.get("disabled_bills"),
                                     income=True, memo=memo)
    if not series:
        return {}
    claims = budget._claim_income_rows(
        bills._income_rows(conn, today, memo=memo),
        budget._income_claimants(conn, series, memo=memo))
    by_payee = {b["payee"]: b for b in series}
    owner = {r["txn_id"]: p for p, rows in sorted(claims.items())
             for r in rows}
    usual_of = {p: _usual_check(rows, by_payee.get(p))
                for p, rows in sorted(claims.items())}

    def fits_owner(txn_id, amount: float, on: dt.date,
                   payee: str) -> bool:
        """The deposit is its claimer's own check: the claimer is another
        series, and the deposit fits its size and one of its paydays."""
        p = owner.get(txn_id)
        if p is None or p == payee or p not in by_payee:
            return False
        lo, hi = PAYCHECK_OTHER_NAME_BAND
        u = usual_of.get(p) or 0.0
        if not lo * u <= amount <= hi * u:
            return False
        near = PAYCHECK_SHORT_NEAR_DAYS
        months = {(x.year, x.month) for x in (
            on - dt.timedelta(days=near), on, on + dt.timedelta(days=near))}
        return any(abs((d - on).days) <= near
                   for y, m in sorted(months)
                   for d in budget.occurrences_in_month(by_payee[p], y, m))

    out: dict[str, dt.date] = {}
    for b in sorted(series, key=lambda x: (x["payee"], x.get("id") or "")):
        if b["payee"] in out:
            continue
        deps = sorted((r for r in claims.get(b["payee"]) or []
                       if as_date(r["date"]) < today),
                      key=lambda r: (as_date(r["date"]), str(r["txn_id"])))
        last = _paycheck_stop_evidence(conn, b, deps, owner, fits_owner,
                                       today)
        if last is not None:
            out[b["payee"]] = last
    return out


def _usual_check(rows: list[dict], bill: dict | None) -> float:
    """A series' usual check: the median of its last three claimed
    deposits, else its own amount."""
    got = sorted(rows, key=lambda r: (as_date(r["date"]), str(r["txn_id"])))
    if got:
        return statistics.median(float(r["amount"]) for r in got[-3:])
    return float((bill or {}).get("amount") or 0.0)


def _paycheck_stop_evidence(conn, bill: dict, deps: list[dict],
                            owner: dict, fits_owner,
                            today: dt.date) -> dt.date | None:
    """stopped_paychecks' test of one series: its last check when the
    ledger says the series stopped, else None. `owner`: {deposit id:
    the series that claims it}; `fits_owner(id, amount, day, payee)`: the
    deposit is that other series' own check."""
    rec = bill.get("recurrence") or {}
    if not rec.get("frequency") or not deps:
        return None
    cycle = max(1, budget._cycle_days(rec))
    last = as_date(deps[-1]["date"])
    grace = dt.timedelta(days=PAYCHECK_STOPPED_GRACE_DAYS)
    if (today - last).days <= cycle + PAYCHECK_STOPPED_GRACE_DAYS:
        return None
    yesterday = today - dt.timedelta(days=1)
    # the paydays since the last check whose grace has run out
    paydays = []
    y, m = last.year, last.month
    while dt.date(y, m, 1) <= today:
        for d in budget.occurrences_in_month(bill, y, m):
            if (d - last).days > cycle // 2 and d + grace <= yesterday:
                paydays.append(d)
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    paydays = sorted(set(paydays))
    if len(paydays) < PAYCHECK_STOPPED_PAYDAYS:
        return None
    accounts = sorted({str(r["account_id"]) for r in deps[-3:]})
    rows = conn.execute(
        """SELECT t.id, t.date, t.amount, t.account_id,
                  COALESCE(t.category_override, t.category_primary, '')
                      AS cat
             FROM transactions t
            WHERE t.removed = 0 AND COALESCE(t.pending, 0) = 0
              AND t.account_id = ANY(%s) AND t.date > %s AND t.date < %s
            ORDER BY t.date, t.id""", (accounts, last, today)).fetchall()
    if not rows:
        return None
    seen = [as_date(r["date"]) for r in rows]
    # (a) the accounts are being read
    if (yesterday - seen[-1]).days > PAYCHECK_FEED_FRESH_DAYS:
        return None
    # (b) enough paydays passed with the accounts read after each
    if sum(1 for d in paydays if seen[-1] >= d + grace) \
            < PAYCHECK_STOPPED_PAYDAYS:
        return None
    # (c) no deposit that may be the same check under another name
    from . import forecast_score
    own: set = set()
    for acct in accounts:
        own |= forecast_score.own_transfer_ids(
            conn, acct, last, yesterday, partner_until=yesterday)
    kinds = ({"INCOME", "TRANSFER_IN", ""}
             | {str(r.get("category") or "") for r in deps})
    usual = _usual_check(deps, bill)
    lo, hi = PAYCHECK_OTHER_NAME_BAND
    for r in rows:
        amt = -float(r["amount"])
        if (amt <= 0 or r["id"] in own or r["cat"] not in kinds
                or owner.get(r["id"]) == bill["payee"]
                or fits_owner(r["id"], amt, as_date(r["date"]),
                              bill["payee"])):
            continue
        if lo * usual <= amt <= hi * usual:
            return None
        on = as_date(r["date"])
        if amt >= PAYCHECK_SHORT_SHARE * usual and any(
                abs((on - d).days) <= PAYCHECK_SHORT_NEAR_DAYS
                for d in paydays):
            return None
    # (d) a gap at least this long, at this time of year, before
    silent = (today - last).days
    days = [as_date(r["date"]) for r in deps]
    for p, q in zip(days, days[1:], strict=False):
        if (abs(((last - p).days) - 365) <= PAYCHECK_SEASON_DAYS
                and (q - p).days > 2 * cycle
                and (q - p).days + PAYCHECK_STOPPED_GRACE_DAYS >= silent):
            return None
    return last


def payday_landing(due: dt.date, habit: str | None) -> dt.date:
    """The day a paycheck scheduled for `due` is walked in: `due` itself
    on a business day; else the business day before (a direct deposit
    lands then), unless the series' own history (payday_habit) says it
    lands after, or on the day itself."""
    if business_days.is_business_day(due) or habit == "same":
        return due
    if habit == "after":
        return business_days.next_business_day(due)
    return business_days.previous_business_day(due)


# tenant -> (key, built forecast, expires at): the forecast a page's
# requests share — Today, the Bills calendar and the Cash Flow tail each ask
# for it, and the second asks while nothing it reads has been written.
_BUILT = _Kept(KEPT_HOUSEHOLDS)
BUILT_TTL_S = 600.0


def build(conn, today: dt.date | None = None, days: int = HORIZON_DAYS,
          *, memo: dict | None = None) -> dict:
    """The cash forecast (_build), kept per household for the day and
    BUILT_TTL_S while nothing it reads is written (learned_stamp): the
    Today page, the Bills calendar and the Cash Flow tail build it once
    between them. The result is read-only to every caller."""
    import time
    cfg = budget.load_config(conn, memo=memo)
    day = today or localtime.now_local(cfg).date()
    tid, stamp = learned_stamp(conn, cfg, day)
    key = (stamp, day, days)
    now = time.monotonic()
    hit = _BUILT.get(tid)
    if hit is not None and hit[0] == key and hit[2] > now:
        return hit[1]
    # the day every month this build reads is read as of (one measured
    # window for all of them: reporting.month_deposit_rows)
    memo = {} if memo is None else memo
    memo.setdefault("forecast.as_of", day)
    out = _build(conn, day, days, memo=memo)
    _BUILT.put(tid, (key, out, now + BUILT_TTL_S))
    # what this build had to learn is stored for every worker and the
    # rest of the day (forecast_learned)
    store_learned(conn, day, tid=tid)
    return out


def _build(conn, today: dt.date | None = None, days: int = HORIZON_DAYS,
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
    # posted balances plus pending charges (card_balances): a pending
    # charge is money the card will take from checking like any other
    # every walked card once a build (card timing reads the idle ones
    # too); the walk pays the ones that owe something
    all_cards = card_balances(conn, excluded, with_empty=True)
    cards = [c for c in all_cards if c["posted"] > 0 or c["pending"] > 0]
    card_debt = sum(c["debt"] for c in cards)
    start = checking_bal - card_debt

    # The full-balance payoff (card_events): each card's whole debt at its
    # due date — the engine-internal plan/pace walks and the calendar's
    # card cells read it. The card-aware walks below read debt_lumps.
    card_events: list[tuple[dt.date, float, str]] = []
    # each card's debt split by its statements, as a due-date payer pays
    # it (specs/today-figures.md, "A statement already paid is not
    # scheduled again", "Pending card charges leave checking at the next
    # statement"): what is still owed of the last statement at the due
    # date, the rest of the posted balance and the pending charges at the
    # statement after it. [(day, amount, dated)] per card id; `dated` when
    # the issuer gave the due date (only those are drawn as chart lines).
    debt_lumps: dict[str, list[tuple[dt.date, float, bool]]] = {}
    for c in cards:
        try:
            due = as_date(c["due"])
        except ValueError:
            # an unparsable or out-of-range due date is the "never told
            # us" case below, not a reason to 500 the whole forecast
            due = None
        # A card whose issuer never told us a due date still has to be paid,
        # so the walk schedules it tomorrow rather than pretending the money
        # is safe. That date is an assumption, not a calendar fact, which is
        # why only the real ones become dated markers: drawing a line
        # labelled "autopay" on a day nothing is known to happen would be a
        # guess wearing a fact's clothes.
        dated = due is not None and due > today
        if not dated:
            due = today + dt.timedelta(days=1)
        if (c["posted"] > 0.5 or c["pending"] > 0.5) and due <= end:
            # said as what it is: a card's nickname ("Bonus", "Cashback")
            # followed by "payment" reads as money coming in
            card_events.append((due, -c["debt"],
                                f'Card payment — {c["name"]}'))
        # the statement part comes out of the POSTED balance; a pending
        # charge is on no statement yet and rides with the rest below
        posted = c["posted"]
        stmt = c["stmt"]
        if stmt is not None:
            stmt = statement_owed(float(stmt), c["issued"], c["paid_on"],
                                  c["paid"])
        stmt = min(stmt, posted) if stmt is not None else posted
        stmt = round(max(stmt, 0.0), 2)
        rest = round(c["debt"] - stmt, 2)
        lumps = []
        land = today + dt.timedelta(days=1)
        # the closed statement's own due date and the open one's
        # (card_cycle): a reported due date a cycle past the statement
        # date is the open statement's, and the closed one's has passed
        _iss, closed_due, open_due = card_cycle(c["issued"], due)
        if dated and open_due != closed_due:
            stmt_on, rest_on = closed_due, open_due
        else:
            stmt_on, rest_on = due, _add_months(due, 1)
        if stmt > 0.005:
            lumps.append((max(bill_landing(stmt_on), land), stmt,
                          dated and stmt_on > today))
        if rest > 0.005:
            lumps.append((max(bill_landing(rest_on), land), rest, dated))
        debt_lumps[c["id"]] = lumps

    # --- dated events: bills out, paychecks in -----------------------------
    # Paid/overdue/floored rules live in budget.upcoming_bill_occurrences —
    # the runway and Today headroom read the same helper.
    # A bill the walk takes from checking on a weekend or a bank holiday
    # posts the business day after (bill_landing); one that lands past
    # the horizon leaves the walk.
    bill_occs = budget.upcoming_bill_occurrences(conn, cfg, today, end,
                                                 memo=memo)
    # every bill's and envelope's payments, read once a build (and kept
    # with the history: bill_payments reads only rows before today), the
    # longest look-back any reader needs; each reader cuts its own
    all_envs = budget._envelope_bills(
        conn, disabled=cfg.get("disabled_bills"), memo=memo)

    def bills_paid(more: list) -> dict:
        everyone = {str(b["payee"]): b for b in
                    budget._recurring_bills(
                        conn, caps=cfg.get("occurrence_caps"),
                        disabled=cfg.get("disabled_bills"), memo=memo)
                    + [o["occ"]["bill"] for o in bill_occs] + all_envs
                    + more}
        return learned(
            conn, cfg, today, "payments:" + "|".join(sorted(everyone)),
            lambda: bill_payments(conn, today, list(everyone.values()),
                                  PAYMENTS_LOOKBACK_DAYS, memo=memo))
    # a seasonal bill's occurrences walk last year's amount for the month
    # (occurrence_amount), labelled as an estimate; only the walk's own
    # occurrence moves — the bill, the plan and the still-due figures keep
    # the bill's amount. An occurrence already part-paid or floored keeps
    # what is left of it.
    estimated: set[str] = set()
    if bill_occs:
        uniq = {id(o["occ"]["bill"]): o["occ"]["bill"] for o in bill_occs}
        hist = bills_paid(list(uniq.values()))
        raws = {r["payee"]: (r["raw"] or {}) for r in conn.execute(
            "SELECT payee, raw FROM bills WHERE payee = ANY(%s) "
            "ORDER BY uid, id",
            ([o["payee"] for o in bill_occs],)).fetchall()}
        occs2 = []
        for o in bill_occs:
            bill = o["occ"]["bill"]
            label = o["payee"]
            planned = o["planned"]
            # a future occurrence only: one due today or late is owed
            # as the bill says
            if (o["occ"]["due"] > today
                    and abs(planned - float(bill["amount"])) < 0.005):
                amt, est = occurrence_amount(
                    bill, hist.get(o["payee"], []), o["due"], today,
                    hand_set=bills.in_manual_grace(
                        raws.get(o["payee"], {}), today))
                if est:
                    planned, label = amt, f"{o['payee']} (estimate)"
                    estimated.add(label)
            occs2.append(dict(o, planned=planned, label=label))
        bill_occs = occs2
    events: list[tuple[dt.date, float, str]] = [
        (day, -b["planned"], b["label"])
        for b in bill_occs
        if (day := bill_landing(b["due"])) <= end]

    # a card payment the card already shows and checking does not yet:
    # the card's balance has dropped by it, so the walk takes it out of
    # checking on the day it should land
    for pay in payments_on_their_way(conn, cfg, today, excluded):
        if pay["lands"] <= end:
            events.append((pay["lands"], -pay["amount"],
                           f'Card payment on its way — {pay["card"]}'))

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
    # the days a paycheck lands in the walk (a payday payer's card rounds)
    paydays: set[dt.date] = set()
    # read a few days past the horizon: a payday just after it that falls
    # on a weekend or holiday lands inside it (payday_landing)
    paychecks = budget.upcoming_bill_occurrences(
        conn, cfg, today, end + dt.timedelta(days=PAYDAY_ROLL_REACH),
        income=True, memo=memo)
    # every deposit to the one series it belongs to, once for the walk
    claims = (budget._claim_income_rows(
        bills._income_rows(conn, today),
        budget._income_claimants(conn, [p["occ"]["bill"] for p in paychecks]))
        if paychecks else {})
    habits: dict[str, str | None] = {}
    # a series whose money stopped is not walked (stopped_paychecks)
    stopped = stopped_paychecks(conn, cfg, today, memo=memo) \
        if paychecks else {}
    for p in paychecks:
        payee = p["payee"]
        if payee in stopped:
            continue
        if payee not in habits:
            habits[payee] = payday_habit(p["occ"]["bill"],
                                         claims.get(payee) or [], today)
        day = p["due"]
        if day == p["occ"]["due"]:
            # a payday re-dated to tomorrow (due today, not landed yet)
            # keeps its day; a scheduled one lands on a business day
            day = max(payday_landing(day, habits[payee]),
                      today + dt.timedelta(days=1))
        if day > end:
            continue
        if payee not in amounts:
            amounts[payee] = _paycheck_amount(
                conn, p["occ"]["bill"], today, cap=pay_caps.get(payee),
                claims=claims)
        # a future payday walks last year's pattern (paycheck_pattern_amount);
        # the plan's income and the paychecks still due keep their figures
        amt, est = amounts[payee], False
        if p["occ"]["due"] > today:
            amt, est = paycheck_pattern_amount(
                amounts[payee], claims.get(payee) or [], p["occ"]["due"],
                today)
        label = (f"{payee} (estimate, from last year's pattern)" if est
                 else payee)
        if est:
            estimated.add(label)
        events.append((day, amt, label))
        paydays.add(day)

    # --- daily burn rates ---------------------------------------------------
    st = budget.month_status(conn, today, memo=memo)

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
    # the household's usual pace (forecast_pace): the last 60 finished
    # days less their one-offs — the verdict keeps the month to date
    # (the window's rows are the pace's working, which no reader of the
    # build needs: they are not kept)
    fpace = learned(conn, cfg, today, "pace",
                    lambda: {k: v for k, v in forecast_pace(
                        conn, today, st=st, memo=memo).items()
                        if k != "rows"})
    plan_daily = fpace["plan_daily"]
    pace_daily = fpace["daily"]
    one_offs = fpace["one_offs"]

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
    # the same drip per envelope, for the card-aware walk (each envelope
    # leaves checking by the account that pays it)
    env_each: dict[str, dict[dt.date, float]] = {}
    _d = today
    for _ in range(days):
        _d += dt.timedelta(days=1)
        r = 0.0
        for e in env_states:
            if e["period_months"] >= 12:               # annual pool
                if _d.year == today.year:
                    x = (e["period_left"] / days_left_year
                         if days_left_year else 0.0)
                else:                                   # pool resets Jan 1
                    x = e["pool"] / (366 if calendar.isleap(_d.year) else 365)
            elif (_d.year, _d.month) == (today.year, today.month):
                x = (max(0.0, e["pool"] - e["used"]) / days_left_month
                     if days_left_month else 0.0)
            else:                                       # pool resets at the 1st
                x = e["pool"] / calendar.monthrange(_d.year, _d.month)[1]
            r += x
            byd = env_each.setdefault(e["payee"], {})
            byd[_d] = byd.get(_d, 0.0) + x
        env_by_day[_d] = r
    # today's effective envelope rate — feeds the reported burn `rate` only
    env_daily = env_by_day.get(today + dt.timedelta(days=1), 0.0)

    by_day: dict[dt.date, float] = {}
    for d, amt, _ in events:
        by_day[d] = by_day.get(d, 0.0) + amt

    card_by_day: dict[dt.date, float] = {}
    for d, amt, _ in card_events:
        card_by_day[d] = card_by_day.get(d, 0.0) + amt

    def walk(rate: float, start_val: float | None = None,
             extra: dict[dt.date, float] | None = None, *,
             dated: dict[dt.date, float] | None = None,
             env: dict[dt.date, float] | None = None,
             shown_rate: float | None = None):
        # `dated`, `env`: the walk's own checking events and envelope drip
        # (the card-aware walks pass theirs); `shown_rate` the spending
        # per day the walk reports when only part of `rate` is checking's
        bal = start if start_val is None else start_val
        dated = by_day if dated is None else dated
        env = env_by_day if env is None else env
        # the chart anchors at TODAY's position — the
        # walk itself still starts tomorrow (today's posted spend is
        # already inside the starting balance)
        dates = [today]
        vals = [bal]
        d = today
        for _ in range(days):
            d += dt.timedelta(days=1)
            bal += dated.get(d, 0.0) - rate - env.get(d, 0.0)
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
                "rate": round((rate if shown_rate is None else shown_rate)
                              + env_daily, 2),
                # what this scenario actually swept, for the event list
                "sweep_events": [(dd.isoformat(), round(a, 2), p)
                                 for dd, a, p in swept]}

    # --- card spending leaves checking when the card is paid -------------
    # (specs/today-figures.md, "Card spending leaves checking when the
    # household pays the card"). Which account pays each bill and envelope
    # (bill_paying_accounts), each card's timing (card_timing) and the one
    # rule for a charged dollar (card_leaves_checking); every walked dollar
    # charged to a card leaves checking once — through one card payment
    # inside the horizon, or counted in cards_after_horizon.
    # the card timing and the payday habit read the history and the
    # balances as they stood at the end of yesterday (today's balance less
    # today's rows): a sync today that brings both moves neither
    eod, eod_posted, eod_checking = yesterday_balances(
        conn, all_cards, checking_pick(conn, cfg)[0], today,
        None if no_checking else checking_bal)
    eod_cards = [dict(c, posted=eod_posted[c["id"]],
                      debt=max(eod_posted[c["id"]], 0.0))
                 for c in all_cards]
    habit = (learned(conn, cfg, today, f"habit:{eod}",
                     lambda: card_payday_habit(conn, cfg, today,
                                               eod_checking, memo=memo))
             if all_cards and not no_checking else
             {"payer": False, "share": 0.0, "payments": 0, "rounds": 0,
              "cushion": 0.0, "paydays": [], "gap": None,
              "last_round": None})
    timing = learned(conn, cfg, today, f"timing:{eod}",
                     lambda: card_timing(conn, eod_cards, today, habit=habit))
    # the household pays after payday when any of its cards is paid so
    habit = dict(habit, payer=any(t["mode"] == "rounds"
                                  for t in timing.values()))
    landed = landed_paydays(conn, claims, habit, today)
    # what is seen today, live on top of what was learned: the cards with
    # a payment credit dated today
    from . import reporting as _rep
    paid_today = {p["account_id"] for p in _rep.card_credit_inflows(
        conn, today, today) if not p["pending"]}
    bill_dicts = {id(o["occ"]["bill"]): o["occ"]["bill"] for o in bill_occs}
    paid_all = bills_paid([])
    pay = learned(
        conn, cfg, today,
        "pay:" + "|".join(sorted(str(b["payee"]) for b in bill_dicts.values())),
        lambda: bill_paying_accounts(conn, cfg, today,
                                     list(bill_dicts.values()), memo=memo,
                                     paid=paid_all))
    envs = [e for e in all_envs if e["payee"] in env_each]
    env_pay = learned(
        conn, cfg, today,
        "envpay:" + "|".join(sorted(str(e["payee"]) for e in envs)),
        lambda: bill_paying_accounts(conn, cfg, today, envs, memo=memo,
                                     raw=True, paid=paid_all))
    card_bills = [o for o in bill_occs
                  if pay.get(o["payee"]) in timing and o["due"] <= end]
    # checking's own events: every dated event but the bills a card pays
    ck_events = list(events)
    for o in card_bills:
        ev = (bill_landing(o["due"]), -o["planned"], o["label"])
        if ev in ck_events:
            ck_events.remove(ev)
    ck_by_day: dict[dt.date, float] = {}
    for d, amt, _ in ck_events:
        ck_by_day[d] = ck_by_day.get(d, 0.0) + amt
    shares = {k: v for k, v in (fpace.get("shares") or {}).items()
              if k == CHECKING or k in timing}
    share_ck = 1.0 - sum(v for k, v in shares.items() if k != CHECKING)
    # an envelope with no payment of its own is split like the pace
    env_ck: dict[dt.date, float] = {}
    env_card: dict[str, dict[dt.date, float]] = {}
    for payee, byd in env_each.items():
        acct = env_pay.get(payee)
        split = ({acct: 1.0} if acct in timing
                 else {CHECKING: 1.0} if acct == CHECKING
                 else dict(shares, **{CHECKING: share_ck}))
        for k, sh in split.items():
            dest = env_ck if k == CHECKING else env_card.setdefault(k, {})
            for d, x in byd.items():
                dest[d] = dest.get(d, 0.0) + x * sh

    _debt_rows: dict[str, list] = {}

    def debt_charges(c: dict) -> list:
        if c["id"] not in _debt_rows:
            _debt_rows[c["id"]] = card_debt_charges(conn, c, today)
        return _debt_rows[c["id"]]

    # every walked card's statement due dates inside the horizon — a fact
    # about the card, the same in every walk (card_due_dates), for a card
    # that owes something or is charged in the walk
    due_cards = ({c["id"] for c in cards} | set(shares) | set(env_card)
                 | {pay[o["payee"]] for o in card_bills}) & set(timing)
    due_dates = sorted((dd, cid) for cid in due_cards
                       for dd in card_due_dates(timing[cid], today, end))

    def pay_off_cards(leave) -> float:
        """Pay all cards today: every card's whole balance (card_balances'
        debt, posted and pending; a payment on its way is already out of
        it and in checking's own events) leaves checking on the walk's
        first day, settling its issued statements. Returns the total."""
        first_day = today + dt.timedelta(days=1)
        total = 0.0
        for c in cards:
            if c["id"] in timing and c["debt"] > 0.005:
                leave(c["id"], first_day, c["debt"], True, kind="payoff")
                total += c["debt"]
        return total

    def card_walk(sc: str, rate: float,
                  rounds: dict[dt.date, list[str]] | None = None,
                  payoff: bool = False) -> dict:
        """The card-aware walk of scenario `sc` ("usual" or "due") at
        `rate` dollars of variable spending a day. A payday payer's usual
        walk (card_payday_habit) pays every card after each payday that
        checking can cover keeping its cushion, and each statement at its
        due date otherwise. `rounds`: pay on exactly these days instead of
        deciding — a heavier line takes its scenario's own rounds, so it
        differs only by the extra spending and never sits above it.
        `payoff` (pay all cards today, then your usual — pay_today_walk):
        every card's whole balance leaves checking on the walk's first
        day instead of by its statements, and each card counts as paid
        today; the walk then goes on exactly as `sc` does."""
        out_by_day: dict[dt.date, float] = {}
        after: dict[str, float] = {cid: 0.0 for cid in timing}
        rows: dict[tuple[str, dt.date, str], float] = {}
        marks: dict[tuple[str, dt.date], float] = {}
        outs: list[tuple[dt.date, float, str]] = []
        # every dollar the walk charges to a card, on the day it is charged
        # (the pace's card shares, the envelope drips and the bills a card
        # pays): what paying every card as it is charged would send
        charges: list[tuple[dt.date, float, str]] = []
        # each card's own way (card_timing): in the usual walk a card paid
        # in rounds after payday waits in the round pool, a carried balance
        # in its own; every other card, and every card in the due-date
        # walk, leaves by card_leaves_checking
        def mode_of(cid: str) -> str:
            return timing[cid]["mode"] if sc == "usual" else "due"

        on_payday = any(mode_of(cid) == "rounds" for cid in timing)
        # [card, charged on, amount, the day the due-date rule pays it,
        # drawn as a line]
        pool: list[list] = []
        # a carried balance: [card, charged on, amount]
        carry: list[list] = []
        stays: dict[str, float] = {cid: 0.0 for cid in timing}

        def rowed(cid: str) -> bool:
            # a card paid at its statements' due dates is listed on them
            t = timing[cid]
            return mode_of(cid) in ("rounds", "carried") or (
                t["due"] is not None and (
                    sc == "due" or t["mode"] in ("statement", "autopay")))

        def leave(cid: str, day: dt.date, amt: float, row: bool,
                  mark: bool = False, kind: str = "due"):
            if amt <= 0:
                return
            if day > end:
                after[cid] += amt
                return
            out_by_day[day] = out_by_day.get(day, 0.0) - amt
            outs.append((day, amt, cid))
            if row:
                rows[(cid, day, kind)] = rows.get((cid, day, kind), 0.0) + amt
                if mark:
                    marks[(cid, day)] = marks.get((cid, day), 0.0) + amt

        # what reaches the cards after today, by day (cards_owed)
        card_in: dict[dt.date, float] = {}

        def charge(cid: str, d: dt.date, amt: float):
            if amt <= 0:
                return
            charges.append((d, amt, cid))
            if d > today:
                card_in[d] = card_in.get(d, 0.0) + amt
            m = mode_of(cid)
            if m == "rounds":
                # a round card is paid in rounds; when none is affordable,
                # at its statement's due date, or — with no cycle — at
                # ROUND_FALLBACK_DAYS after the charge
                fall = (card_leaves_checking(timing[cid], d, today, "due")
                        if timing[cid]["due"] is not None
                        else max(bill_landing(d + dt.timedelta(
                            days=ROUND_FALLBACK_DAYS)),
                            today + dt.timedelta(days=1)))
                pool.append([cid, d, amt, fall,
                             timing[cid]["due"] is not None])
                return
            if m == "carried":
                carry.append([cid, d, amt])
                return
            leave(cid, card_leaves_checking(timing[cid], d, today, sc), amt,
                  rowed(cid), mark=rowed(cid))

        for c in ([] if payoff else cards):
            m = mode_of(c["id"])
            if m == "lag":
                for d0, amt in debt_charges(c):
                    charge(c["id"], d0, amt)
            elif m == "carried":
                carry.append([c["id"], today, c["debt"]])
            else:
                for day, amt, dated_ in debt_lumps.get(c["id"], []):
                    if m == "rounds":
                        if timing[c["id"]]["due"] is None:
                            day = max(bill_landing(today + dt.timedelta(
                                days=ROUND_FALLBACK_DAYS)), day)
                        pool.append([c["id"], today, amt, day, dated_])
                    else:
                        leave(c["id"], day, amt, True, mark=dated_)
        if payoff:
            pay_off_cards(leave)
        d = today
        for _ in range(days):
            d += dt.timedelta(days=1)
            for cid, sh in shares.items():
                if cid != CHECKING and sh > 0:
                    charge(cid, d, rate * sh)
            for cid, byd in env_card.items():
                charge(cid, d, byd.get(d, 0.0))
        for o in card_bills:
            charge(pay[o["payee"]], o["due"], o["planned"])
        paid_rounds: list[dt.date] = []
        round_cards: dict[dt.date, list[str]] = {}
        # each payday's decision, for tests and read-only probes
        round_log: list[dict] = []
        if on_payday:
            # day by day, as checking would see it: each statement due that
            # day leaves; then, the business day after a payday, every card
            # is paid in full when checking stays at or above the cushion
            first = business_days.next_business_day(
                today + dt.timedelta(days=1))
            pay_days = {max(business_days.add_business_days(p, 1), first)
                        for p in paydays | landed}
            # checking as the walk will see it: its own events, its share
            # of the spending and envelopes, and every card payment already
            # placed (the other cards' statements and lags, the pay-today
            # payoff)
            placed = dict(out_by_day)
            bal = checking_bal
            later = sorted(p for p in paydays if p > today)

            def step(x: dt.date) -> float:
                return (ck_by_day.get(x, 0.0) - rate * share_ck
                        - env_ck.get(x, 0.0) + placed.get(x, 0.0))

            def refused(d: dt.date, after: float,
                        paying: list) -> str | None:
                # a round is affordable when checking right after paying
                # is at or above the floor (what the household let itself
                # reach right after a past round) AND the walk's checking
                # stays at or above $0 through the day before the next
                # paycheck lands (bills, the statements due, the other
                # cards and the spending until then counted); else the
                # test that refused it
                if after < habit["cushion"]:
                    return "floor"
                nxt = next((p for p in later if p > d),
                           end + dt.timedelta(days=1))
                mine = {id(e) for e in paying}
                run = after
                x = d + dt.timedelta(days=1)
                while x < nxt and x <= end:
                    run += step(x) - sum(e[2] for e in pool
                                         if e[2] > 0 and e[3] == x
                                         and id(e) not in mine)
                    if run < 0:
                        return "trough"
                    x += dt.timedelta(days=1)
                return None
            # learned through yesterday; a card paid today (seen live) was
            # last paid today
            last_paid = {cid: (today if cid in paid_today or payoff
                               else t.get("last_paid"))
                         for cid, t in timing.items()}
            d = today
            for _ in range(days):
                d += dt.timedelta(days=1)
                bal += step(d)
                for e in pool:
                    if e[2] > 0 and e[3] <= d:
                        leave(e[0], d, e[2], True, mark=e[4])
                        bal -= e[2]
                        e[2] = 0.0
                if d in (pay_days if rounds is None else rounds):
                    # each card joins a round once its own usual gap
                    # since its own last payment has passed (card_timing:
                    # `gap`, `last_paid`); a heavier line takes its
                    # scenario's own cards on its own days
                    if rounds is not None:
                        joins = set(rounds[d])
                    else:
                        joins = {cid for cid in timing
                                 if mode_of(cid) == "rounds" and (
                                     not timing[cid].get("gap")
                                     or last_paid.get(cid) is None
                                     or (d - last_paid[cid]).days
                                     >= ROUND_GAP_SHARE
                                     * timing[cid]["gap"])}
                    due_now = [e for e in pool if e[2] > 0 and e[1] <= d
                               and e[0] in joins]
                    why = None
                    if rounds is None and due_now:
                        why = refused(d, bal - sum(e[2] for e in due_now),
                                      due_now)
                        if why is not None:
                            # not every card: as many as stay affordable,
                            # the nearest statement due first; the rest
                            # wait for their due dates and the next payday
                            first_due: dict[str, dt.date] = {}
                            for e in due_now:
                                first_due[e[0]] = min(
                                    first_due.get(e[0], e[3]), e[3])
                            take: list = []
                            for cid in sorted(first_due,
                                              key=lambda c: (first_due[c],
                                                             c)):
                                more = take + [e for e in due_now
                                               if e[0] == cid]
                                if refused(d, bal - sum(e[2] for e in more),
                                           more) is None:
                                    take = more
                            due_now = take
                    if rounds is None:
                        round_log.append({
                            "day": d.isoformat(),
                            "eligible": sorted(joins & set(timing)),
                            "waiting_gap": sorted(
                                cid for cid in timing
                                if mode_of(cid) == "rounds"
                                and cid not in joins),
                            "paid": sorted({e[0] for e in due_now}),
                            "refused": why})
                    owed = sum(e[2] for e in due_now)
                    if owed > 0:
                        for e in due_now:
                            leave(e[0], d, e[2], True, kind="payday")
                            e[2] = 0.0
                        bal -= owed
                        paid_rounds.append(d)
                        cards_paid = sorted({e[0] for e in due_now})
                        round_cards[d] = cards_paid
                        for cid in cards_paid:
                            last_paid[cid] = d
            for e in pool:
                if e[2] > 0:
                    # its due date is past the chart's end
                    leave(e[0], e[3], e[2], True)
        # a carried balance: what the household has been paying, monthly on
        # its usual day, while the balance lasts; the rest stays on the card
        for cid in {e[0] for e in carry}:
            t = timing[cid]
            mine = [e for e in carry if e[0] == cid]
            left = 0.0
            y, mth = today.year, today.month
            while True:
                pd_ = dt.date(y, mth, min(t["payment_day"] or 1,
                                          calendar.monthrange(y, mth)[1]))
                y, mth = (y, mth + 1) if mth < 12 else (y + 1, 1)
                day = max(bill_landing(pd_), today + dt.timedelta(days=1))
                if pd_ <= today:
                    continue
                if day > end:
                    break
                owed = sum(e[2] for e in mine if e[1] <= day) - left
                take = round(min(t["payment"] or 0.0, max(owed, 0.0)), 2)
                if take > 0.005:
                    leave(cid, day, take, True, kind="carried")
                    left += take
            stays[cid] = sum(e[2] for e in mine) - left
        w = walk(rate * share_ck, start_val=checking_bal, extra=out_by_day,
                 dated=ck_by_day, env=env_ck, shown_rate=rate)
        # the payday rounds this walk paid (rounds=None: decided here)
        w["rounds"] = [dd.isoformat() for dd in paid_rounds]
        w["round_log"] = round_log
        # the cards each round paid (a heavier line pays the same)
        w["round_cards"] = {dd.isoformat(): cs
                            for dd, cs in round_cards.items()}
        names = {cid: t["name"] for cid, t in timing.items()}
        card_rows = sorted(
            ((day, -amt, f"{names[cid]} — "
              + ("paid in full" if kind == "payoff"
                 else "paid after payday" if kind == "payday"
                 else "usual payment" if kind == "carried"
                 else "statement due" if timing[cid]["due"]
                 else "card payment"))
             for (cid, day, kind), amt in rows.items()), key=lambda e: (e[0], e[2], e[1]))
        w["card_rows"] = [(dd.isoformat(), round(a, 2), p)
                          for dd, a, p in card_rows]
        marked = sorted(marks.items(), key=lambda e: (e[0][1], e[0][0]))
        w["marks"] = [(dd.isoformat(), -round(a, 2), names[cid])
                      for (cid, dd), a in marked]
        # the card id of each mark, in the same order (names can repeat)
        w["mark_cards"] = [cid for (cid, _dd), _a in marked]
        # what the cards still hold at the end of each day of the walk:
        # today's debt, plus what is charged to them, less what this walk
        # pays them
        owed = sum(c["debt"] for c in cards)
        held = []
        for dd, _v in w["series"]:
            day_ = dt.date.fromisoformat(dd)
            owed += card_in.get(day_, 0.0) + out_by_day.get(day_, 0.0)
            held.append((dd, round(owed, 2)))
        w["cards_owed"] = held
        # unrounded: the one-dollar-one-exit check adds them to the cent
        w["after"] = {cid: a for cid, a in after.items() if a > 0.005}
        w["cards_after_horizon"] = round(sum(after.values()), 2)
        # a carried balance that stays on the card past the walk
        w["stays"] = {cid: a for cid, a in stays.items() if a > 0.005}
        w["stays_on_card"] = round(sum(stays.values()), 2)
        w["outflows"] = [(dd.isoformat(), a, cid)
                         for dd, a, cid in sorted(outs, key=lambda e: (e[0], e[2], e[1]))]
        w["charges"] = [(dd.isoformat(), round(a, 2), cid) for dd, a, cid
                        in sorted(charges, key=lambda e: (e[0], e[2], e[1]))]
        # each due date with what this walk pays that statement on the day
        # it lands (None: a balance the walk carries); 0 when the walk has
        # paid it before
        land0 = today + dt.timedelta(days=1)
        w["due_marks"] = [
            (dd.isoformat(), names[cid],
             None if mode_of(cid) == "carried" else round(rows.get(
                 (cid, max(bill_landing(dd), land0), "due"), 0.0), 2))
            for dd, cid in due_dates]
        w["charged_bills"] = [
            (o["due"].isoformat(), -round(o["planned"], 2), o["label"],
             names[pay[o["payee"]]]) for o in sorted(
                 card_bills, key=lambda o: (o["due"], o["label"], o["planned"]))]
        w["checking_events"] = sorted(
            [(dd.isoformat(), round(a, 2), p) for dd, a, p in ck_events]
            + w["sweep_events"], key=lambda e: (e[0], e[2], e[1]))
        return w

    plan = walk(plan_daily, start_val=checking_bal, extra=card_by_day)
    pace = walk(pace_daily, start_val=checking_bal, extra=card_by_day)
    pace_now = walk(pace_daily)
    pace_nocards = walk(pace_daily, start_val=checking_bal)
    # the usual walk (the headline) and the due-date walk; `pace_stmt`
    # keeps the meaning an installed client gives it — every card paid at
    # its statements' due dates — and is the due-date walk itself
    pace_usual = card_walk("usual", pace_daily)
    pace_due = card_walk("due", pace_daily)
    pace_stmt = pace_due
    # pay all cards today, then your usual: the usual walk from the state
    # after a one-time payoff (the selector's "now"); pace_now keeps its
    # meaning — cash after card balances, day by day
    pace_today = card_walk("usual", pace_daily, payoff=True)
    # the daily spending that keeps the usual walk at or above $0 through
    # the chart (slow_to_rate), when it dips
    slow_to = (slow_to_rate(lambda r: card_walk("usual", r)["min"],
                            pace_daily)
               if pace_usual["negative_date"] else None)
    # the heavier-month line of each scenario: the same walk at the pace
    # times the household's own heavier-stretch factor
    heavy = learned(conn, cfg, today, "heavy",
                    lambda: heavy_month_factor(conn, today, st=st, memo=memo,
                                               paid=bills_paid([])))
    hrate = pace_daily * heavy["factor"]
    # a heavier line pays the cards on its scenario's own round days
    pace_low = card_walk("usual", hrate, rounds={
        dt.date.fromisoformat(x): cs
        for x, cs in pace_usual["round_cards"].items()})
    heavy_walks = {"usual": pace_low,
                   "now": card_walk("usual", hrate, rounds={
                       dt.date.fromisoformat(x): cs
                       for x, cs in pace_today["round_cards"].items()},
                       payoff=True),
                   "due": card_walk("due", hrate)}
    # every dated list is in one total order — day, then label, then
    # amount — never the order its rows were read in (specs/today-figures.md,
    # "Row order")
    # the event list carries the PRIMARY scenario's realized sweeps so the
    # cash timeline names the month-end dip — sweeps are per-scenario
    # (each walk troughs differently), and the usual walk is what the
    # product shows as the realistic case
    events_out = ([(d.isoformat(), round(a, 2), p)
                   for d, a, p in sorted(events, key=lambda e: (e[0], e[2], e[1]))]
                  + pace_usual["sweep_events"])
    events_out.sort(key=lambda e: (e[0], e[2], e[1]))
    return {
        "start": round(start, 2), "days": days, "card_debt": round(card_debt, 2),
        "checking": round(checking_bal, 2),
        "no_checking": no_checking,
        # the charges the daily pace leaves out (one_off_charges)
        "one_offs": one_offs,
        # the paycheck series the walk leaves out because their money
        # stopped (stopped_paychecks), by payee, each with its last check
        "stopped_paychecks": [{"payee": k, "last": v.isoformat()}
                              for k, v in sorted(stopped.items())],
        # the window the pace was read over (forecast_pace)
        "pace_basis": {"basis": fpace["basis"], "days": fpace["days"],
                       "daily": round(pace_daily, 2)},
        # the envelopes' drip the walks spend beside it, per day, on the
        # walk's first day (each walk's `rate` is the two together)
        "envelope_daily": round(env_daily, 2),
        # the same rate unrounded, for the engine's own checks
        "pace_daily": pace_daily,
        # the timeline labels whose amount is an estimate
        # (occurrence_amount; paycheck_amount_on)
        "estimated": sorted(estimated),
        "plan": plan, "pace": pace, "pace_now": pace_now,
        "pace_nocards": pace_nocards, "pace_stmt": pace_stmt,
        # your usual: the headline walk (card_walk "usual")
        "pace_usual": pace_usual,
        # {rate, scheduled}: the largest daily variable spending at which
        # the usual walk never dips below $0, or scheduled=True when no
        # spending would (null while it does not dip)
        "slow_to": slow_to,
        "events": events_out,
        # the dated checking events every bill from checking (no sweeps):
        # the pay-all-cards-now walk's own list
        "bill_events": [(d.isoformat(), round(a, 2), p)
                        for d, a, p in sorted(events, key=lambda e: (e[0], e[2], e[1]))],
        "card_events": [(d.isoformat(), round(a, 2), p)
                        for d, a, p in sorted(card_events, key=lambda e: (e[0], e[2], e[1]))],
        # the due-date walk's card payments as timeline rows and the dated
        # ones as chart lines (the fields an installed client reads beside
        # pace_stmt); the usual walk's lines for the usual chart
        "card_events_stmt": pace_stmt["card_rows"],
        "card_autopay": pace_stmt["marks"],
        "card_autopay_usual": pace_usual["marks"],
        # pay on the due date: every card paid at its statements' due dates
        "pace_due": pace_due,
        # pay all cards today, then your usual (card_walk payoff=True)
        "pace_today": pace_today,
        # the heavier month (heavy_month_factor): the usual walk at the
        # heavier pace, and each scenario's heavier line
        "pace_low": pace_low,
        "heavy": {k: {"series": w["series"], "min": w["min"],
                      "min_date": w["min_date"],
                      "negative_date": w["negative_date"]}
                  for k, w in heavy_walks.items()},
        "heavy_factor": heavy,
        # card spending a walk pays after its last day (the usual walk's)
        "cards_after_horizon": pace_usual["cards_after_horizon"],
        # where the pace's spending was charged (pace_shares), as walked
        "pace_shares": dict(shares, **{CHECKING: share_ck}),
        # how the household pays its cards after payday (card_payday_habit)
        "card_habit": habit,
        # each projected payday of the usual walk: the cards its gaps made
        # eligible, those waiting on their gap, the cards paid and the test
        # that refused the rest (floor | trough) — for tests and read-only
        # probes, never sent to a client
        "card_habit_debug": pace_usual["round_log"],
        "card_timing": [
            {"card": t["name"], "id": cid, "mode": t["mode"],
             "lag": t["lag"],
             "payment": t.get("payment"),
             # a round card's own spacing, and whether it has a cycle
             "gap": t.get("gap"), "cycle": t.get("due") is not None,
             # its last payment (learned through yesterday; today's live)
             "last_paid": (today if cid in paid_today
                           else t.get("last_paid")),
             "charged": cid in shares or cid in env_card
             or any(pay.get(o["payee"]) == cid for o in card_bills)}
            for cid, t in sorted(timing.items(), key=lambda e: e[1]["name"])
            if cid in {c["id"] for c in cards} or cid in shares
            or cid in env_card],
    }
