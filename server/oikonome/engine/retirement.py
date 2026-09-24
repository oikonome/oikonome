"""Retirement projection — deterministic, tax-aware, all in TODAY's dollars.

Near-pure compute over one SQL query. `current_buckets` classifies accounts
by subtype/name heuristics only — never by institution — so the tax
treatment follows the plan type (401a/401k/403b/457b/ira).

Everything is modeled in real (inflation-adjusted) dollars. Taxes use
adjustable EFFECTIVE rates; withdrawals draw tax-smart:
cash → taxable → tax-deferred → Roth.

Cash earns `cash_nominal` (default: the inflation rate, i.e. 0% real — a
savings or money-market yield that keeps pace with prices). It is an explicit
assumption, not an omission: at 0% nominal the same cash loses ~26% of its
purchasing power over ten years of 3% inflation, and a projection that
silently protects cash from inflation flatters every cash-heavy plan.

RMDs apply from the SECURE 2.0 start age for the saver's birth year (73 for
1951–1959, 75 for 1960 and later) whether or not they are still working — an
IRA's distribution cannot be deferred by employment.
"""
from __future__ import annotations

import copy
import datetime as dt
import functools


def real_return(nominal: float, inflation: float) -> float:
    return (1 + nominal) / (1 + inflation) - 1


def current_buckets(conn) -> dict:
    """Group live balances into the four tax treatments the sim needs.

    Linked shadow accounts (lower-ranked sources of the same
    real-world account) are excluded, same rule as net worth
    (`reporting._live_accounts`) — a dual-source 401k must count once,
    not once per aggregator."""
    from . import links
    shadows = links.shadow_ids(conn)
    b = {"cash": 0.0, "taxable": 0.0, "td": 0.0, "roth": 0.0, "debt": 0.0}
    rows = conn.execute(
        """SELECT a.name, a.type, a.subtype, a.balance_current bal,
                  i.institution_name inst
           FROM accounts a JOIN items i ON i.id=a.item_id
           WHERE a.balance_current IS NOT NULL
             AND NOT (a.id = ANY(%s))
             -- the retirement sim projects PERSONAL
             -- assets — an LLC's checking/card assigned to an entity is the
             -- business's money, not the household's nest egg. Same
             -- predicate as reporting._live_accounts/forecast.build, whose
             -- Net Worth this must agree with.
             AND (NULLIF(current_setting('app.combine_entities', true),
                         '') = 'true' OR a.entity_id IS NULL)""",
        (shadows,)).fetchall()
    for r in rows:
        n = (r["name"] or "").lower()
        sub = (r["subtype"] or "").lower()
        v = r["bal"] or 0.0
        if r["type"] == "credit":
            b["debt"] += v                                  # owed (positive)
        # Roth decides FIRST, whatever the plan wrapper is. Plaid ships a
        # distinct "roth 401k" subtype (and "roth 403b"), which is neither
        # "roth" nor a member of the pre-tax tuple below: checking the
        # employer-plan subtypes first would send a Roth 401(k) to `td` —
        # or, when the subtype misses both, to `taxable`, where withdrawals
        # take a capital-gains haircut on money that is tax-free by
        # definition.
        elif "roth" in sub or "roth" in n:
            b["roth"] += v
        # every pre-tax wrapper Plaid names, plus the flat "retirement"/
        # "pension" stamp aggregators use when they only know the money is
        # a retirement plan — pre-tax is the safe reading for those, since
        # a Roth was already caught above
        elif (sub in ("401a", "401k", "403b", "457b", "ira", "sep ira",
                      "simple ira", "pension", "retirement", "keogh",
                      "sarsep", "profit sharing plan", "thrift savings plan")
              or "traditional" in n):
            b["td"] += v
        # a health savings account is pre-tax money — ordinary income when
        # drawn after 65, tax-free for medical costs — so tax-deferred is the
        # conservative reading; as a brokerage it would take a capital-gains
        # haircut it never owes
        elif sub in ("hsa", "hra"):
            b["td"] += v
        # education savings pay for tuition, not a retirement: never part of
        # the nest egg the simulation spends
        elif sub in ("529", "education savings account"):
            continue
        elif sub == "crypto":
            b["taxable"] += v                               # crypto = cap gains
        elif r["type"] == "depository":
            b["cash"] += v
        elif r["type"] == "investment":
            b["taxable"] += v
    # Net card debt out of the buckets in the sim's draw order — cash →
    # taxable → tax-deferred → Roth (paying it off costs the same assets a
    # withdrawal would draw). Flooring the netting at taxable would make
    # debt beyond cash+taxable silently vanish and start every projection
    # optimistic. Anything beyond ALL buckets is carried
    # as `debt_residual` and subtracted from `total` — the sim can't hold a
    # negative bucket, but the number must never be dropped. (Dollar-for-
    # dollar against tax-deferred is mildly kind — a real payoff from a
    # pre-tax account would gross up — but it beats pretending the debt
    # isn't there; effective-rate precision isn't this heuristic's job.)
    spill = b["debt"]
    for k in ("cash", "taxable", "td", "roth"):
        take = min(b[k], spill)
        b[k] -= take
        spill -= take
    b["debt_residual"] = spill
    b["total"] = (b["cash"] + b["taxable"] + b["td"] + b["roth"]
                  - b["debt_residual"])
    return b


