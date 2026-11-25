"""Bills, detection, proposals and merchant history.
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

import calendar
import datetime as dt
import psycopg
import re
from fastapi import Depends, HTTPException, Query
from fastapi.responses import JSONResponse

from . import data, demoguard
from ..engine import activity as _activity
from ..sync.base import MAX_DESCRIPTOR
from .api_common import (_category_long_ok, _category_name, _category_why,
                         _ck_ym, _conn, _log_proposal, _may_edit,
                         MERCHANT_NAME_MAX, _merchant_name_or_400, router,
                         _ser, _stamp_recurring, _today, _user)
from .fields import Fields, json_fields, json_fields_or_empty
from .schemas import documented
from .schemas.bills import (BillDelete, BillDeleted, BillDetail, BillHint,
                            BillHinted, BillPayee, BillRestored, BillSave,
                            BillSaved, BillToggle, BillToggled, BillsData,
                            BillsDetected, BillsHistory, IncomePayerSaved,
                            IncomePayerSet, MerchantCategory,
                            MerchantCategoryPreview,
                            MerchantCategoryPreviewed, MerchantCategorySet,
                            MerchantCategoryUndo, MerchantCategoryUndone,
                            ProposalAnswer, ProposalAnswered,
                            ProposalsApproveAll, ProposalsApproved)
from .security import limit


# a bill with no occurrence/envelope activity this month (income bills, or a
# fresh tenant) still carries the stats keys — the SPA renders "·" from zeros
_EMPTY_BILL_STATS = {"occurrences": 0, "paid": 0, "paid_amount": 0.0,
                     "paid_on": None, "charges_category": None,
                     "used": None, "overflow": None, "next_unpaid": None,
                     "withdrawn_on": None, "pool": None, "period_months": None,
                     "period_used": None, "period_left": None,
                     "period_frac": None, "pool_text": None}


def _month_bill_stats(conn, cfg: dict, today: dt.date, *,
                     closed: bool = False,
                     events: list | None = None) -> dict[str, dict]:
    """Per-bill occurrence/envelope detail for the current month — the Bills
    page's columns (Paid x/y; envelope Used/Chgs/Paid), computed once per
    request from the same engine passes month_status runs. Caps apply;
    DISABLED bills still get stats — they are marked, not counted, on the
    page.

    Every number here has a twin on Today and in the email, so this reads
    the ledger the way month_status reads it — same candidate occurrences,
    same row ownership, same cash basis. Where the two would differ the
    household is told two things about one payment, and neither page can be
    trusted.

    A CLOSED month is read under the settings and the bill schedule it was
    judged under — its snapshot (budget.snapshot_config /
    snapshot_bill_rows), exactly as lenses.month_state hands them to
    month_status, the live ones only where the month has none. `cfg` is
    the live config; a bill archived, repriced or disabled since, or an
    account excluded since, must not turn a rent the Month lens calls paid
    into "0 of 1" here.

    `events`, when given, receives the month's dated money as the calendar
    shows it — (date, signed amount, label) per bill paid (on the day it
    was paid, with the fees that rode with it), per paycheck received (in
    the month it pays: one that landed outside it sits on the month's
    nearest day, "landed 04/30"), and per bill the month left unpaid (on
    its due day, "not paid") — from the same occurrences the stats
    count."""
    from ..engine import budget
    from ..engine.compat import as_date
    month_start = today.replace(day=1)
    frozen = None
    if closed:
        cfg = budget.snapshot_config(conn, today.year, today.month) or cfg
        frozen = budget.snapshot_bill_rows(conn, today.year, today.month)
    # the accounts the "excl" toggle drops, read the way month_status reads
    # them — a malformed setting means "nothing excluded", never a 500
    excluded = budget.excluded_account_ids(cfg)
    # Two schedules, on purpose. The DISPLAY set is every active bill, so a
    # disabled one still shows its own Paid x/y. The BUDGET set is the one
    # month_status expands — caps, `disabled_bills`, and the card-payment
    # filter — and that is the set allowed to decide who OWNS a ledger row.
    # Ownership has to come from the budget set or the two pages contradict
    # each other: a bill the household took out of the budget would hide
    # its charges from the envelope that category-matches them here, while
    # Today hands the envelope exactly those rows.
    bills = budget._recurring_bills(conn, caps=cfg.get("occurrence_caps"),
                                    rows=frozen)
    budget_bills = budget.drop_card_pay_bills(conn, budget._recurring_bills(
        conn, caps=cfg.get("occurrence_caps"),
        disabled=cfg.get("disabled_bills"), rows=frozen), cfg=cfg)
    # by payee, the bills table's own identity — the two loads return
    # different dicts for the same bill
    in_budget = {b["payee"] for b in budget_bills}
    # Envelopes the same way: the disabled ones come back marked `off`, and
    # the claimer itself gives every budget envelope first refusal on every
    # row, at either tier, so they land on exactly month_status's numbers
    # while a disabled envelope draws on what is left — enough for the
    # informational row it renders, never at an enabled pool's expense.
    envelopes, _ = budget.display_envelopes(
        conn, disabled=cfg.get("disabled_bills"), rows=frozen)
    # The occurrence pass is month_status's own — the same function
    # (budget.month_occurrence_pass): the same row window, the same
    # candidate months, the same binding of payments to occurrences and the
    # same home month for each, so a late rent, a skipped cycle or an ended
    # series cannot read one way here and another on the Month lens. The
    # budget's bills match FIRST, over every candidate row — the matcher is
    # first-come by date gap and amount, so a disabled twin of an enabled
    # bill could otherwise win the row Today hands the enabled one — and
    # the bills the household took out of the budget match over what is
    # left: enough for their informational row, never at an enabled bill's
    # expense. Ownership for the envelope pass (`matched_idx`) is the
    # budget bills' alone.
    occ_pass = budget.month_occurrence_pass(
        conn, budget_bills, today, cfg=cfg, closed=closed,
        extra_bills=[b for b in bills if b["payee"] not in in_budget])
    rows = occ_pass["rows"]
    occs = occ_pass["occs"]
    matched_idx = occ_pass["matched_idx"]
    settled_fees = occ_pass["settled_fees"]
    py, pm = ((today.year, today.month - 1) if today.month > 1
              else (today.year - 1, 12))

    def _dated_here(r) -> bool:
        return month_start <= as_date(r["date"]) <= today

    # income series: paychecks are not in the bill occurrence pass —
    # _recurring_bills is amount<0 only and _spend_rows is outflows — so
    # the Bills page's received column needs its own. Same engine, deposit
    # side: match this month's income occurrences against posted
    # non-transfer inflows.
    income_bills = budget._recurring_bills(conn, income=True, rows=frozen)
    income_ids: set[int] = set()
    if income_bills:
        # The window budget._month_income_parts reads for the Month lens's
        # received figure. A paycheck for an occurrence due on/after the 1st
        # can post in the closing days of the PREVIOUS month, so the read
        # starts PREPAY_MATCH_DAYS before the month start; a closed month's
        # check due on its last day can post days after it ended, so a
        # closed month reads that far past its end too (live, nothing has
        # posted after today). Last month's and next month's occurrences
        # join as decoys — never emitted — so a boundary deposit attaches
        # to its own occurrence instead of eating one of this month's.
        window = dt.timedelta(days=budget.PREPAY_MATCH_DAYS)
        inflows = budget._inflow_rows(
            conn, month_start - window,
            (budget._month_after(today.year, today.month) + window
             if closed else today + dt.timedelta(days=1)),
            excluded=excluded)
        ny, nm = ((today.year, today.month + 1) if today.month < 12
                  else (today.year + 1, 1))
        decoys = budget.month_occurrences(income_bills, py, pm)
        income_occs = budget.month_occurrences(income_bills,
                                               today.year, today.month)
        budget.match_occurrences(
            inflows, decoys + income_occs
            + budget.month_occurrences(income_bills, ny, nm))
        occs += income_occs
        income_ids = {id(o) for o in income_occs}
    _, env_states = budget.match_envelopes(
        rows, envelopes, matched_idx,
        # the year-to-date pool must be drawn down by the same money the
        # verdict draws it down by, so the lookback gets the budget
        # schedule: a row an occurrence claims is the bill's there exactly
        # as it is in the current month. Without it a category-matched
        # annual envelope absorbs every earlier month's bill charges in
        # its category and the Bills page reports a pool spent by bills
        # that were never its, while Today reports it untouched.
        prior_used=budget._envelope_prior_used(
            conn, envelopes, today, excluded=excluded,
            bills=budget_bills))
    if events is not None:
        for occ in occs:
            inc = id(occ) in income_ids
            payee = occ["bill"]["payee"]
            if occ["txn"] is not None:
                on = as_date(occ["txn"]["date"])
                if inc and not month_start <= on <= today:
                    # A paycheck is the income of the month it pays
                    # (budget._month_income_parts), wherever its deposit is
                    # dated: one due on the 1st that landed on the 30th is
                    # this month's Income and no other month's. It goes on
                    # this month's grid at the nearest edge, saying the day
                    # it landed, so the calendar's came_in is the paychecks
                    # Income counts.
                    events.append((min(max(on, month_start), today),
                                   round(abs(occ["txn"]["amount"]), 2),
                                   f"{payee} — landed {on:%m/%d}"))
                    continue
                if not month_start <= on <= today:
                    continue      # paid in another month: that month's cash
                amt = abs(occ["txn"]["amount"]) + (0.0 if inc else sum(
                    abs(r["amount"]) for r in occ.get("companions") or []
                    if _dated_here(r)))
                events.append((on, round(amt if inc else -amt, 2), payee))
            elif not occ["floored"] and not inc:
                events.append((occ["due"], -round(abs(occ["planned"]), 2),
                               f"{payee} — not paid"))
    per: dict[str, dict] = {}
    for occ in occs:
        e = per.setdefault(occ["bill"]["payee"], dict(_EMPTY_BILL_STATS))
        e["occurrences"] += 1
        if occ["txn"] is not None:
            e["paid"] += 1
            on = as_date(occ["txn"]["date"]).isoformat()
            if e["paid_on"] is None or on > e["paid_on"]:
                e["paid_on"] = on
            # Cash basis, the verdict's rule: an occurrence prepaid LAST
            # month is settled here (so it is never overdue) but its money
            # left in the month that counted it, and adds no dollars to
            # this month's paid total. Sum these across the page and you
            # get the verdict's fixed actual; count last month's cash again
            # and the two pages disagree about what the month cost.
            # A paycheck is the income of the month it pays, wherever its
            # deposit is dated (budget._month_income_parts): one due on the
            # 1st that landed on the 30th before is this month's received.
            if (id(occ) in income_ids
                    or as_date(occ["txn"]["date"]) >= month_start):
                e["paid_amount"] += occ["txn"]["amount"]
                # the fees that rode with it are part of what was paid —
                # those dated in this month: a fee posted the day before
                # the 1st is last month's cash, as it is in the verdict
                e["paid_amount"] += sum(r["amount"]
                                        for r in occ.get("companions") or []
                                        if _dated_here(r))
        elif (not occ["floored"]
              and (e["next_unpaid"] is None or occ["due"] < e["next_unpaid"])):
            e["next_unpaid"] = occ["due"]
            # only a missed occurrence can be explained by a withdrawal;
            # one not yet due is just upcoming
            on = occ.get("withdrawn_on")
            e["withdrawn_on"] = (on.isoformat()
                                 if on and occ["due"] <= today else None)
    for payee, fee in settled_fees.items():
        e = per.setdefault(payee, dict(_EMPTY_BILL_STATS))
        e["paid_amount"] += fee
    for e in per.values():
        e["paid_amount"] = round(e["paid_amount"], 2)
        if e["next_unpaid"] is not None:
            e["next_unpaid"] = e["next_unpaid"].isoformat()
    for st in env_states:
        # envelope columns: Used (of the pool), Chgs (charge count, carried
        # in the `paid` key), Paid $ (used + overflow). an annual
        # envelope draws on a calendar-year pool, so `used` alone understates
        # it — carry the pool, what the year already spent, and what's left.
        drawn = st["prior_used"] + st["used"]
        # the category this month's charges carry most: the editor says it
        # beside "count all <category> spending" when the envelope's own
        # category is another one, so ticking it is not a guess
        cats: dict[str, int] = {}
        for r in st["rows"]:
            if r.get("category"):
                cats[r["category"]] = cats.get(r["category"], 0) + 1
        per[st["bill"]["payee"]] = {
            "charges_category": (max(sorted(cats), key=cats.get)
                                 if cats else None),
            "occurrences": 0, "paid": len(st["rows"]),
            "paid_amount": round(st["used"] + st["overflow"], 2),
            "used": round(st["used"], 2),
            "overflow": round(st["overflow"], 2),
            "pool": round(st["bill"]["pool"], 2),
            "period_months": st["bill"]["period_months"],
            "period_used": round(drawn, 2),
            "period_left": round(max(0.0, st["bill"]["pool"] - drawn), 2),
            # the bar both clients draw, decided once (budget.envelope_frac)
            "period_frac": budget.envelope_frac(drawn, st["bill"]["pool"]),
            # the pool named on its own, in the words every surface prints
            # (envelope_breakdown.pool_line)
            "pool_text": _pool_text(st, drawn, kind="closed" if closed
                                    else "running", m=today.month),
            "next_unpaid": None, "withdrawn_on": None, "paid_on": None}
    return per


def _pool_text(st: dict, drawn: float, *, kind: str, m: int) -> str:
    """An envelope's line on the Bills page (envelope_breakdown
    .pool_line): the spent-of-cap pair with its period said and the month's own
    spill past the pool — from the month's figures alone."""
    from . import envelope_breakdown
    b = st["bill"]
    e = {"payee": b["payee"], "uid": b.get("uid"), "used": st["used"],
         "overflow": st["overflow"], "pool": b["pool"],
         "period_months": b["period_months"], "monthly": b["monthly"],
         "period_used": drawn, "period_left": max(0.0, b["pool"] - drawn)}
    return envelope_breakdown.pool_line(envelope_breakdown._item(
        e, settled=False, kind=kind, m=m))


