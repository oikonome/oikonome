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
import re


def real_return(nominal: float, inflation: float) -> float:
    return (1 + nominal) / (1 + inflation) - 1


from .compat import as_date
from .debt import TYPICAL_APR

# Card debt carried into the projection grows at a card's rate, whatever
# the market does: a crash shrinks the portfolio, never what is owed on it.
# The rate is the Debt page's (current_buckets' `debt_apr`); this default,
# the Debt page's typical card rate, is only for a caller that names none.
# Made real with the plan's inflation; the default below assumes 3%.
CARD_DEBT_NOMINAL = TYPICAL_APR["card"] / 100
CARD_DEBT_REAL = real_return(CARD_DEBT_NOMINAL, 0.03)


def _card_apr(conn, cards: list[tuple[str, str | None, float]]) -> float | None:
    """The balance-weighted rate (percent) the owed cards grow at, from the
    Debt page's per-card rates; None when no card is owed on."""
    from . import budget, debt
    owed = [c for c in cards if c[2] > 0]
    total = sum(c[2] for c in owed)
    if total <= 0:
        return None
    aprs = debt.card_aprs(conn, budget.load_config(conn), owed)
    return round(sum(c[2] * aprs[c[0]] for c in owed) / total, 3)


def debt_real_return(buckets: dict, inflation: float) -> float:
    """The real rate the projection grows card debt at: the Debt page's
    rate for these cards, or the default when nothing is owed."""
    apr = buckets.get("debt_apr")
    nominal = apr / 100 if apr is not None else CARD_DEBT_NOMINAL
    return real_return(nominal, inflation)


_PRE_TAX_SUBTYPES = ("401a", "401k", "403b", "457b", "ira", "sep ira",
                     "simple ira", "pension", "retirement", "keogh",
                     "sarsep", "profit sharing plan", "thrift savings plan",
                     # an annuity's gains come out as ordinary income
                     "fixed annuity", "variable annuity", "other annuity",
                     # the Canadian and UK pre-tax plans
                     "rrsp", "rrif", "lira", "lif", "lrif", "lrsp", "prif",
                     "rlif", "sipp")

# "roth" as a word in an account's name: "My Roth401k", never "Brothers"
_ROTH_WORD = re.compile(r"\broth(?![a-z])")
# The word IRA in a bank account's name, as a product writes it — in
# capitals too — never a person called Ira ("Ira's Checking", "IRA SAMPLE
# SAVINGS"). Read word by word: the possessive is another word and never
# counts; an everyday-account word anywhere makes it cash; a retirement
# word beside IRA decides; otherwise IRA counts as the whole name, as the
# first word followed by a deposit-product word ("IRA Savings", "IRA
# MONEY MARKET"), or as the last word after one ("Share Certificate IRA").
_NAME_WORDS = re.compile(r"[a-z0-9]+(?:['’][a-z]+)?")
_IRA_RETIREMENT = frozenset(("roth", "traditional", "sep", "simple",
                             "rollover"))
_IRA_PRODUCT = frozenset(("savings", "share", "money", "market", "cd",
                          "certificate", "term"))
_EVERYDAY = frozenset(("checking", "draft", "primary", "joint"))


def _names_an_ira(name: str, subtype: str = "") -> bool:
    words = _NAME_WORDS.findall(name.lower())
    if subtype == "checking" or "ira" not in words \
            or _EVERYDAY.intersection(words):
        return False
    if _IRA_RETIREMENT.intersection(words):
        return True
    if words[0] == "ira" and (len(words) == 1 or words[1] in _IRA_PRODUCT):
        return True
    return len(words) > 1 and words[-1] == "ira" \
        and words[-2] in _IRA_PRODUCT