def _grow(b, rr, cash_rr=0.0):
    return {"cash": b["cash"] * (1 + cash_rr), "taxable": b["taxable"] * (1 + rr),
            "td": b["td"] * (1 + rr), "roth": b["roth"] * (1 + rr)}


def _draw(bucket, need, net_factor):
    """Withdraw from `bucket` to cover after-tax `need`; net_factor = $
    spendable per $ withdrawn. Returns (remaining_bucket, remaining_need)."""
    if need <= 0 or bucket <= 0 or net_factor <= 0:
        return bucket, need
    gross = min(bucket, need / net_factor)
    return bucket - gross, need - gross * net_factor


# RMDs: SECURE 2.0 start age is 75 for anyone born 1960+, 73 for 1951–1959
# (72 before that); divisors from the IRS Uniform Lifetime Table (2022
# revision). Applied to tax-deferred only, working or retired; the after-tax
# excess over spending needs is reinvested in taxable.
RMD_START = 75


def rmd_start_age(birth_year: int | None) -> int:
    if birth_year is None:
        return RMD_START
    if birth_year <= 1950:
        return 72
    if birth_year <= 1959:
        return 73
    return RMD_START


RMD_DIVISOR = {72: 27.4, 73: 26.5, 74: 25.5,
               75: 24.6, 76: 23.7, 77: 22.9, 78: 22.0, 79: 21.1, 80: 20.2,
               81: 19.4, 82: 18.5, 83: 17.7, 84: 16.8, 85: 16.0, 86: 15.2,
               87: 14.4, 88: 13.7, 89: 12.9, 90: 12.2, 91: 11.5, 92: 10.8,
               93: 10.1, 94: 9.5, 95: 8.9, 96: 8.4, 97: 7.8, 98: 7.3,
               99: 6.8, 100: 6.4}