def paid_words(row: dict, month_end: dt.date, *, live: bool) -> str | None:
    """A bill's paid state for the month on view, in the words both
    clients print beside it (None while nothing of it is paid):

    - "paid 05/03", and on a live month whose next due date is already
      past the month's end, "paid 05/03 · next 06/03" — the paid mark is
      this month's and the date beside it is next month's (with its
      year, "next 05/03/27", when it is not this year's);
    - a bill due more than once in the month: "1 of 2 paid", then
      "2 of 2 paid · last 05/17";
    - income says "received"."""
    occ, paid = row.get("occurrences") or 0, row.get("paid") or 0
    if row.get("used") is not None or occ <= 0 or paid <= 0:
        return None
    verb = "received" if row.get("income") else "paid"

    def _d(iso):
        return dt.date.fromisoformat(iso).strftime("%m/%d")

    if paid < occ:
        return f"{paid} of {occ} {verb}"
    on = row.get("paid_on")
    text = (f"{paid} of {occ} {verb}" + (f" · last {_d(on)}" if on else "")
            if occ > 1 else f"{verb} {_d(on)}" if on else verb)
    due = row.get("due_on")
    if live and due and dt.date.fromisoformat(due) > month_end:
        # a next date in another year says it: a yearly bill's is the
        # same day as the payment beside it
        nxt = dt.date.fromisoformat(due)
        text += (f" · next {_d(due)}" if nxt.year == month_end.year
                 else f" · next {nxt:%m/%d/%y}")
    return text


# a bill this close to its due date is "soon" (amber); the words beside
# the bar say the number, the colour says it from across the room
SOON_DAYS = 3


def _cycle_days(row: dict) -> float:
    """The row's cycle in days, from its cadence value ("MONTHLY:1") and,
    for a rule on several days of the month, how many days it holds."""
    days = row.get("by_month_day") or []
    if len(days) > 1:
        return 30.44 / len(days)
    freq, _, n = (row.get("cadence_value") or "").partition(":")
    try:
        iv = int(n) or 1
    except ValueError:
        iv = 1
    per = {"DAILY": 1, "WEEKLY": 7, "MONTHLY": 30.44,
           "YEARLY": 365.25}.get(freq, 0)
    return per * iv


def due_words(row: dict, today: dt.date, *, live: bool) -> dict:
    """When a bill row is due, in the words both clients print
    (specs/honest-figures.md, "A bill row says when in the server's
    words"), counted from the household's day:

    - `due_label`: the Due column — "today", "tomorrow", else MM/DD/YY;
    - `due_text`: the countdown beside the cycle bar — "due today", "due
      tomorrow", "in 12 days", "1 day late", "3 days late" (income says
      "today" / "tomorrow"); null once the month's occurrences are paid;
    - `due_tone`: the colour those words and the bar take — "late" past
      the date, "withdrawn" past it with the charge taken back by the bank
      feed, "soon" within SOON_DAYS, else "ok"; null with no countdown;
    - `cycle_frac`: how far through its cycle the row is, 0..1.

    All null on a closed month (it has no countdown), on an envelope (it
    has no due date) and on a row with no due date."""
    out = {"due_label": None, "due_text": None, "due_tone": None,
           "cycle_frac": None}
    due = row.get("due_on")
    if (not live or not due or row.get("used") is not None
            or row.get("bill_type") == "envelope"):
        return out
    n = (dt.date.fromisoformat(due) - today).days
    out["due_label"] = ("today" if n == 0 else "tomorrow" if n == 1
                        else dt.date.fromisoformat(due).strftime("%m/%d/%y"))
    cyc = _cycle_days(row)
    out["cycle_frac"] = (0.0 if cyc <= 0
                         else round(min(1.0, max(0.0, 1 - n / cyc)), 4))
    occ, paid = row.get("occurrences") or 0, row.get("paid") or 0
    if occ > 0 and paid >= occ:
        return out
    income = bool(row.get("income"))
    out["due_text"] = (
        ("today" if income else "due today") if n == 0
        else ("tomorrow" if income else "due tomorrow") if n == 1
        else "1 day late" if n == -1
        else f"{-n} days late" if n < 0
        else f"in {n} days")
    out["due_tone"] = ("withdrawn" if n < 0 and row.get("withdrawn_on")
                       else "late" if n < 0
                       else "soon" if n <= SOON_DAYS else "ok")
    return out


# the health verdicts that put a bill in the attention queue — the
# headline's `attention` count counts the same rows
ATTENTION_HEALTH = ("stale", "mismatch", "misdated", "drifting")


def queued(row: dict) -> bool:
    """Whether a bill is in the attention queue: its health wants a
    decision, and it is neither dismissed nor left out of the budget (a
    disabled row shows no health pill and no dismiss control)."""
    return ((row.get("health") or "") in ATTENTION_HEALTH
            and not row.get("health_dismissed") and not row.get("disabled"))


def attention_words(row: dict) -> str | None:
    """The attention queue's line for a bill, after its name: the story
    the ledger tells (both clients print it as sent). None for a row whose
    health puts it in no queue (healthy, withdrawn, off the ledger). A
    dismissed or disabled row keeps its line, so a flag restored on the
    page reads right before the list is read again; the queue leaves those
    out (`queued`).

    A stale bill's "nothing since" is the newest charge from the merchant
    at any amount, because the stale verdict is judged on any of them."""
    h = row.get("health") or ""
    if h not in ATTENTION_HEALTH:
        return None
    def _money(v: float) -> str:
        # cents, as every recurring row prints its amounts
        return f"-${-v:,.2f}" if v < 0 else f"${v:,.2f}"

    def _d(iso: str) -> str:
        return dt.date.fromisoformat(iso[:10]).strftime("%m/%d/%y")

    seen = row.get("last_seen") or {}
    match = row.get("last_match")
    if h == "stale":
        last = (seen.get("date")
                if seen.get("date") and (not match or seen["date"] > match)
                else match)
        cyc = row.get("cycle_days")
        when = (f" ~{_d((dt.date.fromisoformat(last[:10]) + dt.timedelta(days=cyc)).isoformat())}"
                if last and cyc else "")
        return (f"missed its{when} cycle — "
                + (f"nothing since {_d(last)}" if last
                   else "no matching charge on record"))
    if h == "mismatch":
        sug = row.get("suggestion") or {}
        if sug.get("action") == "accept_amount" and sug.get("amount") is not None:
            return (f"charges now run {_money(sug['amount'])}, "
                    f"bill says {_money(row['amount'])}")
        near = (f"{_money(seen['amount'])} on {_d(seen['date'])}"
                if seen.get("date") else "?")
        return f"charges near {near}, bill says {_money(row['amount'])} — amounts vary"
    if h == "drifting":
        return "amount wandering — auto-corrects tonight"
    return "payments match, but the due-date anchor is off the real draft day"


def _calendar_days(events: list) -> list[dict]:
    """(date, signed amount, label) events as the calendar's days — the
    shape GET /api/calendar sends: per day its net, what came in, what
    went out, and the events out-first."""
    by_day: dict[str, list[dict]] = {}
    for on, amount, label in events:
        by_day.setdefault(on.isoformat(), []).append(
            {"label": label, "name": label, "note": None, "amount": amount,
             "kind": "income" if amount > 0 else "bill"})
    return [{"date": d,
             "total": round(sum(e["amount"] for e in evs), 2),
             "came_in": round(sum(e["amount"] for e in evs
                                  if e["amount"] > 0), 2),
             "went_out": round(-sum(e["amount"] for e in evs
                                    if e["amount"] < 0), 2),
             "events": sorted(evs, key=lambda e: e["amount"])}
            for d, evs in sorted(by_day.items())]


# where the plan's income came from (budget.income_source), as the hero says it
# "paychecks" counts every counted deposit that landed (paychecks and
# other income, budget._month_income_parts) plus the paychecks still due:
# it is the month's money in, and the note names paychecks only beside the
# portion of it they are (_income_note)
_INCOME_NOTE = {"paychecks": "this month's money in",
                "scenario": "your income scenario",
                "typed": "the monthly income set on Budget"}
_INCOME_NOTE_CLOSED = {"paychecks": "the month's money in",
                       "scenario": "the month's income scenario",
                       "typed": "the monthly income set then"}


def _income_note(st: dict, *, closed: bool) -> str:
    """The Income tile's caption. A month counted from its deposits is the
    month's money in — paychecks, interest, refunds and one-off deposits
    alike — so it never reads as paychecks alone; the paychecks still due
    (month_status's `income_expected`, part of the figure) are named with
    their amount when there are any."""
    from .report import _money
    src = st.get("income_source")
    note = (_INCOME_NOTE_CLOSED if closed else _INCOME_NOTE).get(
        src, "the month's income")
    due = st.get("income_expected")
    if (src == "paychecks" and not closed
            and isinstance(due, (int, float)) and not isinstance(due, bool)
            and due >= 0.5):
        note += f", incl. {_money(due)} of paychecks still due"
    return note