def bucket_of(acct_type: str | None, subtype: str | None,
              name: str | None) -> str | None:
    """The tax treatment the projection gives an account: "debt" (a card),
    "roth", "td", "taxable", "cash", or None (not part of the nest egg).
    A bank account's name is read only for the word IRA
    (specs/retirement-figures.md)."""
    sub = (subtype or "").lower()
    if acct_type == "credit":
        return "debt"
    n = (name or "").lower()
    # a bank account is cash whatever a bank calls the product ("Traditional
    # Savings"); a health savings subtype makes it tax-deferred, and so
    # does the word IRA — a credit union's IRA share or IRA CD is typed as
    # a bank account — Roth when the name also says Roth
    if acct_type == "depository":
        if sub in ("hsa", "hra", "health reimbursement arrangement"):
            return "td"
        if _names_an_ira(name or "", sub):
            return "roth" if _ROTH_WORD.search(n) else "td"
        return "cash"
    # Roth decides FIRST, whatever the plan wrapper is. Plaid ships a
    # distinct "roth 401k" subtype (and "roth 403b"), which is neither
    # "roth" nor a member of the pre-tax tuple below: checking the
    # employer-plan subtypes first would send a Roth 401(k) to `td` —
    # or, when the subtype misses both, to `taxable`, where withdrawals
    # take a capital-gains haircut on money that is tax-free by
    # definition.
    # a TFSA is the same promise: withdrawals are tax-free
    if "roth" in sub or sub == "tfsa" or _ROTH_WORD.search(n):
        return "roth"
    # every pre-tax wrapper Plaid names, plus the flat "retirement"/
    # "pension" stamp aggregators use when they only know the money is
    # a retirement plan — pre-tax is the safe reading for those, since
    # a Roth was already caught above
    if sub in _PRE_TAX_SUBTYPES or "traditional" in n:
        return "td"
    # a health savings account is pre-tax money — ordinary income when
    # drawn after 65, tax-free for medical costs — so tax-deferred is the
    # conservative reading; as a brokerage it would take a capital-gains
    # haircut it never owes
    if sub in ("hsa", "hra", "health reimbursement arrangement"):
        return "td"
    # education savings pay for tuition, not a retirement: never part of
    # the nest egg the simulation spends. A UGMA/UTMA custodial account
    # (and a Canadian RESP) is the same story with a stronger reason:
    # the money legally belongs to the child, the parents cannot spend it;
    # a Canadian disability savings plan (RDSP) belongs to its beneficiary
    if sub in ("529", "education savings account", "ugma", "utma", "resp",
               "rdsp"):
        return None
    if sub == "crypto":
        return "taxable"                                # crypto = cap gains
    if acct_type == "investment":
        return "taxable"
    return None