def simulate(buckets, age, retire_age, end_age, spend, rr, ss_annual, ss_age,
             rental_annual, td_annual, taxable_annual, resume_age,
             eff_ord, eff_cg, gain_frac, shock=(), rr_path=None,
             cash_rr=0.0, rmd_start=RMD_START, extra_annual=0.0):
    """Run age→end_age. Returns dict(survived, fail_age, end_balance,
    at_retire).

    Year `a` is the year the saver is that age: balances grow, then the
    working saver contributes or the retiree draws. `at_retire` is the
    balance at the moment of retiring — the start of the retire_age year,
    before that year's growth — so it is the same figure the growth path
    shows for that age.

    shock: real-return overrides for the first len(shock) RETIREMENT years —
    the sequence-of-returns stress. rr_path: full per-year real-return
    sequence (index 0 = current age) — overrides rr and shock. cash_rr: the
    cash bucket's real return. rmd_start: first RMD age. extra_annual:
    saving on top of the plan, into taxable, every working year from now —
    not held back to resume_age, because it is the answer to "what would
    I have to start doing"."""
    # The four buckets live in locals for the whole walk. A projection runs
    # this loop thousands of times (every bisection step of every retire
    # age, every historical path), so a dict rebuilt per simulated year is
    # most of the page's cost. The arithmetic — growth factor, draw order,
    # the order the four balances are summed — is unchanged, operation for
    # operation, so the numbers are bit-identical to the dict form.
    cash, taxable, td, roth = (buckets["cash"], buckets["taxable"],
                               buckets["td"], buckets["roth"])
    ntx = 1 - eff_cg * gain_frac            # spendable per $ from taxable
    nod = 1 - eff_ord                       # spendable per $ from tax-deferred
    ss_net = ss_annual * (1 - 0.85 * eff_ord)
    rent_net = rental_annual * (1 - eff_ord)
    at_retire = None
    rmd_div = RMD_DIVISOR
    n_shock = len(shock)
    gc = 1 + cash_rr
    for a in range(age, end_age + 1):
        if a >= retire_age and at_retire is None:
            at_retire = cash + taxable + td + roth
        if rr_path is not None:
            yr_rr = rr_path[a - age]
        else:
            yr_since_ret = a - retire_age
            yr_rr = (shock[yr_since_ret]
                     if 0 <= yr_since_ret < n_shock else rr)
        g = 1 + yr_rr                                       # _grow, inlined
        cash = cash * gc
        taxable = taxable * g
        td = td * g
        roth = roth * g
        # A required minimum distribution is forced by age alone — working
        # does not defer it. Taxed as ordinary income; what spending does
        # not need is reinvested in taxable.
        rmd_net = 0.0
        if a >= rmd_start and td > 0:
            gross = td / rmd_div.get(min(a, 100), 6.4)
            td -= gross
            rmd_net = gross * nod
        if a < retire_age:
            if a >= resume_age:
                td += td_annual                             # employer plan
                taxable += taxable_annual                   # brokerage sweep
            taxable += rmd_net
            if extra_annual:
                taxable += extra_annual
        else:
            income = (ss_net if a >= ss_age else 0.0) + rent_net + rmd_net
            need = spend - income
            if need < 0:
                taxable += -need                            # excess reinvested
                need = 0.0
            # tax-smart draw order: cash → taxable → tax-deferred → Roth
            # (_draw, inlined: gross = min(bucket, need / net_factor))
            if need > 0 and cash > 0:
                gross = min(cash, need / 1.0)
                cash -= gross
                need -= gross * 1.0
            if need > 0 and taxable > 0 and ntx > 0:
                gross = min(taxable, need / ntx)
                taxable -= gross
                need -= gross * ntx
            if need > 0 and td > 0 and nod > 0:
                gross = min(td, need / nod)
                td -= gross
                need -= gross * nod
            if need > 0 and roth > 0:
                gross = min(roth, need / 1.0)
                roth -= gross
                need -= gross * 1.0
            if need > 1.0:
                return {"survived": False, "fail_age": a, "end_balance": 0.0,
                        "at_retire": at_retire or 0.0}
    if at_retire is None:                                    # retire_age > end_age
        at_retire = cash + taxable + td + roth
    return {"survived": True, "fail_age": None,
            "end_balance": cash + taxable + td + roth,
            "at_retire": at_retire}


def _max_spend(buckets, age, retire_age, end_age, rr, ss_annual, ss_age,
               rental_annual, td_annual, taxable_annual, resume_age,
               eff_ord, eff_cg, gain_frac, shock=(), cash_rr=0.0,
               rmd_start=RMD_START):
    """Largest constant real annual spend that still lasts to end_age.

    30 halvings of the $2M bracket resolve the answer to about $0.002 — the
    same resolution hist_spend_at uses — and the page shows whole dollars,
    so more halvings buy nothing visible at a fifth more simulations."""
    lo, hi = 0.0, 2_000_000.0
    for _ in range(30):
        mid = (lo + hi) / 2
        ok = simulate(buckets, age, retire_age, end_age, mid, rr, ss_annual, ss_age,
                      rental_annual, td_annual, taxable_annual, resume_age,
                      eff_ord, eff_cg, gain_frac, shock=shock, cash_rr=cash_rr,
                      rmd_start=rmd_start)["survived"]
        if ok:
            lo = mid
        else:
            hi = mid
    return lo