def bills_hero(st: dict | None, *, bills_monthly: float | None,
               closed: bool = False,
               still_due: dict | None = None,
               envelopes_due: int | None = None) -> list[dict]:
    """The Bills & Income headline as both clients print it, figure and
    words: Income, Bills and what is left after them — the plan's own
    terms (budget.plan_line), so the page says what Budget and Today say
    about the same month. Nothing here is worked out on a client.

    - Bills: the plan line's bills (the evened-out schedule, the figure
      Budget prints); a closed month prints the schedule frozen with it
      (`bills_monthly` from its snapshot), as Budget history does.
    - Income: the plan line's income, named by its source.
    - Left after bills (and savings): to spend + excess, the plan line's
      two terms, said in its note.
    - Still due (live only): Today's one still-due figure,
      the running month's bills breakdown's `still_due`
      (bills_breakdown.running — unpaid occurrences through month end
      plus what is left in the envelopes, and any bill late from an
      earlier month), in Today's words; `still_due` is that breakdown.
      With a pool, its note also names the envelopes half of
      report.month_end_parts (`envelopes_due`), the Envelopes row's "set
      aside to month end" — with still due, the figure the cash tile
      subtracts. (A note, not a figure of its own: an installed client
      knows the headline's four keys only.)
    `st` is the month's month_status (None: no month read); Income and
    Left are left out while the plan has no income."""
    from ..engine.reporting import whole_dollars
    from .report import _money
    plan = (st or {}).get("plan_line")
    rows = []
    if closed:
        if bills_monthly is not None:
            rows.append({"key": "bills", "label": _bills_label(st),
                         "amount": whole_dollars(abs(bills_monthly)),
                         "note": "a month, evened out, as frozen when the "
                                 "month closed", "tone": ""})
    elif plan is not None:
        rows.append({"key": "bills",
                     "label": _bills_label(st),
                     "amount": plan["bills"],
                     "note": "a month, evened out", "tone": ""})
    elif bills_monthly is not None:
        rows.append({"key": "bills", "label": _bills_label(st),
                     "amount": whole_dollars(abs(bills_monthly)),
                     "note": "a month, evened out", "tone": ""})
    due = ([_due_row(still_due, envelopes_due)]
           if still_due is not None and not closed else [])
    if plan is None:
        return rows + due
    # the plan line's order: what comes in, the bills, what is left
    rows.insert(0, {"key": "income", "label": "Income",
                    "amount": plan["income"],
                    "note": _income_note(st or {}, closed=closed),
                    "tone": "pos"})
    left = plan["to_spend"] + plan["excess"]
    rows.append({"key": "left",
                 "label": ("Left after bills and savings"
                           if plan.get("saved") is not None
                           else "Left after bills"),
                 "amount": left,
                 "note": (f"to spend {_money(plan['to_spend'])} + excess "
                          f"{_money(plan['excess'])}"),
                 "tone": "neg" if left < 0 else ""})
    return rows + due


def _bills_label(st: dict | None) -> str:
    """The plan's bills leg as a heading: "Bills & envelopes" when the
    plan has a pool (budget.month_status's plan_bills_label)."""
    label = (st or {}).get("plan_bills_label") or "bills"
    return label[:1].upper() + label[1:]


def _due_row(bd: dict, envelopes_due: int | None = None) -> dict:
    """The Bills headline's still-due tile from the running month's
    breakdown: its still-due figure under the label the Bills row gives
    it ("still due"); with a bill late from an earlier month the note
    gives the two parts in the words the row's table uses."""
    from . import bills_breakdown
    pools = (f" · envelopes {bills_breakdown._money(int(envelopes_due))} "
             "still expected this month" if envelopes_due else "")
    if not bd["late"]:
        return {"key": "due", "label": "Still due",
                "amount": bd["still_due"],
                "note": ("bills not yet paid through month end, as Today's "
                         "Bills row says") + pools,
                "tone": ""}
    late = sum(i["amount"] for i in bd["late"])
    bits = ([f"{bills_breakdown._money(bd['due'])} of bills due this month"]
            if bd["due"] > 0 else [])
    bits.append(f"{bills_breakdown._money(late)} late from "
                f"{bills_breakdown.months_words(bd['late'])}")
    return {"key": "due", "label": "Still due", "amount": bd["still_due"],
            "note": " + ".join(bits) + pools, "tone": ""}


@router.get("/bills")
@documented(response=BillsData)
def bills_list(user: dict = Depends(_user()),
               month: str | None = Query(None)):
    """Proposals + bills — the SPA Bills page's data. Each bill row
    carries health, this month's occurrence/envelope stats (Paid x/y,
    envelope Used/Chgs/Paid), and the ledger-fit hint (💡): the fit summary,
    whether it diverges from the config, and its dismissed flag. `archived`
    lists soft-removed bills (active=0) for the archived section.

    `month=YYYY-MM` reads a PAST month instead: the same stats as that
    month ended (paid x of y, received, envelope used), so the page can
    step back through the year, and that month's own figures — the
    headline (`totals.hero`) from the bill load frozen with it and its
    plan as the Month lens judges it, and `month_calendar`, the month's
    dated money. Nothing of today's rides a closed month: no offers, no
    week ahead. The bill rows and their health are the live schedule (a
    bill added since shows with no occurrence in a month it did not exist
    for). The answer names the month it describes (`month`) and whether
    it is closed (`month_final`)."""
    import calendar as _calendar
    from ..engine import bills, budget
    from ..engine.compat import as_dict
    from .pages import _cadence_label, _proposal_summary
    want_month = None
    if month:
        try:
            yy, mm = (int(x) for x in month.split("-"))
        except (TypeError, ValueError):
            raise HTTPException(400, "month must be YYYY-MM")
        # the shared range check, not a bare dt.date(): year 1 builds a
        # date the month arithmetic below then steps out of (a 500)
        _ck_ym(yy, mm)
        want_month = (yy, mm)
    conn = _conn(user)
    try:
        # payday roll: a received paycheck's Next date
        # advances the moment the page loads, not at the nightly pass —
        # idempotent and income-rows-only, so cheap on a read path.
        # viewers are read-only everywhere else; their page load
        # must not mutate due_on (the nightly detect still rolls for them).
        if user["role"] != "viewer":
            try:
                bills.advance_received_income(conn)
            except Exception:           # noqa: BLE001 — display must render
                import logging
                logging.getLogger("oikonome.bills").exception(
                    "payday roll failed on /api/bills")
        cfg = budget.load_config(conn)
        from .. import localtime
        today = localtime.now_local(cfg).date()
        props = [{"id": p["id"], "kind": p["kind"],
                  "bill_type": p["bill_type"], "payee": p["payee"],
                  "amount": p["amount"],
                  # income proposals carry income=True in evidence;
                  # lets the Recurring UI show an INCOME section vs bills
                  "income": bool((as_dict(p["evidence"]) or {}).get("income")),
                  # an "attach" (fee) or "merchant" proposal names the
                  # bill it would join
                  "bill_payee": (as_dict(p["evidence"]) or {}).get("bill_payee"),
                  "cadence": _cadence_label(p["frequency"], p["interval"],
                                            bills.offer_days(p)),
                  # proposal extras: raw cadence pieces for the
                  # "freq ×N · next date" cell, and the "Looks like" LLM tag
                  "frequency": p["frequency"], "interval": p["interval"],
                  "by_month_day": bills.offer_days(p),
                  "next_due": (p["next_due"].isoformat()
                               if p["next_due"] else None),
                  "upcoming_due": (lambda d: d.isoformat() if d else None)(
                      bills.offer_upcoming_due(p, today)),
                  "llm_tag": p["llm_tag"],
                  # the row's words, written once for both clients
                  **bills.offer_words(p, as_dict(p["evidence"]) or {}),
                  "summary": (bills.merchant_offer_summary(conn, p)
                              if p["kind"] == "merchant"
                              else _proposal_summary(p))}
                 for p in bills.pending_proposals(conn, today)]
        health = bills.analyze_bills(conn, today)
        dismissed = cfg.get("dismissed_hints") or {}
        dismissed_health = cfg.get("dismissed_health") or {}
        disabled_bills = set(cfg.get("disabled_bills") or [])
        # the month the stats describe: this one as of today, or a past
        # one as of its last day — the same reading the Month lens gives
        as_of, month_key, month_final = today, f"{today:%Y-%m}", False
        if want_month and want_month != (today.year, today.month):
            if want_month > (today.year, today.month):
                raise HTTPException(400, "that month has not started")
            yy, mm = want_month
            as_of = dt.date(yy, mm, _calendar.monthrange(yy, mm)[1])
            month_key, month_final = f"{yy:04d}-{mm:02d}", True
        month_events: list | None = [] if month_final else None
        stats = _month_bill_stats(conn, cfg, as_of, closed=month_final,
                                  events=month_events)
        caps = cfg.get("occurrence_caps") or {}
        listed = conn.execute(
            "SELECT payee, amount, frequency, due_on, raw, category, "
            "merchant FROM bills "
            "WHERE active=1 ORDER BY (amount>0), payee").fetchall()
        if month_final:
            # a closed month is shown as it was judged: its snapshot says
            # which bills were budget bills then, and a bill archived since
            # still gets its row, or the rent the Month lens calls paid
            # would vanish from the page that month
            snap_cfg = budget.snapshot_config(conn, yy, mm)
            if snap_cfg is not None:
                disabled_bills = set(snap_cfg.get("disabled_bills") or [])
            frozen = budget.snapshot_bill_rows(conn, yy, mm) or []
            live = {b["payee"] for b in listed}
            listed = list(listed) + sorted(
                ({**r, "frequency": None, "due_on": None} for r in frozen
                 if r["payee"] not in live),
                key=lambda r: ((r["amount"] or 0) > 0, r["payee"]))
        rows = []          # response rows ("bills" key) — `bills` is the module
        for b in listed:
            raw = as_dict(b["raw"])
            rec = raw.get("recurrence")
            if not isinstance(rec, dict):
                rec = {}
            h = health.get(b["payee"]) or {}
            fit = h.get("fit")
            rows.append({"payee": b["payee"],
                         "amount": abs(b["amount"] or 0),
                         "income": (b["amount"] or 0) > 0,
                         "cadence": _cadence_label(
                             rec.get("frequency") or b["frequency"],
                             # envelopes carry their period here, not in
                             # `recurrence` (they have no due dates)
                             budget.envelope_months(raw)
                             if raw.get("bill_type") == "envelope"
                             else rec.get("interval"),
                             rec.get("byMonthDay")),
                         # the days of a rule on several days of the month,
                         # which `cadence_value` cannot say
                         "by_month_day": budget.monthly_rule_days(rec) or None,
                         "due_on": (b["due_on"].isoformat()
                                    if b["due_on"] else None),
                         "health": h.get("health"),
                         # what the ledger last showed for this merchant —
                         # a mismatch with no fit still gets to say why
                         "last_seen": _ser(h["last_seen"]) if h.get("last_seen") else None,
                         # the attention queue's raw material: when the
                         # bill last matched, its cycle, and the ONE action
                         # the evidence supports (see bills._suggest) — the
                         # clients render this verbatim, never invent one
                         "last_match": (h["last_match"].isoformat()
                                        if isinstance(h.get("last_match"),
                                                      (dt.date, dt.datetime))
                                        else h.get("last_match")),
                         "cycle_days": h.get("cycle_days"),
                         "suggestion": h.get("suggestion"),
                         # a dismissed flag stays hidden until the health
                         # itself changes to a different problem
                         # …and to a different ACTION: dismissing "stale"
                         # at one missed cycle must not also hide the
                         # two-cycle Disable offer when it later appears
                         "health_dismissed": (
                             (dismissed_health.get(b["payee"]) or {})
                             .get("health") == h.get("health")
                             and (dismissed_health.get(b["payee"]) or {})
                             .get("action", (h.get("suggestion") or {})
                                  .get("action"))
                             == (h.get("suggestion") or {}).get("action")),
                         # budget-disabled: the row shows no health pill and
                         # no dismiss control, so it must not be counted or
                         # listed as needing attention either
                         "disabled": b["payee"] in disabled_bills,
                         # 💡 pill: show when fit exists, not dismissed, and
                         # health is off or the fit diverges
                         "fit": fit,
                         "fit_diverges": bool(h.get("fit_diverges")),
                         "hint_dismissed": bills.fit_matches_dismissal(
                             fit, dismissed.get(b["payee"])),
                         # the edit-form prefill (/api/bills/bill), carried
                         # on every row so a page of N bills is one request
                         # — the per-bill route stays for single lookups
                         "cadence_value": _bill_cadence(raw),
                         "bill_type": raw.get("bill_type", "occurrence"),
                         "source": raw.get("source", "seed"),
                         "category": b["category"],
                         "merchant": b["merchant"],
                         "cap": caps.get(b["payee"]),
                         "show_today": bool(raw.get("show_today")),
                         "match_category": bool(raw.get("match_category")),
                         "txn_category": raw.get("txn_category") or "",
                         # the merchants the bill pays, by display name
                         "merchants": [x["name"] for x in budget.bill_refs(raw)],
                         # the fees that ride with the payment — shown as
                         # "incl. $1 fee" beside the amount
                         "companions": [{"tokens": c["label"],
                                         "amount": c["amount"],
                                         "window_days": c["window_days"]}
                                        for c in bills.companions_of(raw)],
                         "fee_total": bills.fee_total(raw),
                         # the row's printed amount: a bill and its fees,
                         # an income series' last real deposit
                         # (bills.row_amount)
                         "amount_with_fees": bills.row_amount(
                             b["amount"] or 0, raw, h),
                         **(stats.get(b["payee"]) or _EMPTY_BILL_STATS)})
            # Two dates, two meanings, kept apart. `withdrawn_on` (from the
            # month stats) is THIS month's missed occurrence, and only it may
            # turn the row's status amber; `health_withdrawn_on` is the
            # earlier cycle behind a "withdrawn" health verdict, for the
            # health pill alone. Folding one into the other dressed a later
            # cycle that was never charged — or one not yet due — in last
            # cycle's withdrawal, while Today called it overdue.
            rows[-1]["paid_text"] = paid_words(
                rows[-1], as_of.replace(
                    day=_calendar.monthrange(as_of.year, as_of.month)[1]),
                live=not month_final)
            rows[-1]["health_withdrawn_on"] = (
                h["withdrawn_on"].isoformat() if h.get("withdrawn_on")
                else None)
            rows[-1].update(due_words(rows[-1], today,
                                      live=not month_final))
            rows[-1]["attention_text"] = attention_words(rows[-1])
        archived = [{"payee": r["payee"], "amount": r["amount"],
                     "frequency": r["frequency"],
                     "synced_at": (r["synced_at"].isoformat()
                                   if r["synced_at"] else None)}
                    for r in conn.execute(
                        "SELECT payee, amount, frequency, synced_at "
                        "FROM bills WHERE active=0 "
                        "ORDER BY synced_at DESC NULLS LAST, payee").fetchall()]
        # The headline states what the schedule costs; without it a person
        # would sum thirteen cadence groups by eye. `attention` counts
        # the bills whose health is actually wrong, so the headline can point
        # at the only rows that want a decision.
        totals = budget.recurring_load(conn, today)
        # "withdrawn" is left out on purpose: the bill WAS charged, and a
        # charge the feed took back is a warning to check, not a decision
        still_due = envelopes_due = None
        # the headline in the plan's terms (bills_hero): the live month's
        # own plan, the one Today and Budget read — or a closed month's,
        # judged as the Month lens and the monthly report judge it (its
        # frozen budget and schedule), with the bill load frozen with it
        try:
            if month_final:
                from . import lenses
                st = lenses.month_state(conn, yy, mm, today)
            else:
                memo: dict = {}
                st = budget.month_status(conn, today, cfg=cfg, memo=memo)
                # Today's bills breakdown, read the way gather reads it:
                # the tile prints its still-due part
                from . import bills_breakdown, report
                parts = report.month_end_parts(
                    conn, cfg, today, st["buckets"]["fixed"]["envelopes"],
                    memo=memo)
                still_due = bills_breakdown.running(conn, cfg, st, today,
                                                    memo=memo, parts=parts)
                if st["buckets"]["fixed"]["envelopes"]:
                    envelopes_due = parts["envelopes"]
        except Exception:                # noqa: BLE001 — the list must render
            import logging
            logging.getLogger("oikonome.bills").exception(
                "month plan failed on /api/bills")
            st = None
        if month_final:
            totals["hero"] = bills_hero(
                st, bills_monthly=budget.snapshot_bills_monthly(conn, yy, mm),
                closed=True)
            # a closed month has no week ahead and no offers: those are
            # today's, and the page is about how the month went
            totals["week_total"], totals["week_count"] = 0.0, 0
            props = []
        else:
            totals["hero"] = bills_hero(st, bills_monthly=totals["bills_monthly"],
                                        still_due=still_due,
                                        envelopes_due=envelopes_due)
        # the week ahead in the words both clients print beside "Next 7
        # days" (none when nothing is due in it, and on a closed month)
        from .report import _money
        totals["week_text"] = (
            f"{_money(totals['week_total'])} across {totals['week_count']} "
            f"bill{'' if totals['week_count'] == 1 else 's'}"
            if totals["week_count"] > 0 else None)
        totals["attention"] = sum(
            1 for r in rows
            if queued(r))
        out = {"proposals": props, "bills": rows, "archived": archived,
               "totals": totals, "cadences": CADENCES,
               "month": month_key, "month_final": month_final}
        if month_final:
            out["month_calendar"] = _calendar_days(month_events or [])
        return out
    finally:
        conn.close()