def _nest_egg_rows(conn):
    """The accounts the projection reads (shadows and, unless entities are
    combined, business-entity accounts left out)."""
    from . import links
    shadows = links.shadow_ids(conn)
    return conn.execute(
        """SELECT a.id, a.name, a.type, a.subtype, a.balance_current bal,
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


def current_buckets(conn) -> dict:
    """Group live balances into the four tax treatments the sim needs.

    Linked shadow accounts (lower-ranked sources of the same
    real-world account) are excluded, same rule as net worth
    (`reporting._live_accounts`) — a dual-source 401k must count once,
    not once per aggregator. Cards come from the SAME rows (one scope for
    what is owned and what is owed), each at the rate the Debt page walks
    it at (`debt.card_aprs`); a card's credit balance nets the other way.
    Loans stay out: their payments are already inside the ledger spend
    the plan draws, so netting the balance too would count them twice."""
    b = {"cash": 0.0, "taxable": 0.0, "td": 0.0, "roth": 0.0, "debt": 0.0}
    cards: list[tuple[str, str | None, float]] = []
    for r in _nest_egg_rows(conn):
        v = r["bal"] or 0.0
        kind = bucket_of(r["type"], r["subtype"], r["name"])
        if kind is None:
            continue
        b[kind] += v                                        # debt: owed (+)
        if kind == "debt":
            cards.append((r["id"], r["subtype"], float(v)))
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
    b["debt"] = round(b["debt"], 2)
    b["debt_apr"] = _card_apr(conn, cards)
    spill = b["debt"]
    for k in ("cash", "taxable", "td", "roth"):
        take = min(b[k], spill)
        b[k] -= take
        spill -= take
        # what the page says each bucket is net of (specs/retirement-figures.md)
        b[f"cards_from_{k}"] = round(max(take, 0.0), 2)
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
             cash_rr=0.0, rmd_start=RMD_START, extra_annual=0.0,
             debt_rr=CARD_DEBT_REAL, trace=None, roth_annual=0.0):
    """Run age→end_age. Returns dict(survived, fail_age, end_balance,
    at_retire). `trace`, a list, gets each year's closing balance (the
    chart's points); the walk stops appending where the money runs out.

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
    I have to start doing". debt_rr: the real rate card debt grows at —
    its own, never the market's (shock and rr_path do not touch it).
    roth_annual: saving into Roth accounts, added beside td_annual from
    resume_age (specs/retirement-figures.md, "Saving is split by tax
    treatment"); zero leaves every figure bit-identical."""
    # The four buckets live in locals for the whole walk. A projection runs
    # this loop thousands of times (every bisection step of every retire
    # age, every historical path), so a dict rebuilt per simulated year is
    # most of the page's cost. The arithmetic — growth factor, draw order,
    # the order the four balances are summed — is unchanged, operation for
    # operation, so the numbers are bit-identical to the dict form.
    cash, taxable, td, roth = (buckets["cash"], buckets["taxable"],
                               buckets["td"], buckets["roth"])
    # Card debt beyond every bucket (current_buckets' debt_residual) is a
    # starting liability, not nothing: it grows at the card's own rate,
    # savings pay it before they build anything, and a retiree still owing
    # it pays it before spending. Zero leaves every figure bit-identical
    # (x - 0.0 is x).
    debt = buckets.get("debt_residual") or 0.0
    gd = 1 + debt_rr
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
            at_retire = cash + taxable + td + roth - debt
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
        if debt:
            debt = debt * gd
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
                if roth_annual:
                    roth += roth_annual                     # Roth saving
            taxable += rmd_net
            if extra_annual:
                taxable += extra_annual
            if debt and taxable > 0:
                pay = min(taxable, debt)
                taxable -= pay
                debt -= pay
        else:
            income = (ss_net if a >= ss_age else 0.0) + rent_net + rmd_net
            need = spend - income
            if debt:
                need += debt                                # settle it first
                debt = 0.0
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
        if trace is not None:
            trace.append(cash + taxable + td + roth - debt)
    if at_retire is None:                                    # retire_age > end_age
        at_retire = cash + taxable + td + roth - debt
    return {"survived": True, "fail_age": None,
            "end_balance": cash + taxable + td + roth - debt,
            "at_retire": at_retire}


def _max_spend(buckets, age, retire_age, end_age, rr, ss_annual, ss_age,
               rental_annual, td_annual, taxable_annual, resume_age,
               eff_ord, eff_cg, gain_frac, shock=(), cash_rr=0.0,
               rmd_start=RMD_START, debt_rr=CARD_DEBT_REAL, roth_annual=0.0):
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
                      rmd_start=rmd_start, debt_rr=debt_rr,
                      roth_annual=roth_annual)["survived"]
        if ok:
            lo = mid
        else:
            hi = mid
    return lo


# ---- historical-sequence analysis (FIRECalc-style) ------------------------
# Replay the plan against EVERY contiguous window of real 1928-2025 returns
# (wrapping), blended stock/bond. Deterministic — no RNG — and explainable:
# "survived N of 98 historical sequences".


# the history replay leads the page (specs/retirement-figures.md): an age
# "works" when it lasts in at least this share of past sequences
HIST_LEAD_PCT = 90.0

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


def hist_catch_up_monthly(buckets, age, retire_age, end_age, spend,
                          stock_frac, kw, target_pct=HIST_LEAD_PCT):
    """Smallest extra monthly saving, from now until `retire_age`, that
    makes retiring then last in `target_pct` of past sequences — rounded
    UP to the step. None when no saving under the cap is enough or there
    are no working years left."""
    paths = _hist_paths(end_age - age + 1, stock_frac)
    need = int(-(-target_pct / 100 * len(paths) // 1))

    def ok(extra):
        good = 0
        for i, p in enumerate(paths):
            if simulate(buckets, age, retire_age, end_age, spend, rr_path=p,
                        extra_annual=extra, **kw)["survived"]:
                good += 1
                if good >= need:
                    return True
            if good + (len(paths) - 1 - i) < need:
                return False
        return good >= need
    if ok(0.0):
        return 0
    if retire_age <= age or not ok(CATCH_UP_CAP):
        return None
    lo, hi = 0.0, CATCH_UP_CAP
    # 20 halvings resolve $600k/yr to under $1/yr; the answer is rounded
    # up to $10 a month
    for _ in range(20):
        mid = (lo + hi) / 2
        if ok(mid):
            hi = mid
        else:
            lo = mid
    return int(-(-hi / 12 // CATCH_UP_STEP) * CATCH_UP_STEP)


def _verdict(b, age, end_age, spend, stock_frac, kw, hist_by_age, earliest,
             ref_age, retire_max) -> dict:
    """The page's headline, from the history replay: the earliest age that
    lasts in HIST_LEAD_PCT of past sequences, its share, and — when no age
    does — the saving that would make one (specs/retirement-figures.md)."""
    lead = next((ra for ra in range(age, retire_max + 1)
                 if hist_by_age[ra]["pct"] >= HIST_LEAD_PCT), None)
    at = lead if lead is not None else retire_max
    h = hist_by_age[at]
    catch_up = None
    if lead is None:
        catch_up = []
        for ra in sorted({ref_age, retire_max}):
            m = hist_catch_up_monthly(b, age, ra, end_age, spend, stock_frac,
                                      kw)
            if m:
                catch_up.append({"age": ra, "extra_monthly": m})
    return {"basis": "history", "age": lead, "at_age": at,
            "ok": h["ok"], "n": h["n"], "pct": h["pct"],
            "in_ten": h["ok"] * 10 // h["n"] if h["n"] else 0,
            "threshold_pct": HIST_LEAD_PCT,
            "tone": "good" if lead is not None else "bad",
            "steady_age": earliest, "catch_up": catch_up}


def _median_path(b, age, retire_age, end_age, spend, stock_frac, kw,
                 today: dt.date) -> list:
    """The balance year by year, the median across every past sequence,
    retiring at `retire_age`: points [YYYY-MM, balance] from this month to
    `end_age` (specs/retirement-figures.md)."""
    paths = _hist_paths(end_age - age + 1, stock_frac)
    traces = []
    for p in paths:
        tr: list[float] = []
        simulate(b, age, retire_age, end_age, spend, rr_path=p, trace=tr,
                 **kw)
        tr += [0.0] * (end_age - age + 1 - len(tr))
        traces.append(tr)
    start = (b["cash"] + b["taxable"] + b["td"] + b["roth"]
             - (b.get("debt_residual") or 0.0))
    out = [[f"{today.year}-{today.month:02d}", round(start, 2)]]
    for k in range(end_age - age + 1):
        col = sorted(t[k] for t in traces)
        n = len(col)
        mid = (col[n // 2] if n % 2
               else (col[n // 2 - 1] + col[n // 2]) / 2)
        out.append([f"{today.year + k + 1}-{today.month:02d}",
                    round(max(mid, 0.0), 2)])
    return out


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
                 cash_rr=0.0, rmd_start=RMD_START, eff_ord=0.0,
                 debt_rr=CARD_DEBT_REAL, roth_annual=0.0):
    """Portfolio value (today's $) year by year if you KEEP working & saving.

    Point k is the balance after k years, i.e. at age `age + k` — the same
    figure `simulate` reports as `at_retire` for retiring at that age, so
    the chart and the retire-age table never disagree by a year of growth.
    Same year model as `simulate`: grow, force any RMD, contribute."""
    yr0 = dt.date.today().year
    bb = {k: b[k] for k in ("cash", "taxable", "td", "roth")}
    # card debt beyond every bucket, carried and paid as `simulate` does
    debt = b.get("debt_residual") or 0.0
    path = [[f"{yr0}-06", round(bb["cash"] + bb["taxable"] + bb["td"] + bb["roth"]
                                - debt, 2)]]
    for a in range(age, horizon):
        bb = _grow(bb, rr, cash_rr)
        if debt:
            debt *= 1 + debt_rr
        if a >= rmd_start and bb["td"] > 0:
            gross = bb["td"] / RMD_DIVISOR.get(min(a, 100), 6.4)
            bb["td"] -= gross
            bb["taxable"] += gross * (1 - eff_ord)
        if a >= resume_age:
            bb["td"] += td_annual
            bb["taxable"] += taxable_annual
            if roth_annual:
                bb["roth"] += roth_annual
        if debt and bb["taxable"] > 0:
            pay = min(bb["taxable"], debt)
            bb["taxable"] -= pay
            debt -= pay
        path.append([f"{yr0 + (a - age) + 1}-06",
                     round(bb["cash"] + bb["taxable"] + bb["td"] + bb["roth"]
                           - debt, 2)])
    return path


def age_from_birthdate(birthdate: str, today: dt.date | None = None) -> int:
    """Exact age from an ISO birthdate (per-tenant config `birthdate`)."""
    bd = dt.date.fromisoformat(birthdate)
    today = today or dt.date.today()
    return today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))


# Under this many days of ledger a spend rate is too short to stretch over
# a year (one month of history can be a holiday month or a move).
MIN_SPEND_DAYS = 90


def default_spend(conn, floor: float = 24_000,
                  fallback: int | None = None) -> int | None:
    """The page's honest starting point: the household's OWN last-12-months
    spend (the product's spend definition), rounded to $1k. A fixed
    default is wrong for any household whose real spend differs from it,
    and reads as broken; the ledger is the right source. When the ledger
    is too thin to mean anything the answer is `fallback` — None, so the
    page asks for a spend rather than projecting an assumed one.

    A ledger younger than a year is annualized from the days it covers:
    days before its first row are no data, not days of no spending, so
    three months of history are read as a quarter of a year at the same
    rate, never as a whole year of a quarter of the spending."""
    from .reporting import (NET_AMOUNT_JOINED, REIMB_PARTIAL_JOIN,
                            SPEND_WHERE, excluded_accounts_sql)
    # net of partial reimbursements, like every other spend aggregate — an
    # expensed-and-repaid card charge is not money the household lives on —
    # and without the accounts the household excluded, as every spend
    # report drops them. The first date comes from the SAME rows the sum
    # reads: an old business, loan or shadow account the spend leaves out
    # must not make a young personal ledger look a year deep.
    excl, ex_p = excluded_accounts_sql(conn)
    row = conn.execute(
        f"SELECT MIN(t.date) AS first, COALESCE(SUM({NET_AMOUNT_JOINED})"
        f" FILTER (WHERE t.date >= CURRENT_DATE - 365),0) AS s "
        f"FROM transactions t {REIMB_PARTIAL_JOIN} "
        f"WHERE {SPEND_WHERE} {excl}",
        ex_p).fetchone()
    first = row["first"]
    if first is None:
        return fallback
    if isinstance(first, str):
        first = dt.date.fromisoformat(first[:10])
    covered = (dt.date.today() - first).days + 1
    if covered < MIN_SPEND_DAYS:
        return fallback
    s = float(row["s"] or 0)
    if covered < 365:
        s = s * 365 / covered
    return int(round(s, -3)) if s >= floor else fallback


# money arriving in a retirement account from outside it: a workplace
# plan's payroll contribution rows (sync/plan_csv stores them under
# PLAN_CONTRIBUTION), and a deposit the feed names as one
_SAVING_DETAILED = ("PLAN_CONTRIBUTION",
                    "TRANSFER_IN_INVESTMENT_AND_RETIREMENT_FUNDS",
                    "TRANSFER_IN_DEPOSIT", "TRANSFER_IN_ACCOUNT_TRANSFER")
SAVING_MIN_MONTHS = 3
# a deposit that names itself as money already in a retirement plan
_NOT_NEW_MONEY = re.compile(r"roll\s*-?\s*over|conver(sion|t)", re.I)


def _own_transfer_partners(conn, account: str, since: dt.date,
                           until: dt.date) -> dict[str, str]:
    """`account`'s own-account transfer rows dated in (since, until], each
    with the account its partner leg is on: the pairing
    `forecast_score.own_transfer_ids` makes (both legs transfer-typed, the
    opposite amount, within OWN_TRANSFER_DAYS, one to one, the nearest day
    first, then the partner's day and id), keeping the partner it chose."""
    from .forecast_score import OWN_TRANSFER_DAYS, TRANSFERS
    pad = dt.timedelta(days=OWN_TRANSFER_DAYS)
    legs = conn.execute(
        """SELECT id, date, ROUND(amount * 100) AS c FROM transactions
            WHERE account_id = %s AND removed = 0 AND pending = 0
              AND category_primary = ANY(%s) AND date > %s AND date <= %s
            ORDER BY date, id""",
        (account, list(TRANSFERS), since, until)).fetchall()
    if not legs:
        return {}
    others = conn.execute(
        """SELECT id, account_id, date, ROUND(amount * 100) AS c
             FROM transactions
            WHERE account_id <> %s AND removed = 0 AND pending = 0
              AND category_primary = ANY(%s) AND date >= %s AND date <= %s
            ORDER BY date, id""",
        (account, list(TRANSFERS), since - pad, until + pad)).fetchall()
    by_amount: dict[int, list] = {}
    for o in others:
        by_amount.setdefault(int(o["c"]), []).append(o)
    cands = []
    for leg in legs:
        d0 = as_date(leg["date"])
        for o in by_amount.get(-int(leg["c"]), ()):
            gap = abs((as_date(o["date"]) - d0).days)
            if gap <= OWN_TRANSFER_DAYS:
                cands.append((gap, as_date(o["date"]), str(o["id"]),
                              d0, str(leg["id"]), o["account_id"]))
    cands.sort()
    used: set[str] = set()
    out: dict[str, str] = {}
    for _g, _d, oid, _d0, lid, acct in cands:
        if oid in used or lid in out:
            continue
        used.add(oid)
        out[lid] = acct
    return out


def _moved_between_own_nest_egg(conn, ids: list[str], counted: list[str],
                                since: dt.date, until: dt.date) -> set[str]:
    """The deposits into `ids` that are the household's own money already
    inside the counted retirement money: each deposit's own paired partner
    leg (`_own_transfer_partners`) is on another of `counted`, the
    tax-deferred and Roth accounts. Money arriving from a bank account or
    from a brokerage is new retirement saving (a deposit into a brokerage
    is never counted as saving) and is not returned."""
    moved: set[str] = set()
    counted_set = set(counted)
    for acct in ids:
        for lid, partner in _own_transfer_partners(
                conn, acct, since, until).items():
            if partner != acct and partner in counted_set:
                moved.add(lid)
    return moved


def ledger_saving(conn, today: dt.date | None = None) -> dict | None:
    """What the household put into its retirement accounts over the last
    12 months, as a monthly average: {"monthly", "months", "td_monthly",
    "roth_monthly"} or None when nothing arrived — each deposit counted in
    the bucket of the account it landed in, `monthly` their sum (specs/retirement-figures.md). The accounts are the
    ones the projection counts as tax-deferred or Roth (bucket_of). Money
    moved in from another tax-deferred or Roth account (a conversion, a
    rollover) is not new saving; money moved in from a brokerage is. The average divides by the time their
    ledger covers, in days, when that is under a year, never fewer than
    SAVING_MIN_MONTHS; `months` is that span to one decimal."""
    today = today or dt.date.today()
    kinds = {r["id"]: bucket_of(r["type"], r["subtype"], r["name"])
             for r in _nest_egg_rows(conn)}
    ids = [a for a, k in kinds.items() if k in ("td", "roth")]
    if not ids:
        return None
    since = today - dt.timedelta(days=365)
    first = conn.execute(
        """SELECT MIN(date) AS first FROM transactions
            WHERE removed = 0 AND pending = 0
              AND account_id = ANY(%s) AND date <= %s""",
        (ids, today)).fetchone()["first"]
    if first is None:
        return None
    deposits = conn.execute(
        """SELECT id, account_id, amount, name FROM transactions
            WHERE removed = 0 AND pending = 0 AND account_id = ANY(%s)
              AND date > %s AND date <= %s AND amount < 0
              AND category_detailed = ANY(%s)""",
        (ids, since, today, list(_SAVING_DETAILED))).fetchall()
    moved = (_moved_between_own_nest_egg(conn, ids, ids, since, today)
             if deposits else set())
    by_kind = {"td": 0.0, "roth": 0.0}
    for r in deposits:
        if (str(r["id"]) not in moved
                and not _NOT_NEW_MONEY.search(r["name"] or "")):
            by_kind[kinds[r["account_id"]]] += -float(r["amount"])
    total = by_kind["td"] + by_kind["roth"]
    if total <= 0:
        return None
    covered_days = (today - as_date(first)).days + 1
    months = max(float(SAVING_MIN_MONTHS),
                 min(12.0, covered_days * 12 / 365))
    return {"monthly": round(total / months, 2), "months": round(months, 1),
            "td_monthly": round(by_kind["td"] / months, 2),
            "roth_monthly": round(by_kind["roth"] / months, 2)}


# the page's grid for a monthly saving figure, and the most it takes
SAVING_STEP = 50
SAVING_MAX = 10_000_000
# the saving answers, by the bucket each fills; `employer_mo` is the
# tax-deferred figure (its name is older than the Roth one)
SAVING_FIELDS = (("td", "employer_mo"), ("roth", "roth_mo"),
                 ("taxable", "taxable_mo"))
_SAVING_WORDS = {
    "td": "into tax-deferred accounts — taxed when you take it out",
    "roth": "into Roth accounts — not taxed when you take it out",
    "taxable": "into taxable accounts — gains taxed when you sell",
}


def _snap_saving(v) -> int:
    v = max(0, min(int(v), SAVING_MAX))
    return int(round(v / float(SAVING_STEP)) * SAVING_STEP)


def saving_plan(what_if: dict, answers: dict, found: dict | None) -> dict:
    """The monthly saving the projection counts, split by tax treatment
    (specs/retirement-figures.md, "Saving is split by tax treatment").

    `what_if`: the query's `employer_mo` / `roth_mo` / `taxable_mo` (None
    or absent = not asked); `answers`: the marked saved answers; `found`:
    `ledger_saving`'s reply. The base is the saved answers when any saving
    answer is among them (a missing one is $0, never guessed), else the
    ledger split, else nothing; each what-if then replaces its own field
    only — except that while the Roth figure is the ledger's, an
    `employer_mo` asked without `roth_mo` is an older client's figure, the
    saving that is not taxable-account saving (Roth inside it, the
    ledger's Roth first). Returns {td_mo, roth_mo, taxable_mo,
    employer_mo, seed_td_mo, seed_roth_mo, basis, source, parts, hint} —
    the seeds are what a current client's boxes open on; `employer_mo` is
    that older figure, the one every older client reads and sends back;
    `source` is
    the older three-valued name (a ledger figure reads as "saved"),
    `parts` each figure above $0 worded for the page, `hint` an offer of
    the ledger's Roth saving a saved block lacks."""
    saved = any(k in answers for _b, k in SAVING_FIELDS)
    if saved:
        base = {k: _snap_saving(answers.get(k, 0)) for _b, k in SAVING_FIELDS}
        basis = "saved"
    elif found is not None:
        # the total on the grid first, so the parts sum to the figure the
        # ledger shows; tax-deferred is what the Roth part leaves
        total = _snap_saving(round(found["monthly"]))
        roth = min(_snap_saving(round(found.get("roth_monthly", 0.0))), total)
        base = {"employer_mo": total - roth, "roth_mo": roth, "taxable_mo": 0}
        basis = "ledger"
    else:
        base = {k: 0 for _b, k in SAVING_FIELDS}
        basis = "none"
    asked = {k: v for k, v in (what_if or {}).items()
             if v is not None and k in base}
    # the Roth figure is the ledger's until a client names one
    roth_from_ledger = basis == "ledger" and "roth_mo" not in asked
    for k, v in asked.items():
        base[k] = _snap_saving(v)
    if roth_from_ledger and "employer_mo" in asked:
        # an older client's employer figure: tax-deferred + Roth, as it was
        # given; the ledger's Roth stays inside it, so an untouched
        # Recalculate moves nothing
        whole = base["employer_mo"]
        led_roth = min(_snap_saving(round(found.get("roth_monthly", 0.0))),
                       _snap_saving(round(found["monthly"])))
        base["roth_mo"] = min(led_roth, whole)
        base["employer_mo"] = whole - base["roth_mo"]
    if asked:
        basis = "what_if"
    parts = [{"kind": b, "monthly": base[k],
              "text": f"${base[k]:,}/mo {_SAVING_WORDS[b]}"}
             for b, k in SAVING_FIELDS if base[k] > 0]
    hint = None
    seed_td, seed_roth = base["employer_mo"], base["roth_mo"]
    found_roth = (_snap_saving(round(found.get("roth_monthly", 0.0)))
                  if found is not None else 0)
    # A saved tax-deferred figure with no Roth answer beside it may hold
    # the Roth saving (an older client saved the total there). The
    # projection counts it as saved; the boxes a current client fills are
    # seeded with the same total split by the ledger, so saving them
    # stores a split of that total, never an addition to it. (Not while
    # the person is trying a figure: it is counted as asked then.)
    offered = min(found_roth, base["employer_mo"])
    if (saved and "roth_mo" not in answers and "roth_mo" not in asked
            and "employer_mo" not in asked and offered > 0):
        seed_roth = offered
        seed_td = base["employer_mo"] - offered
        hint = (f"Your saved ${base['employer_mo']:,}/mo includes about "
                f"${offered:,}/mo that your last 12 months show going into "
                "Roth accounts — save it to count it as Roth.")
    return {"td_mo": base["employer_mo"], "roth_mo": base["roth_mo"],
            "employer_mo": base["employer_mo"] + (
                base["roth_mo"] if roth_from_ledger else 0),
            "taxable_mo": base["taxable_mo"], "basis": basis,
            "source": {"ledger": "saved"}.get(basis, basis),
            "seed_td_mo": seed_td, "seed_roth_mo": seed_roth,
            "parts": parts, "hint": hint}


def _ss_fra_months(birth_year: int | None) -> int:
    """Full retirement age in months: 66 through 1954 births, two months
    more a year to 67 for 1960 and later (67 when the year is unknown)."""
    if birth_year is None or birth_year >= 1960:
        return 67 * 12
    return 66 * 12 + 2 * max(0, birth_year - 1954)


def _ss_factor(age: int, fra: int) -> float:
    """SSA's benefit at claim age `age` as a share of the full-age one:
    5/9 % a month for the first 36 months early, 5/12 % beyond; 2/3 % a
    month of delayed credit after full age, to 70."""
    m = min(age, 70) * 12 - fra
    if m >= 0:
        return 1 + m * 2 / 300
    early = -m
    return 1 - min(early, 36) * 5 / 900 - max(early - 36, 0) * 5 / 1200


def ss_at_age(sched: dict[int, float], age: int,
              birth_year: int | None = None) -> float:
    """Monthly Social Security at claim age `age` from the saved estimates
    (claim age → $/mo). An age with no estimate of its own is the straight
    line between the nearest saved ages on either side; an age outside
    them is the nearest one moved by SSA's early-claim reduction or
    delayed credit (`birth_year` sets the full retirement age). No
    estimates at all is $0."""
    # an estimate is a claim age SSA allows (62-70); a restored config may
    # carry anything, and an age outside that range has no factor to scale by
    sched = {a: v for a, v in sched.items()
             if isinstance(a, int) and 62 <= a <= 70}
    if not sched:
        return 0.0
    if age in sched:
        return float(sched[age])
    below = [a for a in sched if a < age]
    above = [a for a in sched if a > age]
    if not below or not above:
        near = min(above) if not below else max(below)
        fra = _ss_fra_months(birth_year)
        return float(sched[near]) * _ss_factor(age, fra) / _ss_factor(near, fra)
    lo, hi = max(below), min(above)
    return sched[lo] + (sched[hi] - sched[lo]) * (age - lo) / (hi - lo)


def project_nothing(conn, *, nominal, inflation, cash_nominal=None,
                    birth_year=None) -> dict:
    """The payload when there is no spend to plan for (no answer saved and
    too little ledger to read one from): the household's balances and the
    rates it chose, every spend-based figure empty — the page asks for a
    spend instead of projecting an assumed one. Same keys as `project`."""
    b = current_buckets(conn)
    cash_rr = (real_return(cash_nominal, inflation)
               if cash_nominal is not None else 0.0)
    return {
        "buckets": b, "rows": [], "earliest": None,
        "real_return": real_return(nominal, inflation),
        "cash_real_return": cash_rr,
        "rmd_start": rmd_start_age(birth_year),
        "spend": None, "spend_now": None,
        "growth_path": [], "grow_to_67": None,
        "stress": [], "stress_ref_age": None, "stress_floor_spend": None,
        "stress_floor_vs_spend": None,
        "hist": None, "catch_up": None,
        "verdict": None, "growth_path_retire_age": None,
    }


def project(conn, *, age, end_age, spend, nominal, inflation, ss_monthly, ss_age,
            rental_monthly, td_annual, taxable_annual, resume_age,
            eff_ord=0.25, eff_cg=0.18, gain_frac=0.5, retire_max=72,
            stock_frac=0.9, cash_nominal=None, birth_year=None,
            roth_annual=0.0):
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
           ss_monthly, ss_age, rental_monthly, td_annual, roth_annual,
           taxable_annual, resume_age, eff_ord, eff_cg, gain_frac, retire_max, stock_frac,
           cash_rr, rmd_start, dt.date.today().replace(day=1))
    return copy.deepcopy(_project_cached(key))


@functools.lru_cache(maxsize=64)
def _project_cached(key: tuple) -> dict:
    (b_items, age, end_age, spend, nominal, inflation, ss_monthly, ss_age,
     rental_monthly, td_annual, roth_annual, taxable_annual, resume_age,
     eff_ord, eff_cg, gain_frac, retire_max, stock_frac, cash_rr, rmd_start,
     month) = key
    b = dict(b_items)
    rr = real_return(nominal, inflation)
    ss_annual = ss_monthly * 12
    rental_annual = rental_monthly * 12
    kw = dict(rr=rr, ss_annual=ss_annual, ss_age=ss_age, rental_annual=rental_annual,
              td_annual=td_annual, taxable_annual=taxable_annual, resume_age=resume_age,
              eff_ord=eff_ord, eff_cg=eff_cg, gain_frac=gain_frac,
              cash_rr=cash_rr, rmd_start=rmd_start,
              debt_rr=debt_real_return(b, inflation), roth_annual=roth_annual)
    retire_max = max(retire_max, age)     # age>72 must still yield ≥1 row
    rows, earliest = [], None
    for ra in range(age, retire_max + 1):
        sim = simulate(b, age, ra, end_age, spend, **kw)
        ms = _max_spend(b, age, ra, end_age, **kw)
        rows.append({"age": ra, "at_retire": sim["at_retire"],
                     "feasible": sim["survived"], "fail_age": sim["fail_age"],
                     "max_spend": ms,
                     # the row's green figure: its most a year covers the
                     # target (the clients colour by it, compare nothing)
                     "meets_target": ms >= spend})
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
                       "max_spend_at_ref": s_max,
                       "under_target": s_max < spend})
    # the most a year that survives the worst of the bad-market scenarios —
    # every row but the first, which is average markets
    stress_floor = min((s["max_spend_at_ref"] for s in stress[1:]),
                       default=None)

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
    hist_by_age = {ra: hist_success(b, age, ra, end_age, spend, stock_frac, kw)
                   for ra in range(age, retire_max + 1)}
    for row in rows:
        h = hist_by_age.get(row["age"])
        row["hist_pct"] = h["pct"] if h else None
        row["hist_lasts"] = bool(h) and h["pct"] >= HIST_LEAD_PCT
    verdict = _verdict(b, age, end_age, spend, stock_frac, kw, hist_by_age,
                       earliest, ref_age, retire_max)
    chart_age = verdict["age"] if verdict["age"] is not None else ref_age

    # the steady keep-working balance, read only for its value at 67
    horizon = min(end_age, 67)
    path = _growth_path(b, age, horizon, rr, td_annual, taxable_annual, resume_age,
                        cash_rr=cash_rr, rmd_start=rmd_start, eff_ord=eff_ord,
                        debt_rr=kw["debt_rr"], roth_annual=roth_annual)
    # point k is the balance at age + k; with no point at 67 (already past
    # it, or a plan ending first) there is no "by 67" figure to show
    at67 = path[67 - age][1] if 0 <= 67 - age < len(path) else None
    return {
        "verdict": verdict,
        "growth_path_retire_age": chart_age,
        "buckets": b, "rows": rows, "earliest": earliest,
        "real_return": rr, "cash_real_return": cash_rr, "rmd_start": rmd_start,
        "spend": spend,
        "spend_now": next((r["max_spend"] for r in rows if r["age"] == age), 0.0),
        "growth_path": _median_path(b, age, chart_age, end_age, spend,
                                    stock_frac, kw, month),
        "grow_to_67": at67,
        "stress": stress, "stress_ref_age": ref_age,
        "stress_floor_spend": stress_floor,
        # how far the target spend sits under (+) or over (−) it
        "stress_floor_vs_spend": (stress_floor - spend
                                  if stress_floor is not None else None),
        "hist": hist,
        "catch_up": catch_up,
    }
