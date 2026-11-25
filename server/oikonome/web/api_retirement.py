"""The retirement projection and the debt payoff planner (the SPA's
Retirement and Debt pages).
Routes register on the shared API router (api_common.router); api.py
imports this module at a fixed point so the route order is preserved
(specs/api-routers.md).
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import Depends, HTTPException
from pydantic import BeforeValidator

from ..engine import activity as _activity
from .api_common import _conn, _may_edit, _today, _user, router
from .fields import MONEY_MAX, Fields, json_fields
from .schemas import documented
from .schemas.retirement import DebtPlan, DebtPlanSave, RetirementData
from .security import limit


# ---- retirement (SPA Retirement page) ---------------------------------------

def _blank_is(default):
    """Query coercion for a numeric what-if field the reader CLEARED.

    The adjust panels render these as text boxes where empty means "use
    the default" — the cash-yield box says so in its own placeholder. But
    an emptied box still rides the query string as `x=`, and an empty
    string is not a number, so without this one cleared field would 422
    the whole projection instead of reverting one assumption. The clients
    drop their own blanks before they build the query, but a bookmarked
    link or an older client can still carry one, so the server reads blank
    the way the UI labels it.
    Anything else still validates exactly as before: "abc" is a broken
    client, not an answer, and stays a 422."""
    def coerce(v):
        # an older phone stringifies a null it was sent ("null"), and a
        # field it never had ("undefined"): neither is a what-if
        if isinstance(v, str) and v.strip().lower() in ("", "null",
                                                         "undefined"):
            return default
        return v
    return BeforeValidator(coerce)


def _rate(v: float | None, default: float | None) -> float | None:
    """A what-if percentage, bounded to the range the page's controls offer.

    NaN reaches here — "nan" parses as a float — and then survives a
    min/max clamp, because `max(lo, min(nan, hi))` is `lo`: a NaN yield
    would quietly become the WORST assumption in the range and come back
    in `inputs` as though the reader had chosen it. No what-if can mean
    NaN, so it reads as the default. ±inf is ordinary out-of-range input
    and clamps to an end of the range like any other huge number."""
    import math
    # one range for this page and the retirement wizard's saved answers
    from ..sync.restore import RET_RATE_MAX, RET_RATE_MIN
    if v is None or math.isnan(v):
        return default
    return max(RET_RATE_MIN, min(v, RET_RATE_MAX))


# The spend the server assumed before it learned to ask for one; a client
# that cannot show the spend-less reply is still projected at it.
OLDER_CLIENT_SPEND = 250_000


# A cache MISS here is ~10k simulations (one age→end_age walk per retire-age
# row, plus the bisection and the 1928-2025 historical replay), the handler
# is sync so it occupies a shared threadpool worker for all of it, and the
# projection cache is process-wide. So the request budget is small on
# purpose: the page fetches once per load and once per Recalculate, which
# 30/min is far past, while a script walking a parameter to miss the cache
# every time is stopped before it can hold the pool.
@router.get("/retirement",
            dependencies=[Depends(limit("retirement", 30, 60))])
@documented(response=RetirementData)
def retirement_api(user: dict = Depends(_user()),
                   age: Annotated[int | None, _blank_is(None)] = None,
                   spend: Annotated[int | None, _blank_is(None)] = None,
                   ret: Annotated[float | None, _blank_is(None)] = None,
                   infl: Annotated[float | None, _blank_is(None)] = None,
                   end: Annotated[int | None, _blank_is(None)] = None,
                   ssage: Annotated[int | None, _blank_is(None)] = None,
                   employer_mo: Annotated[int | None, _blank_is(None)] = None,
                   taxable_mo: Annotated[int | None, _blank_is(None)] = None,
                   roth_mo: Annotated[int | None, _blank_is(None)] = None,
                   resume: Annotated[int, _blank_is(0)] = 0,
                   stockpct: Annotated[int, _blank_is(90)] = 90,
                   cash: Annotated[float | None, _blank_is(None)] = None,
                   ask_spend: Annotated[int, _blank_is(0)] = 0):
    """Thin wrapper over engine/retirement.project, as JSON.

    Every input is, in order: the query param (a what-if from the page's
    adjust panel), the household's saved retirement answers
    (`ret_wizard_answers`), then the server's default. Age reads the
    configured birthdate before a saved age. Spend with no answer is the
    ledger's trailing year; with too little ledger for that there is NO
    assumed spend: `inputs.spend_source` is "needed" and nothing is
    projected — for a client that sends `ask_spend=1`. A caller that does
    not (a phone built before the spend-less reply, which reads spend,
    rows and stress unguarded) gets the projection the older server sent,
    at `OLDER_CLIENT_SPEND`, still labelled "needed".
    `spend_source` / `saving_source` name where each came from.
    `cash` is the cash bucket's nominal yield in %; unset, cash keeps pace
    with inflation."""
    from ..engine import budget, retirement as retire
    from ..sync.restore import ret_marked_answers, ret_wizard_answer
    conn = _conn(user)
    try:
        cfg = budget.load_config(conn)
        # the saved answers, each held to the rule the settings door and
        # the restore hold it to — a hand-edited blob must not 500 the page
        # Only answers a current client saved are inputs; an older
        # wizard's saves were its prefilled boxes and stay prefills.
        raw_ans = cfg.get("ret_wizard_answers")
        ans = {}
        for k in ret_marked_answers(raw_ans):
            n = ret_wizard_answer(k, raw_ans.get(k))
            if n is not None:
                ans[k] = n

        def pick(q, key, default):
            return q if q is not None else ans.get(key, default)

        bd = cfg.get("birthdate")
        birth_year = None
        try:
            birth_year = dt.date.fromisoformat(bd).year if bd else None
        except (TypeError, ValueError):
            pass                                # corrupt config must not 500
        if not age:
            try:
                age = retire.age_from_birthdate(bd) if bd else None
            except (TypeError, ValueError):
                age = None
            if age is None:
                age = int(ans.get("age", 45))
        end = int(pick(end, "end", 95))
        ssage = int(pick(ssage, "ssage", 67))
        infl = _rate(pick(infl, "infl", 3.0), 3.0)
        ret = _rate(pick(ret, "ret", 7.0), 7.0)
        # spend: a what-if, else the saved answer, else the ledger's year,
        # else none at all (0 is no plan, the way the page always read it)
        if spend:
            spend_source = "what_if"
        elif ans.get("spend"):
            spend, spend_source = ans["spend"], "saved"
        else:
            spend = retire.default_spend(conn)
            spend_source = "ledger" if spend else "needed"
        # saving, split by tax treatment: one decider
        # (specs/retirement-figures.md, "Saving is split by tax treatment")
        found = retire.ledger_saving(conn, _today(conn))
        plan = retire.saving_plan(
            {"employer_mo": employer_mo, "roth_mo": roth_mo,
             "taxable_mo": taxable_mo}, ans, found)
        td_mo, roth_mo, taxable_mo = (plan["td_mo"], plan["roth_mo"],
                                      plan["taxable_mo"])
        saving_source, saving_basis = plan["source"], plan["basis"]
        # clamp adversarial/typo'd query params — bad input must not 500
        age = max(18, min(age, 90))
        end = max(age + 1, min(end, 110))
        ssage = max(62, min(ssage, 70))
        cash = _rate(cash, None)
        stockpct = max(0, min(stockpct, 100))
        # `resume` is an age like the others and is bounded the same way.
        # It is part of the projection's memo key, so an unclamped value is
        # also an unbounded set of cache entries an anonymous query string
        # picks — the same "size decided by the client" shape the clamps
        # above close.
        resume = max(0, min(resume, 110))
        # These are floored AND capped (saving_plan holds the saving
        # figures to the same $10M a month and the $50 grid): the block
        # above exists precisely because bad input must not 500. A monthly
        # contribution of 1e308 overflows to inf inside the compounding loop
        # and the projection comes back as a wall of NaN that json.dumps
        # refuses to serialize. $10M/month and $100M/year of spend are past
        # any real plan and far short of the float ceiling.
        if spend is not None:
            spend = max(0, min(int(spend), 100_000_000))
        # Snap the continuous inputs to the grid the page's own controls
        # step on ($1k spend, $50/mo contributions, 0.1% rates). The
        # projection is memoized on its exact parameter tuple, so an
        # unquantized dollar would be a distinct cache key: spend=1,2,3,… would
        # miss the cache on every request and each miss would run the full
        # simulation in a shared threadpool worker. A projection does not change
        # meaningfully between $38,400 and $38,000 of spend — it does
        # change between a cached answer and 10k simulations — and
        # `default_spend` already rounds the page's starting figure to the
        # same $1k. `inputs` echoes what was actually used, so the client
        # always shows the numbers the answer was computed from.
        if spend is not None:
            spend = int(round(spend, -3)) or (1000 if spend else 0)
        ret = round(ret, 1)
        infl = round(infl, 1)
        if cash is not None:
            cash = round(cash, 1)
        # A restored/hand-edited config can carry a non-integer ss_estimates
        # key (the Settings-save path validates it as an int in [62,70], but
        # the restore config-merge does not) — a bad key must be dropped, not
        # 500 the whole Retirement page, same as the birthdate guard above.
        # The amounts get the same guard: text, null, a list, a negative or
        # a non-finite figure would reach the simulation as its monthly SS
        # and fail it on every load, so such an entry is dropped too.
        sched = {}
        raw_sched = cfg.get("ss_estimates")
        for k, v in (raw_sched.items() if isinstance(raw_sched, dict)
                     else ()):
            if isinstance(v, bool):
                continue
            try:
                amt = float(v)
                age_k = int(k)
            except (ValueError, TypeError, OverflowError):
                continue
            # the Settings door's bounds: a claim age SSA allows and an
            # amount no larger than any money field takes (a restored
            # 1e308 overflows to inf once scaled or made yearly)
            if 62 <= age_k <= 70 and 0 <= amt <= MONEY_MAX:
                sched[age_k] = amt
        # from the SSA statement: a claim age without its own estimate is
        # the straight line between the saved ages around it, or SSA's
        # adjustment from the nearest one outside them
        your_ss = round(retire.ss_at_age(sched, ssage, birth_year), 2)
        resume_age = resume or age              # default: resume saving now
        cash_nominal = cash / 100 if cash is not None else None
        if spend_source == "needed" and not ask_spend:
            spend = OLDER_CLIENT_SPEND
        if spend_source == "needed" and ask_spend:
            r = retire.project_nothing(conn, nominal=ret / 100,
                                       inflation=infl / 100,
                                       cash_nominal=cash_nominal,
                                       birth_year=birth_year)
        else:
            r = retire.project(conn, age=age, end_age=end, spend=spend,
                               nominal=ret / 100, inflation=infl / 100,
                               ss_monthly=your_ss, ss_age=ssage,
                               rental_monthly=0, td_annual=td_mo * 12,
                               roth_annual=roth_mo * 12,
                               taxable_annual=taxable_mo * 12,
                               resume_age=resume_age,
                               stock_frac=stockpct / 100,
                               cash_nominal=cash_nominal,
                               birth_year=birth_year)
    finally:
        conn.close()
    r["inputs"] = dict(age=age, spend=spend, ret=ret, infl=infl, end=end,
                       ssage=ssage, employer_mo=plan["employer_mo"],
                       td_mo=plan["seed_td_mo"],
                       taxable_mo=taxable_mo, roth_mo=plan["seed_roth_mo"],
                       resume=resume_age,
                       stockpct=stockpct, cash=cash,
                       saving_yr=(td_mo + roth_mo + taxable_mo) * 12,
                       saving_mo=td_mo + roth_mo + taxable_mo,
                       saving_parts=plan["parts"],
                       saving_hint=plan["hint"],
                       saving_basis=saving_basis,
                       saving_found_mo=(int(round(found["monthly"] / 50.0) * 50)
                                        if found is not None else None),
                       your_ss=your_ss, has_sched=bool(sched),
                       spend_source=spend_source,
                       saving_source=saving_source)
    return r


# ---- debt payoff planner (SPA Debt page) -----------------------------------
# The walk is a few hundred months over a handful of debts, three times per
# request (chosen method, the other method, minimums-only), so the budget is
# generous next to the retirement projection's; the limit is there so a
# script cannot make the pool do it in a loop.

@router.get("/debt-plan", dependencies=[Depends(limit("debt", 120, 60))])
@documented(response=DebtPlan)
def debt_plan_api(user: dict = Depends(_user()),
                  method: Annotated[str | None, _blank_is(None)] = None,
                  extra: Annotated[float | None, _blank_is(None)] = None):
    """The payoff plan for every card and loan carrying a balance.
    `method` and `extra` are what-if overrides of the saved plan and are
    not written; the reply says which plan is saved."""
    from ..engine import debt as _debt
    conn = _conn(user)
    try:
        return _debt.build(conn, _today(conn), method=method, extra=extra)
    finally:
        conn.close()


@router.post("/debt-plan")
@documented(response=DebtPlan, body=DebtPlanSave)
def debt_plan_save_api(user: dict = Depends(_user()),
                       body: Fields = Depends(json_fields)):
    """Save the plan — the method, the extra monthly payment and any
    per-debt rate / minimum overrides — and return the plan it produces.
    Bookkeeping like a budget, so a member may, and the demo may."""
    _may_edit(user)
    from ..engine import budget, debt as _debt
    # each top-level field typed here; normalize_plan keeps every range
    # and checks each per-debt override itself. A key left out stays out,
    # so the plan's own defaults apply, and a null method or extra is
    # refused as it always was.
    sent: dict = {}
    if body.present("method"):
        sent["method"] = body.one_of(
            "method", _debt.METHODS, required=True,
            msg="method must be avalanche or snowball")
    if body.present("extra_monthly"):
        sent["extra_monthly"] = (
            None if body.json_type("extra_monthly") == "null" else body.money(
                "extra_monthly", default=0.0,
                msg="extra monthly payment must be a number from 0 to "
                    "1,000,000"))
    sent["debts"] = body.opaque(
        "debts", msg="debts must be an object keyed by account id")
    try:
        plan = _debt.normalize_plan(sent)
    except ValueError as e:
        raise HTTPException(400, str(e))
    conn = _conn(user)
    try:
        with budget.config_txn(conn) as cfg:
            changed = cfg.get(_debt.PLAN_KEY) != plan
            cfg[_debt.PLAN_KEY] = plan
        if changed:
            _activity.record(conn, user, "settings", "debt_plan",
                             detail={"keys": [_debt.PLAN_KEY]})
        return _debt.build(conn, _today(conn))
    finally:
        conn.close()