@router.post("/bills/detect",
             dependencies=[Depends(limit("bills_detect", 6, 600))])
@documented(response=BillsDetected)
def bills_detect_api(user: dict = Depends(_user())):
    # the whole-ledger pass: rate-limited and one at a time per household
    from ..engine import bills
    conn = _conn(user)
    try:
        stats = bills.run_single_flight(conn)
    finally:
        conn.close()
    if stats is None:
        raise HTTPException(409, "bill detection is already running")
    return {"proposed_add": stats["proposed_add"],
            "proposed_income": stats.get("proposed_income", 0),
            "drift_applied": stats["drift_applied"]}


def _proposal_superseded(conn, pid: str) -> bool:
    """bills.proposal_superseded: the offer was withdrawn because what it
    offered already exists."""
    from ..engine import bills
    return bills.proposal_superseded(conn, pid)


def _proposal_adjust(adj: Fields | None) -> dict:
    """The fields a person changed on an offer before approving it, read
    and checked the way the bill save checks them, before anything is
    written. A blank or null field is one they left alone."""
    if adj is None:
        return {}
    out: dict = {}
    name = adj.text("name")
    if name:
        out["name"] = name
    amount = adj.money("amount")
    if amount is not None:
        if not amount > 0:
            raise HTTPException(400, "amount must be a positive dollar value")
        out["amount"] = amount
    cadence = adj.text("cadence")
    if cadence:
        out["cadence"] = _parse_cadence(cadence)
    days = adj.list_of("by_month_day", int, 2, msg=_RULE_DAYS_MSG)
    if days is not None:
        out["by_month_day"] = _rule_days_or_400(days)
    day = adj.int_in("due_day", 1, 31, blank_ok=True,
                     msg="due_day must be a day of the month, 1 to 31")
    if day is not None:
        out["due_day"] = day
    return out


def _next_on_day(today: dt.date, day: int, freq: str, interval: int,
                 due: dt.date | None) -> dt.date | None:
    """The due date of a bill whose due day the person set: the day of the
    month moved inside the cycle the bill is already due in (its due date's
    month and year, this month when it has none, the last day of a shorter
    month), then stepped on by the bill's own interval, in years for a
    yearly bill, while it is still before `today`. Stepping by one month
    instead would pull a quarterly bill into next month and a two-yearly
    one into next year, and the plan would count a charge that is not
    coming. A cadence with no day of the month (weekly, daily, one time)
    keeps its date; an envelope has no due date at all."""
    if freq == "ENVELOPE":
        return None
    if freq not in ("MONTHLY", "YEARLY"):
        return due
    step = max(1, int(interval or 1)) * (12 if freq == "YEARLY" else 1)
    base = due or today
    n = base.year * 12 + base.month - 1

    def on(n: int) -> dt.date:
        y, m = divmod(n, 12)
        return dt.date(y, m + 1, min(day, calendar.monthrange(y, m + 1)[1]))
    d = on(n)
    if d < today:
        # whole cycles to the first one on or after today, not a loop: a
        # long-stale due date must not cost one iteration per cycle
        behind = (today.year * 12 + today.month - 1) - n
        n += max(0, behind // step) * step
        d = on(n)
        if d < today:
            d = on(n + step)
    return d


def _adjust_approved_bill(conn, payee: str, adj: dict) -> str:
    """Apply what the person changed to the bill an approval just created,
    inside the approval's transaction, and return the bill's name.

    The approval creates the bill exactly as an unadjusted one would, so
    the merchants it was found from and the history it was read from are
    attached by the same code. The changes are then an edit of that bill:
    a rename takes everything that names it along, and the schedule save
    carries the identity forward. A changed amount counts as a hand edit,
    so the drift check leaves it alone for its grace window."""
    try:
        return _adjust_bill_now(conn, payee, adj)
    except psycopg.errors.UniqueViolation:
        # the existence checks and the writes are separate statements, so a
        # rename or a save racing onto the same name can still trip the
        # (tenant_id, id) key at the write: the same answer as a name
        # already taken, and the caller's transaction leaves the offer
        # pending, not approved under values the person did not choose
        raise HTTPException(
            409, f"a bill named {adj.get('name') or payee!r} already exists")


def _adjust_bill_now(conn, payee: str, adj: dict) -> str:
    from .. import localtime
    from ..engine import bills
    from ..engine.compat import as_dict
    new = adj.get("name") or payee
    if new != payee:
        # the offer was checked against the bills already tracked under its
        # own name; a new name is checked again, by the token it would
        # match on, so a rename cannot slip a second tracker onto charges
        # another bill already claims
        by = bills.covering_bill(conn, new, besides=payee)
        if by is not None:
            raise HTTPException(409, f"'{by}' already tracks these charges")
        # an archived bill's name, or a spelling of it, is refused as the
        # Bills page's rename refuses it: rename_bill counts every bill
        if (bills.get_bill(conn, new) is not None
                or bills.rename_bill(conn, payee, new) == 0):
            raise HTTPException(409, f"a bill named {new!r} already exists")
    if not any(k in adj for k in ("amount", "cadence", "due_day",
                                  "by_month_day")):
        return new
    bill = conn.execute("SELECT * FROM bills WHERE payee = %s FOR UPDATE",
                        (new,)).fetchone()
    raw = as_dict(bill["raw"])
    if "cadence" in adj:
        freq, interval = adj["cadence"]
    else:
        f, _, i = _bill_cadence(raw).partition(":")
        freq, interval = f.upper(), int(i or 1)
    days = adj.get("by_month_day")
    if days and (freq, interval) != ("MONTHLY", 1):
        raise HTTPException(400, _DAYS_NEED_MONTHLY)
    rec = raw.get("recurrence") or {}
    bmd = rec.get("byMonthDay") if isinstance(rec, dict) else None
    # the day a monthly bill is anchored to, kept unless the person moved
    # it: re-read from a due date clamped into a short month it would slide
    # a bill due on the 31st to the 30th for good
    anchor = (bmd[0] if isinstance(bmd, list) and bmd
              and isinstance(bmd[0], int) else None)
    next_due = bill["due_on"]
    if "due_day" in adj:
        next_due = _next_on_day(localtime.household_day(conn),
                                adj["due_day"], freq, interval,
                                bill["due_on"])
        anchor = adj["due_day"]
        # a 409, as every refusal about what the person typed in this form
        # is: the wizard shows it where they are typing
        off = bills.due_off_rule(raw, freq, interval, days, next_due)
        if off:
            raise HTTPException(409, off)
    try:
        bills.save_bill(
            conn, payee=new,
            amount=adj.get("amount") or abs(bill["amount"] or 0),
            bill_type="envelope" if freq == "ENVELOPE" else "occurrence",
            frequency=None if freq in ("ONE_TIME", "ENVELOPE") else freq,
            interval=interval, next_due=next_due,
            category=bill["category"],
            source=raw.get("source", "detected"),
            last_seen=raw.get("lastDueOn"),
            by_month_day=days or (anchor if freq == "MONTHLY" else None),
            income=(bill["amount"] or 0) > 0,
            manual_touch="amount" in adj)
    except ValueError as e:
        raise HTTPException(400, str(e))
    # what the bill matches moved with its amount: stamp its rows now, as
    # the bill save does, not on the next nightly pass
    bills.apply_txn_categories(conn, bill_id=bills._slug(new))
    return new


def _hold_bill_claims(conn) -> None:
    """The household's bill-claim lock (bills.hold_bill_claims), held
    here across the approval AND its adjustment, so a rename onto another
    offer's charges is inside the same turn."""
    from ..engine import bills
    bills.hold_bill_claims(conn)


def _decide_proposal(conn, pid: str, action: str, adj: dict) -> dict:
    """Approve or dismiss one offer; an approval applies `adj` too."""
    from ..engine import bills
    if action != "approve":
        return bills.apply_proposal(conn, pid, action)
    # one transaction: an adjustment that cannot be applied (the new name
    # is taken) leaves the offer pending, not approved under the values
    # the person just declined; and the claim lock is held until the bill
    # this approval makes is committed
    with conn.transaction():
        _hold_bill_claims(conn)
        if adj:
            p = conn.execute(
                "SELECT kind, status FROM bill_proposals WHERE id = %s "
                "FOR UPDATE", (pid,)).fetchone()
            if p is not None and p["status"] == "pending" \
                    and p["kind"] != "add":
                raise HTTPException(
                    400, "only an offer to add a bill or income can be "
                         "adjusted")
        r = bills.apply_proposal(conn, pid, action, roll=not adj)
        if adj and not r.get("error"):
            r["approved"] = _adjust_approved_bill(conn, r["approved"], adj)
            # a date already behind today moves on by the adjusted cadence
            bills.roll_passed_due(conn, r["approved"])
    return r


@router.post("/bills/proposal")
@documented(response=ProposalAnswered, body=ProposalAnswer)
def bills_proposal_api(user: dict = Depends(_user()),
                       body: Fields = Depends(json_fields)):
    """Approve or dismiss one offer. An approval may carry `adjust`
    ({name, amount, cadence, due_day}, each optional) — what the person
    changed on an offer to add a bill before saying yes; the bill is
    created with those values and keeps everything else the offer found.
    `adjust` is ignored on a dismissal."""
    action = body.one_of("action", ("approve", "reject"), required=True,
                         msg="action must be approve or reject")
    adj = _proposal_adjust(body.sub("adjust")) if action == "approve" else {}
    pid = body.ident("pid")
    conn = _conn(user)
    try:
        r = _decide_proposal(conn, pid, action, adj)
        if not r.get("error"):
            _log_proposal(conn, user, pid, action, r)
        superseded = bool(r.get("error")) and _proposal_superseded(conn, pid)
    finally:
        conn.close()
    if superseded:
        # the offer is gone (the bill it offered is already tracked), so a
        # client drops its card rather than keep offering a dead button
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": r["error"], "superseded": True},
                            status_code=400)
    if r.get("error"):
        raise HTTPException(400, r["error"])
    out = {"ok": True, "payee": r.get("approved") or r.get("rejected")}
    if r.get("note"):
        # an approve can undo something the person set (a pinned amount);
        # the client shows this so the change is never silent
        out["note"] = r["note"]
    return out