# ---- historical-sequence analysis (FIRECalc-style) ------------------------
# Replay the plan against EVERY contiguous window of real 1928-2025 returns
# (wrapping), blended stock/bond. Deterministic — no RNG — and explainable:
# "survived N of 98 historical sequences".


CATCH_UP_CAP = 50_000.0 * 12     # past this a month the honest answer is "no"
CATCH_UP_STEP = 10               # the page shows a round monthly figure


def catch_up_monthly(buckets, age, retire_age, end_age, spend, kw):
    """Smallest extra monthly saving, from now until `retire_age`, that
    makes retiring then last to end_age — rounded UP to the step, so the
    figure shown is one that works. 0 when the plan already works; None
    when there are no working years left to save in, or no saving under
    the cap is enough."""
    def ok(extra):
        return simulate(buckets, age, retire_age, end_age, spend,
                        extra_annual=extra, **kw)["survived"]
    if ok(0.0):
        return 0
    if retire_age <= age or not ok(CATCH_UP_CAP):
        return None
    lo, hi = 0.0, CATCH_UP_CAP
    for _ in range(30):
        mid = (lo + hi) / 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    monthly = int(-(-hi / 12 // CATCH_UP_STEP) * CATCH_UP_STEP)
    return monthly


@functools.lru_cache(maxsize=32)
def _hist_paths(years_needed: int, stock_frac: float) -> tuple[tuple[float, ...], ...]:
    """Pure function of its two arguments, built from a constant table, so the
    result is cached: one projection asks for the same paths once per retire
    age. Tuples, so a cached value cannot be mutated by a caller."""
    from . import hist_returns as H
    blend = [stock_frac * s + (1 - stock_frac) * bo
             for s, bo in zip(H.STOCK, H.BOND)]
    n = len(blend)
    return tuple(tuple(blend[(s + i) % n] for i in range(years_needed))
                 for s in range(n))


def hist_success(buckets, age, retire_age, end_age, spend, stock_frac, kw) -> dict:
    """Fraction of historical start-years the plan survives, at this spend."""
    paths = _hist_paths(end_age - age + 1, stock_frac)
    ok = sum(1 for p in paths
             if simulate(buckets, age, retire_age, end_age, spend,
                         rr_path=p, **kw)["survived"])
    return {"ok": ok, "n": len(paths), "pct": round(100 * ok / len(paths), 1)}


def hist_spend_at(buckets, age, retire_age, end_age, stock_frac, kw,
                  target: float = 0.90) -> float:
    """Largest constant real spend surviving ≥ target fraction of history."""
    paths = _hist_paths(end_age - age + 1, stock_frac)
    need = int(-(-target * len(paths) // 1))          # ceil

    def ok(spend):
        good = 0
        for i, p in enumerate(paths):
            if simulate(buckets, age, retire_age, end_age, spend,
                        rr_path=p, **kw)["survived"]:
                good += 1
                if good >= need:
                    return True
            if good + (len(paths) - 1 - i) < need:
                return False                          # can't reach target
        return good >= need

    lo, hi = 0.0, 2_000_000.0
    for _ in range(30):
        mid = (lo + hi) / 2
        if ok(mid):
            lo = mid
        else:
            hi = mid
    return lo


# Sequence-of-returns stress scenarios: real-return overrides for the first
# years OF RETIREMENT. Calibrated to historical drawdowns in real terms.
SCENARIOS = [
    ("Average markets", ()),
    ("Crash at retirement (−35% year 1)", (-0.35,)),
    ("Crash + slow recovery (−20%, −10%, 0%)", (-0.20, -0.10, 0.0)),
    ("Lost decade (0% real, 10 yrs)", (0.0,) * 10),
]


def _growth_path(b, age, horizon, rr, td_annual, taxable_annual, resume_age,
                 cash_rr=0.0, rmd_start=RMD_START, eff_ord=0.0):
    """Portfolio value (today's $) year by year if you KEEP working & saving.

    Point k is the balance after k years, i.e. at age `age + k` — the same
    figure `simulate` reports as `at_retire` for retiring at that age, so
    the chart and the retire-age table never disagree by a year of growth.
    Same year model as `simulate`: grow, force any RMD, contribute."""
    yr0 = dt.date.today().year
    bb = {k: b[k] for k in ("cash", "taxable", "td", "roth")}
    path = [[f"{yr0}-06", round(bb["cash"] + bb["taxable"] + bb["td"] + bb["roth"], 2)]]
    for a in range(age, horizon):
        bb = _grow(bb, rr, cash_rr)
        if a >= rmd_start and bb["td"] > 0:
            gross = bb["td"] / RMD_DIVISOR.get(min(a, 100), 6.4)
            bb["td"] -= gross
            bb["taxable"] += gross * (1 - eff_ord)
        if a >= resume_age:
            bb["td"] += td_annual
            bb["taxable"] += taxable_annual
        path.append([f"{yr0 + (a - age) + 1}-06",
                     round(bb["cash"] + bb["taxable"] + bb["td"] + bb["roth"], 2)])
    return path


def age_from_birthdate(birthdate: str, today: dt.date | None = None) -> int:
    """Exact age from an ISO birthdate (per-tenant config `birthdate`)."""
    bd = dt.date.fromisoformat(birthdate)
    today = today or dt.date.today()
    return today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))


def default_spend(conn, floor: float = 24_000, fallback: int = 250_000) -> int:
    """The page's honest starting point: the household's OWN last-12-months
    spend (the product's spend definition), rounded to $1k. A fixed
    default is wrong for any household whose real spend differs from it,
    and reads as broken; the ledger is the right source. Falls back only
    when the ledger is too thin to mean anything."""
    from .reporting import NET_AMOUNT_JOINED, REIMB_PARTIAL_JOIN, SPEND_WHERE
    # net of partial reimbursements, like every other spend aggregate — an
    # expensed-and-repaid card charge is not money the household lives on
    row = conn.execute(
        f"SELECT COALESCE(SUM({NET_AMOUNT_JOINED}),0) AS s "
        f"FROM transactions t {REIMB_PARTIAL_JOIN} "
        f"WHERE {SPEND_WHERE} AND t.date >= CURRENT_DATE - 365").fetchone()
    s = float(row["s"] or 0)
    return int(round(s, -3)) if s >= floor else fallback


def project(conn, *, age, end_age, spend, nominal, inflation, ss_monthly, ss_age,
            rental_monthly, td_annual, taxable_annual, resume_age,
            eff_ord=0.25, eff_cg=0.18, gain_frac=0.5, retire_max=72,
            stock_frac=0.9, cash_nominal=None, birth_year=None):
    """Build the retire-age × (feasible?, max-spend) table + headline answers
    + the forward growth curve.

    cash_nominal: the cash bucket's nominal yield; None means "keeps pace
    with inflation" (0% real). birth_year sets the RMD start age."""
    b = current_buckets(conn)
    cash_rr = (real_return(cash_nominal, inflation)
               if cash_nominal is not None else 0.0)
    rmd_start = rmd_start_age(birth_year)
    # Everything past this line is a pure function of the buckets and the
    # what-if parameters (plus today's year, which dates the growth path), so
    # the result is memoized on exactly those. Dragging a slider re-requests
    # the same projection many times; a hit costs a copy instead of ~10k
    # simulations. No tenant state leaks: the key contains the balances the
    # value was computed from, so a hit is only ever the caller's own answer.
    key = (tuple(sorted(b.items())), age, end_age, spend, nominal, inflation,
           ss_monthly, ss_age, rental_monthly, td_annual, taxable_annual,
           resume_age, eff_ord, eff_cg, gain_frac, retire_max, stock_frac,
           cash_rr, rmd_start, dt.date.today().year)
    return copy.deepcopy(_project_cached(key))


@functools.lru_cache(maxsize=64)
def _project_cached(key: tuple) -> dict:
    (b_items, age, end_age, spend, nominal, inflation, ss_monthly, ss_age,
     rental_monthly, td_annual, taxable_annual, resume_age, eff_ord, eff_cg,
     gain_frac, retire_max, stock_frac, cash_rr, rmd_start, _today_year) = key
    b = dict(b_items)
    rr = real_return(nominal, inflation)
    ss_annual = ss_monthly * 12
    rental_annual = rental_monthly * 12
    kw = dict(rr=rr, ss_annual=ss_annual, ss_age=ss_age, rental_annual=rental_annual,
              td_annual=td_annual, taxable_annual=taxable_annual, resume_age=resume_age,
              eff_ord=eff_ord, eff_cg=eff_cg, gain_frac=gain_frac,
              cash_rr=cash_rr, rmd_start=rmd_start)
    retire_max = max(retire_max, age)     # age>72 must still yield ≥1 row
    rows, earliest = [], None
    for ra in range(age, retire_max + 1):
        sim = simulate(b, age, ra, end_age, spend, **kw)
        ms = _max_spend(b, age, ra, end_age, **kw)
        rows.append({"age": ra, "at_retire": sim["at_retire"],
                     "feasible": sim["survived"], "fail_age": sim["fail_age"],
                     "max_spend": ms})
        if sim["survived"] and earliest is None:
            earliest = ra
    # stress table — ref age never below current age (a ref_age in the past
    # would place the shock years pre-retirement where they're ignored)
    ref_age = earliest if earliest is not None else max(age, min(67, retire_max))
    stress = []
    for label, shock in SCENARIOS:
        s_earliest = None
        for ra in range(age, retire_max + 1):
            if simulate(b, age, ra, end_age, spend, shock=shock, **kw)["survived"]:
                s_earliest = ra
                break
        s_max = _max_spend(b, age, ref_age, end_age, shock=shock, **kw)
        stress.append({"label": label, "earliest": s_earliest,
                       "max_spend_at_ref": s_max})

    # no age works: say what saving would make one work — the customary
    # age and the last one the table tries
    catch_up = None
    if earliest is None:
        catch_up = []
        for ra in sorted({ref_age, retire_max}):
            m = catch_up_monthly(b, age, ra, end_age, spend, kw)
            if m:
                catch_up.append({"age": ra, "extra_monthly": m})

    hist = hist_success(b, age, ref_age, end_age, spend, stock_frac, kw)
    hist["spend90"] = hist_spend_at(b, age, ref_age, end_age, stock_frac, kw, 0.90)
    hist["spend100"] = hist_spend_at(b, age, ref_age, end_age, stock_frac, kw, 1.0)
    hist["stock_frac"] = stock_frac
    hist_by_age = {ra: hist_success(b, age, ra, end_age, spend, stock_frac, kw)["pct"]
                   for ra in range(age, retire_max + 1)}
    for row in rows:
        row["hist_pct"] = hist_by_age.get(row["age"])

    horizon = min(end_age, 80)
    path = _growth_path(b, age, horizon, rr, td_annual, taxable_annual, resume_age,
                        cash_rr=cash_rr, rmd_start=rmd_start, eff_ord=eff_ord)
    at67 = next((v for y, v in path
                 if y == f"{dt.date.today().year + (67 - age)}-06"), None)
    if at67 is None:
        at67 = path[-1][1] if path else b["total"]
    return {
        "buckets": b, "rows": rows, "earliest": earliest,
        "real_return": rr, "cash_real_return": cash_rr, "rmd_start": rmd_start,
        "spend": spend,
        "spend_now": next((r["max_spend"] for r in rows if r["age"] == age), 0.0),
        "growth_path": path,
        "grow_to_67": at67,
        "stress": stress, "stress_ref_age": ref_age,
        "hist": hist,
        "catch_up": catch_up,
    }