@router.post("/bills/proposals/approve-all")
@documented(response=ProposalsApproved, body=ProposalsApproveAll)
def bills_proposals_approve_all(user: dict = Depends(_user()),
                                body: Fields = Depends(json_fields_or_empty)):
    """Approve every pending proposal in one click — the setup wizard's
    bills step, where the finder's first pass over a fresh history is
    usually right and a person should not have to click approve twenty
    times. `pids` names the offers the client showed: a detection pass
    can file new offers (or change which twin is listed) between the
    page loading and the click, and the button approves what it counted,
    never those. A shown offer no longer listed is skipped. A client that
    sends no `pids` (an older one) approves every listed offer, as it
    always did. Each proposal goes through the same locked
    apply as a single approve, so a twin click or a concurrent single
    approve cannot double-create; one that fails to apply (slug
    collision, bad amount) stays pending and is reported, the rest still
    land."""
    from ..engine import bills
    shown = body.list_of("pids", str, 2000)
    conn = _conn(user)
    try:
        # the offers the page lists (bills.pending_proposals), oldest
        # first: one left out of the list (stale, about a bill that is no
        # longer active, a card payment, a twin of a listed one) is not
        # approved by a button that counted only what it showed. Nor is
        # one an active bill covers tried: it is no offer the person saw,
        # so it is neither approved nor reported as one that stayed
        # pending; the nightly retires it.
        listed = bills.pending_proposals(conn)
        if shown is not None:
            keep = set(shown)
            listed = [p for p in listed if p["id"] in keep]
        pids = [p["id"] for p in sorted(
            listed, key=lambda p: (p["created_at"] or dt.datetime.min.replace(
                tzinfo=dt.timezone.utc), p["id"]))]
        approved, failed, notes = [], [], []
        # each approval commits on its own (one that fails stays pending,
        # the rest land); the new bills' starts are read from the ledger
        # once, after the last
        with bills.starts_filled_once(conn):
            for pid in pids:
                r = _decide_proposal(conn, pid, "approve", {})
                if r.get("error"):
                    failed.append({"pid": pid, "error": r["error"],
                                   "superseded": _proposal_superseded(
                                       conn, pid)})
                else:
                    approved.append(r.get("approved"))
                    if r.get("note"):
                        notes.append({"payee": r.get("approved"),
                                      "note": r["note"]})
                    _log_proposal(conn, user, pid, "approve", r)
    finally:
        conn.close()
    return {"ok": True, "approved": len(approved), "payees": approved,
            "failed": failed, "notes": notes}


# ---- recurring management (bill CRUD — /bills/{save,delete,restore,
#      toggle,hint} + /bills/history as JSON) --------------------------------

# cadence select values: "<FREQ>:<interval>" | "ONE_TIME:1" | "ENVELOPE:1"
CADENCES = [("DAILY:1", "daily"),
            ("WEEKLY:1", "weekly"), ("WEEKLY:2", "every 2 weeks"),
            ("DAILY:14", "every 14 days"), ("MONTHLY:1", "monthly"),
            ("WEEKLY:8", "every 8 weeks"), ("MONTHLY:2", "every 2 months"),
            ("MONTHLY:3", "every 3 months"), ("MONTHLY:6", "every 6 months"),
            ("YEARLY:1", "yearly"), ("YEARLY:2", "every 2 years"),
            ("ONE_TIME:1", "one-time"),
            ("ENVELOPE:1", "envelope (monthly pool)"),
            ("ENVELOPE:12", "envelope (annual pool)")]


_DAYS_NEED_MONTHLY = "by_month_day goes with a monthly cadence"
_RULE_DAYS_MSG = ("by_month_day must be one or two days of the month, "
                  "1 to 31 or -1 for the last day")


def _rule_days_or_400(value) -> list[int]:
    """The days of the month a client names for a monthly rule: one or two,
    each 1 to 31 or -1 for the last day. Naming them is what changes a
    rule's days, so anything else is refused, never read as a guess."""
    if (not isinstance(value, list) or not 1 <= len(value) <= 2
            or any(isinstance(d, bool) or not isinstance(d, int)
                   or not (d == -1 or 1 <= d <= 31) for d in value)):
        raise HTTPException(400, _RULE_DAYS_MSG)
    return list(dict.fromkeys(value))


def _parse_cadence(value: str) -> tuple[str, int]:
    """A CADENCES value ("<FREQ>:<interval>") → (FREQ, interval), or a 400.
    Both doors that write a bill's schedule read it here: the bill save and
    an approved offer the person adjusted first."""
    from ..engine import bills
    freq, _, iv = str(value or "").partition(":")
    freq = freq.strip().upper()
    # Allowlist the frequency BEFORE any mutation: an unknown token (e.g.
    # "HOURLY") is not ONE_TIME/ENVELOPE, so it reaches PER_YEAR[freq] in
    # recurring._monthly and raises KeyError → a 500 that lands AFTER the
    # rename/config migration has already committed (partial mutation).
    if freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY",
                    "ONE_TIME", "ENVELOPE"):
        raise HTTPException(400, "bad cadence frequency")
    try:
        interval = int(iv or 1)
    except (TypeError, ValueError):
        raise HTTPException(400, "bad cadence interval")
    # Bound the cycle, not the count: an unbounded value later overflows
    # dt.timedelta in the occurrence expansion (a persistent 500 on every
    # Today render), and a count that is modest for MONTHLY is centuries for
    # YEARLY — the nightly roll would carry the due date past any date the
    # product can read. Twenty years is well past any real bill cadence.
    if interval < 1 or bills.cadence_too_long(freq, interval):
        raise HTTPException(400, "cadence interval out of range")
    return freq, interval


def _bill_cadence(raw: dict) -> str:
    """A bill's raw config → the CADENCES select value (edit-form prefill)."""
    from ..engine.budget import envelope_months
    if raw.get("bill_type") == "envelope":
        return f"ENVELOPE:{envelope_months(raw)}"
    rec = raw.get("recurrence")
    if not isinstance(rec, dict) or not rec.get("frequency"):
        return "ONE_TIME:1"
    return f"{str(rec['frequency']).upper()}:{rec.get('interval') or 1}"


def _save_payee_config(conn, mutate) -> None:
    """Load config, apply `mutate(cfg) -> changed`, save if changed.

    Under the row lock: every caller here is a
    read-modify-write of the shared settings blob, which is the exact shape
    that silently discards a concurrent save."""
    from ..engine import budget
    with budget.config_txn(conn) as cfg:
        mutate(cfg)


@router.get("/bills/bill")
@documented(response=BillDetail)
def bills_bill_api(user: dict = Depends(_user()), payee: str = ""):
    """One bill's editable fields (the edit-row prefill), plus its
    payee-keyed config: occurrence cap + budget-disabled flag."""
    from ..engine import bills, budget
    from ..engine.compat import as_dict
    conn = _conn(user)
    try:
        row = bills.get_bill(conn, payee)
        if row is None:
            raise HTTPException(404, "no such bill")
        raw = as_dict(row["raw"])
        cfg = budget.load_config(conn)
    finally:
        conn.close()
    return {"payee": row["payee"],
            "amount": abs(row["amount"] or 0),
            "income": (row["amount"] or 0) > 0,
            "cadence": _bill_cadence(raw),
            "by_month_day": budget.monthly_rule_days(
                raw.get("recurrence") if isinstance(raw.get("recurrence"), dict)
                else {}) or None,
            "next_due": row["due_on"].isoformat() if row["due_on"] else "",
            "bill_type": raw.get("bill_type", "occurrence"),
            "source": raw.get("source", "seed"),
            "category": row["category"],
            "merchant": row["merchant"],
            "active": bool(row["active"]),
            "cap": (cfg.get("occurrence_caps") or {}).get(row["payee"]),
            "disabled": row["payee"] in (cfg.get("disabled_bills") or []),
            "show_today": bool(raw.get("show_today")),
            "match_category": bool(raw.get("match_category")),
            # the transaction category stamped on the rows this bill
            # matches ("" = none — rows keep the merchant rule)
            "txn_category": raw.get("txn_category") or "",
            "merchants": [x["name"] for x in budget.bill_refs(raw)],
            "companions": [{"tokens": c["label"], "amount": c["amount"],
                            "window_days": c["window_days"]}
                           for c in bills.companions_of(raw)],
            "fee_total": bills.fee_total(raw),
            "cadences": CADENCES}


class _ArchivedBillExists(Exception):
    """An add named a bill that is archived; the save writes nothing."""

    def __init__(self, payee: str):
        super().__init__(payee)
        self.payee = payee


@router.post("/bills/save")
@documented(response=BillSaved, body=BillSave)
def bills_save_api(user: dict = Depends(_user()),
                   body: Fields = Depends(json_fields)):
    """Add or edit a bill.

    Body: payee, amount (positive $), cadence "<FREQ>:<interval>" (see
    CADENCES), by_month_day (a monthly rule's days; present only when the
    person changed them, since absent keeps the bill's own), next_due (ISO
    date or ""; a typed date off a several-day rule is a 400), income
    (bool), orig_payee (set on rename), cap (int occurrence cap; ""/null clears; only touched when the
    key is present), merchant (match key for NEW bills only), merchants
    (the display names of the merchants the bill pays — its identity),
    show_today / match_category (bools) and category (string) — each only
    touched when its key is present. An add (no orig_payee) over an
    archived bill's name answers 409 {detail, archived: true, payee} unless
    the body carries restore: true, which brings the bill back with the
    values sent."""
    from ..engine import bills, budget
    from ..engine.compat import as_dict
    payee = body.text("payee")
    if not payee:
        raise HTTPException(400, "payee required")
    amount = body.money("amount", default=0.0)
    # the money check only rejects NaN/Inf; save_bill separately requires a
    # POSITIVE amount, but it runs AFTER the rename/config migration commits
    # (autocommit connection, no transaction). Reject a non-positive amount
    # up front so a failing edit never leaves a half-applied rename behind.
    if not amount > 0:
        raise HTTPException(400, "amount must be a positive dollar value")
    freq, interval = _parse_cadence(
        body.text("cadence", default="MONTHLY:1"))
    orig_payee = body.text("orig_payee")
    # validated before any mutation. A cap is an occurrence COUNT; a
    # non-positive value is used as a Python slice bound in
    # budget._occurrences (expanded[:cap]), so a negative silently drops
    # occurrences and can flip the verdict. ""/null clears it.
    cap_n = body.int_in("cap", 1, text_ok=True,
                        msg="cap must be a positive integer")
    # pin-to-Today / category-pool flags: only touched when the key is
    # present (absent = keep the bill's current setting, same contract as
    # `cap`; null reads as false); category likewise, so a plain edit never
    # strips detection's category from the row
    show_today_v = (body.flag("show_today", default=False)
                    if body.present("show_today") else None)
    match_cat_v = (body.flag("match_category", default=False)
                   if body.present("match_category") else None)
    category_v = None
    if body.present("category"):
        cat_s = body.text("category")
        if (len(cat_s) > data.CATEGORY_MAX_LEN
                and not _category_long_ok(user, cat_s)):
            raise HTTPException(400, "category too long")
        category_v = cat_s or None
    # the TRANSACTION category the bill stamps on matched rows: only touched
    # when the key is present; "" clears it (and reverts the rows)
    txn_cat_v = None
    # present = replace the bill's companion fees ([] clears them)
    companions_v = None
    if body.present("companions"):
        companions_v = []
        _msg = "companions must be a list of up to 8 fees"
        sent = body.list_of("companions", Fields, 8, msg=_msg)
        if sent is None:
            raise HTTPException(400, _msg)
        for c in sent:
            # both bill editors cap their fee input at this same 80
            label = c.text("tokens", 80,
                           msg="a fee's words on the charge are limited "
                               "to 80 characters")
            if not label:
                raise HTTPException(400, "companion needs a short description")
            famt = c.money("amount", default=0.0,
                           msg="companion amount must be a number")
            if not 0 < famt <= bills.COMPANION_MAX_AMOUNT:
                raise HTTPException(
                    400, f"a companion fee is between $0.01 and "
                         f"${bills.COMPANION_MAX_AMOUNT:.0f}")
            win = c.int_in("window_days", 0, 14,
                           default=bills.COMPANION_WINDOW_DAYS, text_ok=True,
                           # both clients send Number(text) of the field
                           truncate=True,
                           msg="companion window is 0–14 days")
            companions_v.append({"tokens": label, "amount": famt,
                                 "window_days": win})
    if body.present("txn_category"):
        txn_cat_v = body.text("txn_category")
        if (len(txn_cat_v) > data.CATEGORY_MAX_LEN
                and not _category_long_ok(user, txn_cat_v)):
            raise HTTPException(400, "txn_category too long")
        # never a FLOW category: stamping TRANSFER_OUT / INCOME / LOAN
        # PAYMENTS on a bill's charges would silently drop them from spend
        # (the verdict reads the override) — the spend-exclusion machinery
        # decides flow, not a bill; the row picker refuses these too
        from ..engine import categories as _cats
        if txn_cat_v in _cats.FLOW:
            raise HTTPException(
                400, "a bill cannot categorize its charges as a transfer, "
                     "income or loan payment")
    # the merchants the bill pays, by display name — only touched when the
    # key is present (absent = keep the bill's identity), as for `cap`
    merchants_v = None
    if body.present("merchants"):
        merchants_v = []
        _msg = "merchants must be a list of up to 20 names"
        sent = body.list_of("merchants", str, 20, msg=_msg)
        if sent is None:
            raise HTTPException(400, _msg)
        for m in sent:
            # uncapped: a chip must name a merchant the ledger already has
            # (checked below), so it writes no new name, and an imported
            # descriptor over the rename cap is a real merchant a bill pays
            _merchant_name_or_400("merchant", m, max_len=None)
            if m.strip():
                merchants_v.append(m.strip())
    days_v = body.list_of("by_month_day", int, 2, msg=_RULE_DAYS_MSG)
    if days_v is not None:
        days_v = _rule_days_or_400(days_v)
    # Validate next_due up front too: save_bill parses it via as_date only
    # AFTER the rename commits, so a malformed date — or a year outside the
    # range as_date enforces — would otherwise leave a half-applied rename
    # behind (same partial-mutation window as amount).
    next_due_d = body.date("next_due", msg="next_due must be an ISO date")
    next_due_in = next_due_d.isoformat() if next_due_d else ""
    conn = _conn(user)
    try:
        if orig_payee and bills.get_bill(conn, orig_payee) is None:
            # The form carries the bill's name as the list showed it, and
            # names are read trimmed here, so a stored name that carries
            # stray whitespace never matches by string and the edit reads
            # as "deleted" though the row is right there. The slug ignores
            # whitespace: resolve the row by it and edit under the name
            # the row really holds — which makes this save a rename onto
            # the trimmed name, so the stray whitespace leaves with it.
            stored = conn.execute("SELECT payee FROM bills WHERE id=%s",
                                  (bills._slug(orig_payee),)).fetchone()
            if stored is not None and (stored["payee"] or "").strip() == orig_payee:
                orig_payee = stored["payee"]
        # what the bill was before this edit, for the log's sentence — read
        # under the name the client edited (a rename moves it below)
        _was = _activity.bill_snapshot(
            bills.get_bill(conn, orig_payee or payee))
        if merchants_v:
            # A chip must name a merchant this ledger HAS. A name nothing
            # answers to is stored as a ref with no id, and an identity
            # whose refs resolve to nothing matches NOTHING: the bill stops
            # being paid — no history, no stamped rows, an unpaid month —
            # while the form shows the chip accepted. Resolved exactly as
            # the write path resolves it (budget.resolve_refs), so any
            # casing the picker offers passes. A name the bill ALREADY
            # carries is let through: a bill that arrived from another
            # instance can name a merchant this instance has never seen,
            # and an edit to the
            # amount or the date must not be refused because of it.
            prev = bills.get_bill(conn, orig_payee or payee)
            have = {r["name"].lower()
                    for r in budget.bill_refs(prev["raw"] if prev else None)}
            added = [m for m in merchants_v if m.lower() not in have]
            known = {r["name"].lower() for r in budget.resolve_refs(
                conn, [{"id": None, "name": m} for m in added]) if r["id"]}
            for m in added:
                if m.lower() not in known:
                    raise HTTPException(400, f"no merchant named {m!r}")
        # The edit form names the bill it was opened on (`orig_payee`), so
        # under that bill's row lock the save can tell that it was archived
        # or deleted since: an archived bill stays out of the plan until it
        # is restored, and a deleted one is not quietly recreated by the
        # insert path. The queue's amount accept sends `orig_payee` too, so
        # it is an edit and never brings an archived bill back. A save
        # without `orig_payee` is an add. An add whose name an ARCHIVED
        # bill already holds would otherwise write into that row and leave
        # it archived while the form reports the bill added, so it is
        # refused with `archived: true` unless the body says
        # `restore: true` — the explicit ask to bring the bill back with
        # the values just entered. The rename, the bill write and these
        # checks commit together, so a refusal leaves no half-applied
        # rename behind either.
        renamed = bool(orig_payee and orig_payee != payee)
        revive = False
        with conn.transaction():
            # first, as an approval takes it: a bill added, renamed or
            # re-pointed here while an approval is between its check for a
            # bill claiming these charges and its commit would be invisible
            # to that check
            _hold_bill_claims(conn)
            if not orig_payee:
                held = conn.execute(
                    "SELECT payee, active FROM bills WHERE id=%s FOR UPDATE",
                    (bills._slug(payee),)).fetchone()
                if held is not None and not held["active"]:
                    if body.flag("restore", default=False) is not True:
                        raise _ArchivedBillExists(held["payee"] or payee)
                    revive = True
            if orig_payee:
                if renamed:
                    # the rename's own lock order — pending proposals first,
                    # then the bill — so taking the bill here cannot invert
                    # the order an approval locks them in
                    conn.execute(
                        "SELECT id FROM bill_proposals WHERE status='pending' "
                        "AND (payee=%s OR (kind IN ('attach','merchant') "
                        "AND evidence->>'bill_payee'=%s)) FOR UPDATE",
                        (orig_payee, orig_payee))
                held = conn.execute(
                    "SELECT active FROM bills WHERE payee=%s FOR UPDATE",
                    (orig_payee,)).fetchone()
                if held is None:
                    raise HTTPException(
                        409, "this bill was deleted after the form was "
                             "opened — add it again to keep tracking it")
                if not held["active"]:
                    raise HTTPException(
                        409, "this bill was archived after the form was "
                             "opened — restore it to edit it")
            # rename: move the row (its payee-keyed config follows below).
            # A rename onto an EXISTING different bill aborts the whole save —
            # falling through would overwrite the unrelated target.
            if renamed:
                # the existence check and the UPDATE are two statements; two
                # renames racing onto one name both pass the check and the
                # loser trips the (tenant_id, id) primary key — same answer
                try:
                    taken = (bills.get_bill(conn, payee) is not None
                             or bills.rename_bill(conn, orig_payee, payee) == 0)
                except psycopg.errors.UniqueViolation:
                    taken = True
                if taken:
                    raise HTTPException(
                        409, f"a bill named {payee!r} already exists")
            old = bills.get_bill(conn, payee)
            if not body.present("cadence") and old is not None:
                # an amount-only edit (the attention queue's Accept from a
                # client whose row lacks cadence_value) must not silently turn
                # a yearly bill monthly through the default — keep its own
                _f, _, _i = _bill_cadence(as_dict(old["raw"])).partition(":")
                # raw can arrive verbatim from a restore ZIP: only a cadence
                # that would have passed the explicit path's own checks is
                # carried forward, else the parsed default stands
                try:
                    _n = int(_i or 1)
                except (TypeError, ValueError):
                    _n = 0
                if (_f.upper() in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY",
                                   "ENVELOPE", "ONE_TIME")
                        and 1 <= _n <= 240
                        # the count cap is not the length cap: a legacy
                        # YEARLY:21+ would make save_bill refuse the Accept
                        # until a new cadence was picked by hand
                        and not bills.cadence_too_long(_f.upper(), _n)):
                    freq, interval = _f.upper(), _n
            if days_v and (freq, interval) != ("MONTHLY", 1):
                raise HTTPException(400, _DAYS_NEED_MONTHLY)
            # inside the transaction, so a refusal takes the rename with it
            off_rule = bills.due_off_rule(
                as_dict(old["raw"]) if old is not None else {}, freq, interval,
                days_v, next_due_in or None)
            if off_rule:
                raise HTTPException(400, off_rule)
            src, last_seen = "manual", None
            if old is not None:
                old_raw = as_dict(old["raw"])
                src = old_raw.get("source", "manual")
                # keep the series' history floor — editing must not re-floor the
                # bill as brand-new
                last_seen = old_raw.get("lastDueOn")
            # New bill: pin its match key from the merchant it was added from or,
            # failing that, the payee itself — so it matches the ledger
            # immediately. Edits pass None so save_bill keeps the derived merchant.
            merchant_val = (None if old is not None
                            else body.text("merchant", default=payee))
            # Direction: an explicit flag wins. Absent, an existing bill
            # (archived included) keeps its own, as it keeps its cadence and
            # category; a new bill takes the direction its payee's history
            # shows — the same majority vote the history page renders as
            # "received" — so adding an employer from its own history page
            # saves income, not an expense that would match no row and read
            # as an unpaid paycheck-sized charge every cycle.
            if body.present("income"):
                income_v = bool(body.flag("income", default=False))
            elif old is not None:
                income_v = (old["amount"] or 0) > 0
            else:
                income_v = bool(bills.merchant_history(
                    conn, merchant_val or payee, months=None)["inflow"])
            try:
                bills.save_bill(
                    conn, payee=payee, amount=amount,
                    bill_type="envelope" if freq == "ENVELOPE" else "occurrence",
                    frequency=None if freq in ("ONE_TIME", "ENVELOPE") else freq,
                    interval=interval,
                    next_due=next_due_in or None,
                    by_month_day=days_v,
                    source=src,
                    category=(category_v if body.present("category")
                              else old["category"] if old is not None else None),
                    last_seen=last_seen, income=income_v,
                    merchant=merchant_val,
                    manual_touch=True,  # human edit → drift grace
                    show_today=show_today_v, match_category=match_cat_v,
                    txn_category=txn_cat_v, companions=companions_v,
                    merchants=merchants_v, revive=revive)
            except ValueError as e:     # $0 amount / slug collision / bad date
                raise HTTPException(400, str(e))
        # (a rename moved its payee-keyed settings inside bills.rename_bill)
        # (re)stamp this bill's rows right away — an edit that sets or
        # changes the transaction category should show on the ledger on
        # the next load, not after the nightly pass. Also on a plain edit:
        # a changed amount changes what the bill matches.
        bills.apply_txn_categories(conn, bill_id=bills._slug(payee))
        if body.present("cap"):
            def _set_cap(cfg):
                caps = cfg.get("occurrence_caps") or {}
                if cap_n is not None:
                    caps[payee] = cap_n
                elif payee in caps:
                    caps.pop(payee)
                else:
                    return False
                if caps:
                    cfg["occurrence_caps"] = caps
                else:
                    cfg.pop("occurrence_caps", None)
                return True
            _save_payee_config(conn, _set_cap)
        _now = _activity.bill_snapshot(bills.get_bill(conn, payee))
        _activity.record(conn, user, "bill",
                         "restored" if revive
                         else "edited" if _was is not None else "added",
                         target=payee, label=payee,
                         detail={"before": _was, "after": _now})
        return ({"ok": True, "payee": payee, "restored": True} if revive
                else {"ok": True, "payee": payee})
    except _ArchivedBillExists as e:
        return JSONResponse(status_code=409, content={
            "detail": f"an archived bill named {e.payee!r} already exists — "
                      "restore it to track it again",
            "archived": True, "payee": e.payee})
    finally:
        conn.close()


@router.post("/bills/delete")
@documented(response=BillDeleted, body=BillDelete)
def bills_delete_api(user: dict = Depends(_user()),
                     body: Fields = Depends(json_fields)):
    """mode=archive (default): soft-remove, restorable, keeps payee config.
    mode=purge: hard delete (from the archived list) + drop payee-keyed
    config leftovers (bills.PAYEE_KEYED_CONFIG)."""
    from ..engine import bills
    payee = body.text("payee")
    if not payee:
        raise HTTPException(400, "payee required")
    mode = body.one_of("mode", ("archive", "purge"), default="archive")
    conn = _conn(user)
    try:
        if mode == "purge":
            # drops the payee-keyed config with the row
            n = bills.delete_bill(conn, payee)
        else:
            n = bills.archive_bill(conn, payee)
        # a bill that is gone (or resting) lets go of the rows it categorized
        bills.apply_txn_categories(conn, bill_id=bills._slug(payee))
        if n:
            _activity.record(conn, user, "bill",
                             "deleted" if mode == "purge" else "archived",
                             target=payee, label=payee)
        return {"ok": True, "mode": mode, "affected": n}
    finally:
        conn.close()


@router.post("/bills/restore")
@documented(response=BillRestored, body=BillPayee)
def bills_restore_api(user: dict = Depends(_user()),
                      body: Fields = Depends(json_fields)):
    from ..engine import bills
    payee = body.text("payee")
    if not payee:
        raise HTTPException(400, "payee required")
    conn = _conn(user)
    try:
        try:
            n = bills.restore_bill(conn, payee)
        except ValueError as e:     # past the active-bill ceiling
            raise HTTPException(400, str(e))
        # back in service: its transaction category applies again
        bills.apply_txn_categories(conn, bill_id=bills._slug(payee))
        if n:
            _activity.record(conn, user, "bill", "restored",
                             target=payee, label=payee)
        return {"ok": True, "affected": n}
    finally:
        conn.close()


@router.post("/bills/toggle")
@documented(response=BillToggled, body=BillToggle)
def bills_toggle_api(user: dict = Depends(_user()),
                     body: Fields = Depends(json_fields)):
    """Flip a bill's budget-disabled flag (config `disabled_bills`) — the
    bill row stays; it's just excluded from the budget math."""
    from ..engine import bills
    payee = body.text("payee", max_len=MAX_DESCRIPTOR)
    if not payee:
        raise HTTPException(400, "payee required")
    state = {}
    conn = _conn(user)
    try:
        # an explicit target state makes a retried or duplicated request
        # idempotent — a bare flip would net a double-click to no change
        want = body.flag("disabled")
        # only a bill the household has can be paused: the list is the
        # settings document's, read on every request, so a payee no bill
        # carries would be stored for nothing. Resuming one already in the
        # list stays possible after its bill is gone.
        is_bill = bills.get_bill(conn, payee) is not None

        def _flip(cfg):
            dis = list(cfg.get("disabled_bills") or [])
            to_disabled = (payee not in dis) if want is None else want
            if to_disabled and payee not in dis and not is_bill:
                raise HTTPException(404, "no such bill")
            state["changed"] = to_disabled != (payee in dis)
            if not to_disabled:
                dis = [p for p in dis if p != payee]
                state["disabled"] = False
            elif payee not in dis:
                dis.append(payee)
                state["disabled"] = True
            else:
                state["disabled"] = True
            if dis:
                cfg["disabled_bills"] = dis
            else:
                cfg.pop("disabled_bills", None)
            return True
        _save_payee_config(conn, _flip)
        if state.get("changed"):
            _activity.record(conn, user, "bill",
                             "paused" if state["disabled"] else "resumed",
                             target=payee, label=payee)
        return {"ok": True, "payee": payee, "disabled": state["disabled"]}
    finally:
        conn.close()


# A household has a handful of payers; the word per payer lives in the
# settings document every request reads, so the count is bounded.
PAYER_WORDS_MAX = 500


@router.post("/income/payer")
@documented(response=IncomePayerSaved, body=IncomePayerSet)
def income_payer_api(user: dict = Depends(_user()),
                     body: Fields = Depends(json_fields)):
    """The household's word on one payer: its deposits count as paychecks,
    never do, or (`auto`) the rules decide (specs/paychecks.md). One key
    and one word per call, so a stale tab cannot overwrite the word on
    another payer."""
    from ..engine import paychecks
    key = body.text("key")
    word = body.text("count_as")
    # read before any write: a refused body must change nothing
    label = (body.text("name", max_len=200) or key)[:120]
    if not paychecks.valid_key(key):
        raise HTTPException(400, "key must be a payer key (letters and "
                                 "digits)")
    if word not in ("paycheck", "never", "auto"):
        raise HTTPException(400, "count_as must be paycheck, never or auto")
    _may_edit(user)
    state = {}
    conn = _conn(user)
    try:
        def _apply(cfg):
            words = paychecks.overrides(cfg)
            before = words.get(key)
            if (word != "auto" and key not in words
                    and len(words) >= PAYER_WORDS_MAX):
                raise HTTPException(
                    409, f"at most {PAYER_WORDS_MAX} payers can carry a "
                         "word — set some back to auto first")
            if word == "auto":
                words.pop(key, None)
            else:
                words[key] = word == "paycheck"
            state["changed"] = words.get(key) != before
            if words:
                cfg[paychecks.CONFIG_KEY] = words
            else:
                cfg.pop(paychecks.CONFIG_KEY, None)
            return True
        _save_payee_config(conn, _apply)
        if state.get("changed"):
            _activity.record(conn, user, "merchant",
                             {"paycheck": "paycheck",
                              "never": "not_paycheck",
                              "auto": "paycheck_auto"}[word],
                             target=key, label=label)
        return {"ok": True, "key": key,
                "override": None if word == "auto" else word}
    finally:
        conn.close()


@router.post("/bills/hint")
@documented(response=BillHinted, body=BillHint)
def bills_hint_api(user: dict = Depends(_user()),
                   body: Fields = Depends(json_fields)):
    """Dismiss (or restore) a bill's ledger-fit hint — or, with
    target="health", its health pill. Dismissal stores a signature —
    rejection memory: the hint stays hidden until the underlying finding
    itself changes (a different fit, or a different health problem)."""
    from ..engine import bills
    payee = body.text("payee")
    if not payee:
        raise HTTPException(400, "payee required")
    action = body.one_of("action", ("dismiss", "restore"), default="dismiss")
    target = body.one_of("target", ("fit", "health"), default="fit")
    conn = _conn(user)
    try:
        # the SAME calendar day the list judged the row on — a UTC "today"
        # here could cross a threshold the household's day had not, and
        # store a signature the list then refuses to honour
        today = _today(conn)

        def _apply(cfg):
            key = "dismissed_health" if target == "health" \
                else "dismissed_hints"
            dismissed = cfg.get(key, {})
            if action == "restore":
                dismissed.pop(payee, None)
            elif target == "health":
                # ONE bill's analysis — a whole-table pass would run inside
                # the settings row lock and block every concurrent
                # settings write for as long as the ledger took
                h = bills.analyze_bills(conn, today, payee=payee
                                        ).get(payee) or {}
                if (h.get("health") or "") in ("stale", "mismatch",
                                               "misdated", "drifting"):
                    dismissed[payee] = {"health": h["health"],
                                        "action": (h.get("suggestion")
                                                   or {}).get("action"),
                                        "dismissed": today.isoformat()}
            else:
                h = bills.analyze_bills(conn, today, payee=payee).get(payee)
                fit = h.get("fit") if h else None
                if fit:
                    dismissed[payee] = {"kind": fit.get("kind"),
                                        "cadence": fit.get("cadence"),
                                        "by_month_day": fit.get("by_month_day"),
                                        "amount": fit.get("amount"),
                                        "dismissed": today.isoformat()}
            if dismissed:
                cfg[key] = dismissed
            else:
                cfg.pop(key, None)
            return True
        _save_payee_config(conn, _apply)
        return {"ok": True, "payee": payee,
                "dismissed": action != "restore"}
    finally:
        conn.close()


# Two ledger scans and a fuzzy match per row, on a payee the caller names —
# the same class of expense as the analytics reports next door, so the same
# budget. Both clients link here from every merchant name they render, so the
# ceiling has to sit far above a person reading their spending and still well
# under a loop.
@router.get("/bills/history",
            dependencies=[Depends(limit("bills_history", 60, 60))])
@documented(response=BillsHistory)
def bills_history_api(user: dict = Depends(_user()), payee: str = "",
                      window: str = "all"):
    """Everything the per-payee history page renders, as JSON: matched
    txns (with `hit` = the
    bill/proposal matcher would count it), monthly totals, lifetime
    aggregate, ledger fit, the bill's config (or `covered_by` when another
    bill's matcher already covers this merchant), health, and the
    payee-scoped proposals.

    `window` is the site-wide timeframe vocabulary (This month · 1m · 3m
    6m 1y 3y 5y all; bills.HISTORY_WINDOWS) and bounds the transaction
    list only, by the days Cash Flow's figure for the same key covers
    (reporting.ledger_ranges: this month to today, else complete months
    ending with last month); the lifetime aggregate is lifetime by
    definition. An unknown value reads as all."""
    from ..engine import bills, budget, merchant_dedup
    from ..engine.compat import as_dict
    from .pages import _cadence_label, _proposal_summary
    conn = _conn(user)
    try:
        # whether this name is a merchant the ledger files rows under — the
        # clients offer "Merge into…" only then (a bill's own label that no
        # row displays has nothing to merge)
        merchant_in_use = bool(payee.strip()) and merchant_dedup.display_in_use(
            conn, payee)
        hist = bills.merchant_history(
            conn, payee, window=window if window in bills.HISTORY_WINDOWS else "all")
        bill = bills.get_bill(conn, payee)
        # a merchant clicked from Transactions may already be covered by a
        # bill under a different display name — link to it instead of
        # offering a duplicate add. Uses the bill's REAL matcher.
        #
        # Several bills can match one string (the matcher keys on a brand's
        # leading word, so "Zenith Gas" and "Zenith Coffee" both match a
        # coffee-shop descriptor). The answer has to be the same on every
        # load and the bill that names this merchant most closely: the one
        # whose match key shares the most words with the text, then the
        # longer key, then the payee name as a stable tiebreak. Only live
        # bills are candidates — an archived one covers nothing.
        covered_by = None
        if bill is None and payee.strip():
            text = payee.lower()
            words = set(re.findall(r"[a-z0-9]+", text))
            best = None
            for b in conn.execute(
                    "SELECT payee, merchant FROM bills "
                    "WHERE active=1 AND amount<0 ORDER BY payee").fetchall():
                if b["merchant"] and budget.text_matches_merchant(
                        text, b["merchant"]):
                    key = b["merchant"].lower()
                    shared = len(words & set(re.findall(r"[a-z0-9]+", key)))
                    rank = (-shared, -len(key), b["payee"])
                    if best is None or rank < best[0]:
                        best = (rank, b["payee"])
            covered_by = best[1] if best else None
        bill_info = health = None
        if bill is not None and bill["active"]:
            raw = as_dict(bill["raw"])
            health = bills.analyze_bills(conn, payee=payee).get(payee)
            bill_info = {"amount": abs(bill["amount"] or 0),
                         "bill_type": raw.get("bill_type", "occurrence"),
                         "source": raw.get("source", "seed"),
                         "cadence": _bill_cadence(raw),
                         "by_month_day": budget.monthly_rule_days(
                             raw.get("recurrence")
                             if isinstance(raw.get("recurrence"), dict)
                             else {}) or None,
                         "due_on": (bill["due_on"].isoformat()
                                    if bill["due_on"] else None),
                         "income": (bill["amount"] or 0) > 0,
                         "tol": budget.bill_tolerance(abs(bill["amount"] or 0))}
        props = [p for p in bills.pending_proposals(conn)
                 if (p["payee"] or "").lower() == payee.lower()]
        summaries = {p["id"]: (bills.merchant_offer_summary(conn, p)
                               if p["kind"] == "merchant"
                               else _proposal_summary(p)) for p in props}
        # highlight: rows the bill/proposal matcher would count — by charge
        # SIZE, as bill_info reads it: an empty amount (a restore can land
        # one) is $0 and a signed one is its size, never a 500 or a band
        # on the wrong side of zero
        ref = (abs(bill["amount"] or 0) if bill is not None else
               abs(props[0]["amount"] or 0) if props else None)
        bands = (budget.bill_amount_bands(bill["amount"], bill["raw"])
                 if bill is not None else
                 [(ref, budget.bill_tolerance(ref), None)] if ref else None)
        want_inflow = bool(bill_info and bill_info["income"])
        hint_dismissed = bills.fit_matches_dismissal(
            hist["fit"],
            (budget.load_config(conn).get("dismissed_hints") or {}).get(payee))
        # the page renders the ledger through the SAME row builder as the
        # Transactions page — full pills/flags/receipt
        # affordances, stamped with the ⟳ bill indicator, so the two
        # surfaces cannot drift
        txns = [_ser(r) for r in data.transactions_by_ids(
            conn, [t["txn_id"] for t in hist["txns"]])]
        for o in txns:
            o["category_why"] = _category_why(o)
        _stamp_recurring(conn, txns)
    finally:
        conn.close()
    for row in txns:
        row["txn_id"] = row["id"]  # the payload key both clients read
        row["hit"] = bool(
            (bill_info is not None and bill_info["bill_type"] == "envelope"
             and row["amount"] > 0)
            or (bands is not None
                and (row["amount"] < 0 if want_inflow else row["amount"] > 0)
                and budget.amount_fits(bands, row["amount"], row.get("date"))))
    return {"payee": payee, "txns": txns, "monthly": hist["monthly"],
            "logo": hist.get("logo"),
            "fit": hist["fit"], "lifetime": _ser(hist["lifetime"]),
            "inflow": hist["inflow"], "bill": bill_info,
            "health": _ser(health) if health else None,
            "proposals": [{"id": p["id"], "kind": p["kind"],
                           "bill_type": p["bill_type"], "payee": p["payee"],
                           "amount": p["amount"],
                           "cadence": _cadence_label(p["frequency"],
                                                     p["interval"],
                                                     bills.offer_days(p)),
                           "by_month_day": bills.offer_days(p),
                           "summary": summaries[p["id"]]}
                          for p in props],
            "covered_by": covered_by, "hint_dismissed": hint_dismissed,
            # an archived bill under this payee: `bill` is null for it (it
            # is not live), so a client offering "add as recurring" here
            # would edit that bill instead — offer Restore on true
            "bill_archived": bill is not None and not bill["active"],
            "merchant_in_use": merchant_in_use,
            "cadences": CADENCES,
            # this page is the name's identity, not a figure's rows: a
            # Spending figure's rows are its own drill (specs/spending-drill.md)
            "scope_line": HISTORY_SCOPE_LINE}


HISTORY_SCOPE_LINE = "Every charge filed under this name — not only spending."


# bulk category override for a whole merchant, from its history page.
# Owner-only (highest-blast-radius write). preview → confirm-with-count →
# apply (returns an undo snapshot) → undo. Canonical-merchant scoped.

# Demo-denied like the other merchant-wide rewrites (merchant rename,
# category rename): the demo tenant is shared and resets hourly, and a
# write that reshapes a whole merchant's history and rules for every
# visitor in between is the kind the instance does not host.

# The longest name a merchant-wide write can be looking for: no stored
# descriptor is longer (sync.base.MAX_DESCRIPTOR clamps every one that is
# written), so a longer payee names nothing. Unbounded, it became a token
# pattern Postgres refuses to compile ("regular expression is too complex").
LOOKUP_NAME_MAX = MAX_DESCRIPTOR


@router.post("/bills/merchant-category/preview",
             dependencies=[Depends(limit("merchant_category_preview", 60, 60))])
@documented(response=MerchantCategoryPreviewed, body=MerchantCategoryPreview)
def merchant_category_preview_api(user: dict = Depends(_user()),
                                  body: Fields = Depends(json_fields),
                                  canon_only: bool = False):
    """`canon_only` (query flag or body field) narrows the preview to the
    payee's single canonical — the scope of the ledger row's "apply to
    whole merchant" write (set_category scope="all"), which the row menu
    previews. The default token-family scope is the history page's
    (BillsHistory previews the bulk set, set_merchant_category). The
    preview must count the same rows as the write behind it, and the two
    writes have different scopes."""
    demoguard.deny(user)
    _may_edit(user)
    payee = body.text("payee", max_len=LOOKUP_NAME_MAX)
    category = _category_name(body, "category", user=user)
    if not payee or not category:
        raise HTTPException(400, "payee and category required")
    canon_only = canon_only or bool(body.flag("canon_only", default=False))
    conn = _conn(user)
    try:
        return data.merchant_category_preview(conn, payee, category,
                                              family=not canon_only)
    finally:
        conn.close()


@router.post("/bills/merchant-category")
@documented(response=MerchantCategorySet, body=MerchantCategory)
def merchant_category_apply_api(user: dict = Depends(_user()),
                                body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    _may_edit(user)
    payee = body.text("payee", max_len=LOOKUP_NAME_MAX)
    category = _category_name(body, "category", user=user)
    if not payee or not category:
        raise HTTPException(400, "payee and category required")
    conn = _conn(user)
    try:
        out = data.set_merchant_category(conn, payee, category)
        _activity.record(conn, user, "rule", "set",
                         target=out.get("merchant") or payee,
                         label=out.get("merchant") or payee,
                         detail={"after": category, "count": out.get("count")})
        return out
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()


@router.post("/bills/merchant-category/undo")
@documented(response=MerchantCategoryUndone, body=MerchantCategoryUndo)
def merchant_category_undo_api(user: dict = Depends(_user()),
                               body: Fields = Depends(json_fields)):
    demoguard.deny(user)
    _may_edit(user)
    undo = body.opaque("undo", msg="undo snapshot required")
    if undo is None:
        raise HTTPException(400, "undo snapshot required")
    conn = _conn(user)
    try:
        # `undo` is what the write answered: a reference to the snapshot
        # the server holds (spent once; another household's, or a pruned
        # one, is not found), or an older client's inline snapshot, whose
        # shape and every value it would write are checked, not trusted
        n = data.undo_merchant_category(conn, undo)
        # an inline snapshot's merchant is the client's text: the log keeps
        # it at the length any merchant name may have, not as posted
        merchant = str(undo.get("merchant") or "")[:MERCHANT_NAME_MAX]
        _activity.record(conn, user, "rule", "undone",
                         target=merchant, label=merchant or "a merchant",
                         detail={"count": n})
        return {"ok": True, "restored": n}
    except data.UndoNotFound as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        conn.close()
