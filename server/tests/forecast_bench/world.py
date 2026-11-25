"""A household's money, simulated with known ground truth.

`simulate(persona, seed)` is pure: no database, no clock, no global random
state. It draws everything random first (`_draw`: paydays and amounts, the
bill schedule, every intended purchase, one-offs, the hand payer's paydays,
the household's reaction strength) from generators seeded by
`(persona, seed, stream)`, then `_realize` walks the days in order and
applies what depends on the balances as they stand — card statements and
payments, savings sweeps, overdraft fees and the household's reaction to a
dip. Re-realizing the same draws with the reaction switched off after a
day gives `World.counterfactual(day)`: the balance had the household not
reacted, which the shortfall metrics count against.

Money is kept in integer cents inside the walk, so the truth reconciles
with the ledger to the cent. Signs follow Plaid: a positive amount is money
out of the account (a charge on a card, a debit on checking).

The true checking balance (`World.checking`) is the AVAILABLE balance at
the end of each day, as a bank reports it: a debit counts from the day it
was made (pending), a credit from the day it posted.
"""

from __future__ import annotations

import calendar
import datetime as dt
import functools
import math
import random
import zlib
from dataclasses import dataclass, field

from .personas import PERSONAS, Persona

DAY = dt.timedelta(days=1)

# The default world: twelve months of history, forecasts made in the
# thirteenth month, two months of outcome after the last of them. Starts
# on a Monday so a biweekly `anchor` of 4 is a Friday.
DEFAULT_START = dt.date(2024, 11, 4)
DEFAULT_ASOF = (dt.date(2025, 11, 1), dt.date(2025, 11, 9),
                dt.date(2025, 11, 16), dt.date(2025, 11, 24))
DEFAULT_END = dt.date(2026, 1, 31)
# long mode: 24 months of history, weekly forecasts over the last twelve
LONG_END = dt.date(2027, 1, 2)


def long_asof() -> tuple[dt.date, ...]:
    d, out = dt.date(2025, 11, 1), []
    while d <= dt.date(2026, 10, 31):
        out.append(d)
        d += 7 * DAY
    return tuple(out)


def rng_for(key: str, seed: int, stream: str) -> random.Random:
    """A generator of its own per (persona, seed, stream). `hash()` of a
    string changes between processes, so the seed is a CRC of the text."""
    return random.Random(zlib.crc32(f"{key}|{seed}|{stream}".encode()))


# ---- calendars --------------------------------------------------------------

_US = (("fixed", 1, 1), ("nth", 1, 0, 3), ("nth", 2, 0, 3),
       ("nth", 5, 0, -1), ("fixed", 6, 19), ("fixed", 7, 4),
       ("nth", 9, 0, 1), ("nth", 10, 0, 2), ("fixed", 11, 11),
       ("nth", 11, 3, 4), ("fixed", 12, 25))
# an invented non-US calendar: none of these is a US bank holiday
_OTHER = (("fixed", 1, 2), ("fixed", 1, 6), ("fixed", 5, 1),
          ("fixed", 8, 15), ("fixed", 11, 1), ("fixed", 12, 26),
          ("nth", 4, 0, 1), ("nth", 6, 0, 2))


def _nth(y: int, m: int, wd: int, n: int) -> dt.date:
    days = [d for d in range(1, calendar.monthrange(y, m)[1] + 1)
            if dt.date(y, m, d).weekday() == wd]
    return dt.date(y, m, days[n - 1 if n > 0 else n])


@functools.lru_cache(maxsize=64)
def holidays(cal: str, year: int) -> frozenset:
    out = set()
    for r in (_US if cal == "us" else _OTHER):
        if r[0] == "fixed":
            d = dt.date(year, r[1], r[2])
            if cal == "us" and d.weekday() == 6:
                d += DAY
        else:
            d = _nth(year, r[1], r[2], r[3])
        out.add(d)
    return frozenset(out)


def business_day(d: dt.date, cal: str) -> bool:
    return d.weekday() < 5 and d not in holidays(cal, d.year)


def next_bd(d: dt.date, cal: str) -> dt.date:
    while not business_day(d, cal):
        d += DAY
    return d


def prev_bd(d: dt.date, cal: str) -> dt.date:
    while not business_day(d, cal):
        d -= DAY
    return d


def _clamp(y: int, m: int, day: int) -> dt.date:
    return dt.date(y, m, min(day, calendar.monthrange(y, m)[1]))


def _add_months_clamped(d: dt.date, k: int, day: int) -> dt.date:
    y, m = divmod(d.month - 1 + k, 12)
    return _clamp(d.year + y, m + 1, day)


def _months(start: dt.date, end: dt.date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# ---- the record ---------------------------------------------------------------


@dataclass
class Txn:
    id: str
    account: str
    date: dt.date          # the day the money moved (authorised, initiated)
    post: dt.date          # the day it posted (== date when never pending)
    amount: float          # Plaid's sign: positive = money out
    name: str
    primary: str
    detailed: str
    kind: str              # paycheck bill variable one_off card_payment
    #                        transfer refund fee interest cash
    scheduled: bool        # could a household know it in advance
    pending_id: str | None = None
    series: str | None = None
    pair: str | None = None


@dataclass
class Statement:
    card: str
    close: dt.date
    balance: float
    minimum: float
    due: dt.date
    interest: float
    carried: float         # the previous statement's balance


@dataclass
class Occurrence:
    nominal: dt.date       # the day its schedule names
    day: dt.date           # the day it really moved
    amount: float
    account: str


@dataclass
class Series:
    payee: str
    kind: str              # bill | income
    cadence: str
    spec: dict
    plantable: bool
    category: tuple
    occurrences: list = field(default_factory=list)


@dataclass
class World:
    persona: Persona
    seed: int
    start: dt.date
    end: dt.date
    accounts: list
    ledger: list
    statements: dict
    checking: dict         # date -> end-of-day available balance, dollars
    posted: dict           # account -> {date: posted balance (cards: debt)}
    available: dict        # account -> {date: available balance}
    schedule: list
    reactions: list        # (first day, last day, cut, rescued)
    income_monthly: float
    spend_monthly: float
    checking_id: str = "chk"
    _plan: object = None
    _cf: dict = field(default_factory=dict)

    def counterfactual(self, d0: dt.date) -> dict:
        """The checking balance by day had the household not reacted to a
        dip after `d0`: the same draws, the reaction off from d0 + 1."""
        if d0 not in self._cf:
            self._cf[d0] = _realize(self.persona, self.seed, self._plan,
                                    react_until=d0)["checking"]
        return self._cf[d0]

    @property
    def scale(self) -> float:
        """I: the money the household moves in a month — its mean monthly
        income, or its mean monthly spending when that is larger (a
        household living off savings, a near-zero one). Errors are read as
        shares of it."""
        return max(self.income_monthly, self.spend_monthly)


# ---- names (all invented) ---------------------------------------------------

_FOOD = ("Maplewood Grocer", "Fernhill Market", "Bluepine Cafe",
         "Saltmarsh Diner", "Copperkettle Deli", "Thistle Bakehouse")
_OTHER_SHOPS = (("Northgate Hardware", "GENERAL_MERCHANDISE",
                 "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE"),
                ("Pinecone Books", "GENERAL_MERCHANDISE",
                 "GENERAL_MERCHANDISE_BOOKSTORES_AND_NEWSSTANDS"),
                ("Tidewater Fuel", "TRANSPORTATION",
                 "TRANSPORTATION_GAS"),
                ("Glimmer Cinema", "ENTERTAINMENT",
                 "ENTERTAINMENT_TV_AND_MOVIES"),
                ("Oakridge Outfitters", "GENERAL_MERCHANDISE",
                 "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES"),
                ("Willowbend Pharmacy", "MEDICAL",
                 "MEDICAL_PHARMACIES_AND_SUPPLEMENTS"))
# one-offs come from many merchants, as real ones do: a place name and a
# trade, each pair its own merchant
_ONE_OFF_PLACES = ("Lakeside", "Summitline", "Riverbend", "Clearwater",
                   "Bramble", "Copperfield", "Hollow Creek", "Fernwood",
                   "Moss Hill", "Quarry Lane")
_ONE_OFF_TRADES = (("Motors", "TRANSPORTATION",
                    "TRANSPORTATION_OTHER_TRANSPORTATION"),
                   ("Appliances", "HOME_IMPROVEMENT",
                    "HOME_IMPROVEMENT_FURNITURE"),
                   ("Dental Studio", "MEDICAL", "MEDICAL_DENTAL_CARE"),
                   ("Roofing", "HOME_IMPROVEMENT",
                    "HOME_IMPROVEMENT_REPAIR_AND_MAINTENANCE"),
                   ("Travel Co", "TRAVEL", "TRAVEL_LODGING"),
                   ("Veterinary", "GENERAL_SERVICES",
                    "GENERAL_SERVICES_OTHER_GENERAL_SERVICES"))
_SEASON = {"heating": {12: 2.0, 1: 2.0, 2: 1.8, 11: 1.4, 3: 1.3, 4: 0.9,
                       5: 0.6, 6: 0.5, 7: 0.5, 8: 0.5, 9: 0.6, 10: 0.9},
           "cooling": {6: 1.3, 7: 1.6, 8: 1.6, 9: 1.2, 12: 0.9, 1: 0.9,
                       2: 0.9, 3: 0.85, 4: 0.85, 5: 0.95, 10: 0.9,
                       11: 0.9}}
_MONTHS_OF = {"monthly": 1, "quarterly": 3, "semiannual": 6, "annual": 12}


def _cents(x: float) -> int:
    return int(round(x * 100))


# ---- the draw -----------------------------------------------------------------


@dataclass
class _Plan:
    start: dt.date
    end: dt.date
    cal: str
    incomes: list          # (day, nominal, series, amount, account, cat, plantable)
    bills: list            # (day, nominal, series, amount, account, cat)
    intents: dict          # day -> [(cat, merchant, primary, detailed,
    #                          amount, account, lag, discretionary)]
    one_offs: list         # (day, merchant, primary, detailed, amount, acct, lag)
    cash: list             # (day, amount)
    refund: tuple | None   # (intent day, intent index, refund day)
    hand_days: dict        # card -> set of days it is paid by hand
    paydays: list          # the primary paydays (sweeps, transfers)
    cuts: dict             # day -> reaction strength drawn for that day
    schedule: list


def _income_days(inc: dict, start: dt.date, end: dt.date, cal: str,
                 rng: random.Random) -> list[tuple[dt.date, dt.date, float]]:
    cad = inc["cadence"]
    out: list[tuple[dt.date, dt.date]] = []
    if cad in ("weekly", "biweekly"):
        step = 7 if cad == "weekly" else 14
        d = start + dt.timedelta(days=inc.get("anchor", 4))
        while d <= end:
            out.append((d, prev_bd(d, cal)))
            d += dt.timedelta(days=step)
    elif cad == "semimonthly":
        for y, m in _months(start, end):
            for day in (15, 31):
                n = _clamp(y, m, day)
                out.append((n, prev_bd(n, cal)))
    elif cad == "monthly":
        for y, m in _months(start, end):
            n = _clamp(y, m, inc["day"])
            out.append((n, prev_bd(n, cal)))
    elif cad == "weekday_rule":
        for y, m in _months(start, end):
            n = _nth(y, m, inc["weekday"], inc["nth"])
            out.append((n, prev_bd(n, cal)))
    elif cad == "quarterly":
        for y, m in _months(start, end):
            if m in inc["months"]:
                n = _clamp(y, m, inc["day"])
                out.append((n, prev_bd(n, cal)))
    elif cad == "gig":
        d = start
        while d <= end:
            if d.weekday() in inc["weekdays"] and rng.random() >= inc["p_dry"]:
                out.append((d, next_bd(d, cal)))
            d += DAY
    else:
        raise ValueError(f"unknown income cadence {cad}")
    res = []
    lo = start + dt.timedelta(days=inc.get("start_day", 0))
    hi = (start + dt.timedelta(days=inc["end_day"])
          if inc.get("end_day") is not None else end)
    for nominal, day in out:
        amt = inc["amount"] * max(0.25, 1 + rng.gauss(0, inc.get("cv", 0)))
        late = inc.get("late")
        if late and rng.random() < late[0]:
            day = next_bd(day + dt.timedelta(days=rng.randint(2, late[1])),
                          cal)
        if lo <= nominal <= hi and start <= day <= end:
            res.append((day, nominal, round(amt, 2)))
    # the first `checks` deposits of each new calendar year smaller by
    # `share` (deductions that reset with the year); no draw, so the
    # streams of every other persona are unchanged
    ys = inc.get("year_start")
    if ys and res:
        first_year, count, out2 = res[0][0].year, {}, []
        for day, nominal, amt in res:
            if day.year != first_year:
                k = count.get(day.year, 0)
                if k < ys["checks"]:
                    amt = round(amt * (1 - ys["share"]), 2)
                count[day.year] = k + 1
            out2.append((day, nominal, amt))
        res = out2
    return res


def _bill_nominals(b: dict, start: dt.date, end: dt.date) -> list[dt.date]:
    k = _MONTHS_OF[b["cadence"]]
    first = b.get("month", start.month)
    out = []
    for y, m in _months(start, end):
        if (m - first) % k == 0:
            n = _clamp(y, m, b["day"])
            if start <= n <= end:
                out.append(n)
    return out


def _draw(p: Persona, seed: int, start: dt.date, end: dt.date) -> _Plan:
    cal = p.calendar
    schedule: list[Series] = []
    incomes = []
    for i, inc in enumerate(p.incomes):
        rng = rng_for(p.key, seed, f"income{i}")
        s = Series(inc["payer"], "income", inc["cadence"], inc,
                   inc.get("plantable", True), tuple(inc["category"]))
        for day, nominal, amt in _income_days(inc, start, end, cal, rng):
            acct = inc.get("account", "chk")
            incomes.append((day, nominal, inc["payer"], amt, acct,
                            tuple(inc["category"]), s.plantable))
            s.occurrences.append(Occurrence(nominal, day, amt, acct))
        schedule.append(s)
    primary = p.incomes[0]["payer"] if p.incomes else None
    main_paydays = sorted({d for d, _n, payer, *_ in incomes
                           if payer == primary})
    # the bills; one checking bill misses a payment in the history and is
    # paid with the next one plus a late fee
    rng = rng_for(p.key, seed, "bills")
    bills = []
    missable = [i for i, b in enumerate(p.bills)
                if b["cadence"] == "monthly"
                and b.get("account", "chk") in ("chk", "chk2")]
    missed_bill = rng.choice(missable) if missable else None
    missed_month = rng.randint(3, 9)
    for i, b in enumerate(p.bills):
        acct = b.get("account", "chk")
        s = Series(b["payee"], "bill", b["cadence"], b, True,
                   tuple(b["category"]))
        step_month = rng.randint(4, 9)
        nominals = _bill_nominals(b, start, end)
        carry = 0.0
        for k, n in enumerate(nominals):
            years = sum(1 for yy in range(start.year, n.year + 1)
                        if dt.date(yy, step_month, 1) <= n
                        and dt.date(yy, step_month, 1) > start)
            amt = b["amount"] * (1 + b.get("drift", 0.0)) ** years
            if b.get("seasonal"):
                amt *= _SEASON[b["seasonal"]][n.month]
            amt *= max(0.5, 1 + rng.gauss(0, b.get("cv", 0.0)))
            amt = round(amt, 2)
            day = n if acct.startswith("card") else next_bd(n, cal)
            if i == missed_bill and k == missed_month:
                carry = amt
                continue
            if carry:
                amt, carry = round(amt + carry + 25.0, 2), 0.0
            bills.append((day, n, b["payee"], amt, acct,
                          tuple(b["category"])))
            s.occurrences.append(Occurrence(n, day, amt, acct))
        schedule.append(s)
    # intended purchases, day by day
    rng = rng_for(p.key, seed, "spend")
    boost_days = set()
    for d in main_paydays:
        for k in range(4):
            boost_days.add(d + dt.timedelta(days=k))
    span = (end - start).days + 1
    boost_share = len([x for x in boost_days if start <= x <= end]) / span
    cards = p.cards
    weights = [c.get("weight", 1.0) for c in cards]
    intents: dict[dt.date, list] = {}
    per = {"food": (p.food_monthly * 12 / 365, 28.0),
           "other": (p.other_monthly * 12 / 365, 45.0)}
    d = start
    while d <= end:
        factor = p.weekend_factor if d.weekday() >= 5 else 1.0
        if d in boost_days:
            factor *= p.payday_boost
        # normalise so the month's mean stays the persona's
        factor /= (5 + 2 * p.weekend_factor) / 7
        factor /= 1 + (p.payday_boost - 1) * boost_share
        row = []
        for cat, (daily, mean) in per.items():
            lam = daily * factor / mean
            n = _poisson(rng, lam)
            for _ in range(n):
                amt = round(max(1.5, rng.lognormvariate(
                    math.log(mean) - 0.32, 0.8)), 2)
                if cat == "food":
                    merchant = rng.choice(_FOOD)
                    prim, det = ("FOOD_AND_DRINK",
                                 "FOOD_AND_DRINK_GROCERIES")
                else:
                    merchant, prim, det = rng.choice(_OTHER_SHOPS)
                acct = ("chk" if not cards or rng.random() >= p.card_share
                        else rng.choices([c["id"] for c in cards],
                                         weights)[0])
                lag = rng.choice((0, 1, 1, 2, 2, 3))
                disc = rng.random() < (0.4 if cat == "food" else 0.8)
                row.append((cat, merchant, prim, det, amt, acct, lag, disc))
        if row:
            intents[d] = row
        d += DAY
    # lumpy one-offs (Poisson, about `one_off_rate` a month)
    rng = rng_for(p.key, seed, "oneoff")
    one_offs = []
    lo, hi = p.one_off_range
    d = start
    while d <= end:
        if rng.random() < p.one_off_rate * 12 / 365:
            amt = round(min(hi, max(lo, rng.lognormvariate(
                math.log(math.sqrt(lo * hi)), 0.6))), 2)
            trade, prim, det = rng.choice(_ONE_OFF_TRADES)
            merchant = f"{rng.choice(_ONE_OFF_PLACES)} {trade}"
            acct = (rng.choice([c["id"] for c in cards])
                    if cards and rng.random() < 0.6 else "chk")
            one_offs.append((d, merchant, prim, det, amt, acct,
                             rng.choice((1, 2))))
        d += DAY
    # cash withdrawals, Saturdays
    cash = []
    if p.cash_monthly:
        rng = rng_for(p.key, seed, "cash")
        d = start
        while d <= end:
            if d.weekday() == 5:
                cash.append((d, float(round(p.cash_monthly * 12 / 52
                                            * rng.uniform(0.5, 1.5) / 20)
                                      * 20) or 20.0))
            d += DAY
    # one refund: a card purchase over $40 in the first half, credited
    # back two to five weeks later
    rng = rng_for(p.key, seed, "refund")
    refund = None
    half = start + (end - start) / 2
    cands = [(day, i) for day, row in sorted(intents.items()) if day < half
             for i, it in enumerate(row)
             if it[5].startswith("card") and it[4] >= 40]
    if cands:
        day, i = rng.choice(cands)
        refund = (day, i, day + dt.timedelta(days=rng.randint(14, 35)))
    # the hand payer's paydays
    hand_days: dict[str, set] = {}
    for c in cards:
        if c["paying"] != "hand":
            continue
        rng = rng_for(p.key, seed, f"hand-{c['id']}")
        lo_w, hi_w = c.get("hand_weeks", (2, 4))
        days, nxt = set(), start
        for d in main_paydays:
            if d >= nxt:
                days.add(d)
                nxt = d + dt.timedelta(weeks=rng.randint(lo_w, hi_w)) - 3 * DAY
        hand_days[c["id"]] = days
    rng = rng_for(p.key, seed, "react")
    cuts = {}
    d = start
    while d <= end:
        cuts[d] = rng.uniform(*p.react_cut)
        d += DAY
    return _Plan(start, end, cal, incomes, bills, intents, one_offs, cash,
                 refund, hand_days, main_paydays, cuts, schedule)


def _poisson(rng: random.Random, lam: float) -> int:
    k, p, el = 0, 1.0, math.exp(-lam)
    while True:
        p *= rng.random()
        if p <= el:
            return k
        k += 1


# ---- the walk -----------------------------------------------------------------


def _realize(p: Persona, seed: int, plan: _Plan, *,
             react_until: dt.date) -> dict:
    start, end, cal = plan.start, plan.end, plan.cal
    n_days = (end - start).days + 1
    idx = {start + dt.timedelta(days=i): i for i in range(n_days)}
    accts = ["chk"] + (["chk2"] if p.two_checking else []) + ["sav"] + [
        c["id"] for c in p.cards]
    is_card = {a: a.startswith("card") for a in accts}
    # per account, cents by day index: change to available, to posted
    avail_d = {a: [0] * (n_days + 8) for a in accts}
    post_d = {a: [0] * (n_days + 8) for a in accts}
    opening = {"chk": _cents(p.checking_open), "chk2": _cents(p.chk2_open),
               "sav": _cents(p.savings_open)}
    for c in p.cards:
        opening[c["id"]] = _cents(c["open"])
    ledger: list[Txn] = []
    counter = [0]
    tag = f"{p.key[:6]}{seed}"

    def add(acct, date, post, amount, name, prim, det, kind, scheduled,
            series=None, pair=None) -> Txn:
        counter[0] += 1
        tid = f"{tag}-{counter[0]:05d}"
        t = Txn(tid, acct, date, post, round(amount, 2), name, prim, det,
                kind, scheduled,
                pending_id=(f"{tid}-p" if post > date else None),
                series=series, pair=pair)
        ledger.append(t)
        c = _cents(amount)
        # past the world's end: an overflow slot no day reads
        if is_card[acct]:
            # a card's balance is its posted debt; nothing "available"
            post_d[acct][idx.get(post, n_days + 7)] += c
        else:
            post_d[acct][idx.get(post, n_days + 7)] -= c
            when = date if c > 0 else post
            avail_d[acct][idx.get(when, n_days + 7)] -= c
        return t

    def pay(from_acct, card, day, amount, *, debit_day=None, kind_name=""):
        amount = round(amount, 2)
        if amount <= 0:
            return 0.0
        cname = next(c["name"] for c in p.cards if c["id"] == card)
        a = add(card, day, day, -amount, "PAYMENT - THANK YOU",
                "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                "card_payment", True)
        # the card is credited the day it is paid; it counts against the
        # open statement from then
        paid_since[card] += _cents(amount)
        dd = debit_day or day
        b = add(from_acct, dd, dd, amount, f"{cname.upper()} PAYMENT",
                "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                "card_payment", True, series=f"{cname} payment", pair=a.id)
        a.pair = b.id
        return amount

    def transfer(src, dst, day, amount, out_name, in_name, out_det, in_det):
        amount = round(amount, 2)
        if amount <= 0:
            return
        a = add(src, day, day, amount, out_name, "TRANSFER_OUT", out_det,
                "transfer", True)
        b = add(dst, day, day, -amount, in_name, "TRANSFER_IN", in_det,
                "transfer", True, pair=a.id)
        a.pair = b.id

    cards = {c["id"]: c for c in p.cards}
    stmts: dict[str, list[Statement]] = {c: [] for c in cards}
    paid_since: dict[str, int] = {c: 0 for c in cards}  # cents since close
    by_day_inc: dict[dt.date, list] = {}
    for it in plan.incomes:
        by_day_inc.setdefault(it[0], []).append(it)
    by_day_bill: dict[dt.date, list] = {}
    for it in plan.bills:
        by_day_bill.setdefault(it[0], []).append(it)
    by_day_one: dict[dt.date, list] = {}
    for it in plan.one_offs:
        by_day_one.setdefault(it[0], []).append(it)
    cash = dict(plan.cash)
    paydays = set(plan.paydays)
    refund_amount: dict[dt.date, tuple] = {}
    run = {a: [opening.get(a, 0), opening.get(a, 0)] for a in accts}
    # [available, posted] at the end of the previous day
    checking: dict[dt.date, float] = {}
    posted: dict[str, dict] = {a: {} for a in accts}
    available: dict[str, dict] = {a: {} for a in accts}
    react_end: dt.date | None = None
    react_cut = 0.0
    reactions = []
    od_month: dict[tuple, int] = {}
    upcoming_bills = sorted(plan.bills)
    next_income = sorted(d for d, *_ in plan.incomes)

    d = start
    while d <= end:
        i = idx[d]
        # income
        for day, nominal, payer, amt, acct, cat, plantable in \
                by_day_inc.get(d, []):
            add(acct, d, d, -amt, payer.upper(), cat[0], cat[1],
                "paycheck", plantable, series=payer)
        # transfers on the primary payday
        if d in paydays:
            if p.sweep_per_payday:
                transfer("chk", "sav", d, p.sweep_per_payday,
                         "TRANSFER TO SAVINGS", "TRANSFER FROM CHECKING",
                         "TRANSFER_OUT_SAVINGS",
                         "TRANSFER_IN_ACCOUNT_TRANSFER")
            if p.two_checking and p.bills_transfer:
                transfer("chk", "chk2", d, p.bills_transfer,
                         "TRANSFER TO BILLS CHECKING",
                         "TRANSFER FROM MAIN CHECKING",
                         "TRANSFER_OUT_ACCOUNT_TRANSFER",
                         "TRANSFER_IN_ACCOUNT_TRANSFER")
        # month-end sweep: on the 1st, everything above the keep
        if p.month_end_keep is not None and d.day == 1 and d > start:
            spare = run["chk"][0] / 100 - p.month_end_keep
            if spare > 100:
                transfer("chk", "sav", d, math.floor(spare / 50) * 50,
                         "TRANSFER TO SAVINGS", "TRANSFER FROM CHECKING",
                         "TRANSFER_OUT_SAVINGS",
                         "TRANSFER_IN_ACCOUNT_TRANSFER")
        # bills
        for day, nominal, payee, amt, acct, cat in by_day_bill.get(d, []):
            add(acct, d, d, amt, payee.upper(), cat[0], cat[1], "bill", True,
                series=payee)
        # card payments
        for cid, c in cards.items():
            last = stmts[cid][-1] if stmts[cid] else None
            src = c.get("pays_from", "chk")
            owed_debt = run[cid][1] + post_d[cid][i]
            if c["paying"] == "autopay_full":
                if last and d == last.due and last.balance > 0:
                    amt = last.balance - paid_since[cid] / 100
                    if amt > 0.005:
                        pay(src, cid, d, amt, debit_day=next_bd(d, cal))
            elif c["paying"] == "hand":
                if d in plan.hand_days.get(cid, ()) and owed_debt > 100:
                    pay(src, cid, d, owed_debt / 100,
                        debit_day=next_bd(d + DAY, cal))
            elif c["paying"] in ("minimum", "fixed"):
                if last and last.balance > 0 and d == last.due - dt.timedelta(
                        days=c.get("lead", 3)):
                    want = last.minimum
                    if c["paying"] == "fixed":
                        want = max(want, c.get("fixed", 0.0))
                    amt = min(want - paid_since[cid] / 100,
                              owed_debt / 100)
                    if amt > 0.005:
                        pay(src, cid, d, amt, debit_day=next_bd(d, cal))
        # spending
        reacting = (react_end is not None and d <= react_end
                    and d <= react_until)
        for k, (cat, merchant, prim, det, amt, acct, lag, disc) in \
                enumerate(plan.intents.get(d, [])):
            if reacting and disc:
                amt = round(amt * (1 - react_cut), 2)
            t = add(acct, d, d + dt.timedelta(days=lag), amt,
                    merchant.upper(), prim, det, "variable", False)
            if plan.refund and plan.refund[0] == d and plan.refund[1] == k:
                refund_amount[plan.refund[2]] = (acct, amt, merchant, prim,
                                                 det, t.id)
        for day, merchant, prim, det, amt, acct, lag in by_day_one.get(d, []):
            add(acct, d, d + dt.timedelta(days=lag), amt, merchant.upper(),
                prim, det, "one_off", False)
        if d in cash:
            add("chk", d, d, cash[d], "ATM CASH WITHDRAWAL", "TRANSFER_OUT",
                "TRANSFER_OUT_WITHDRAWAL", "cash", False)
        if d in refund_amount:
            acct, amt, merchant, prim, det, _ = refund_amount[d]
            add(acct, d, d, -amt, f"{merchant.upper()} REFUND", prim, det,
                "refund", False)
        # statements close at the end of the day
        for cid, c in cards.items():
            if d.day != min(c["close_day"], calendar.monthrange(
                    d.year, d.month)[1]):
                continue
            debt = run[cid][1] + post_d[cid][i]
            last = stmts[cid][-1] if stmts[cid] else None
            interest = 0.0
            if last is None:
                carried = opening[cid] / 100
                revolving = carried > 0 and c["paying"] in ("minimum",
                                                            "fixed")
            else:
                carried = last.balance
                revolving = (last.balance > 0
                             and paid_since[cid] / 100 < last.balance - 0.005)
            if revolving and debt > 0:
                interest = round(c["apr"] / 12 * debt / 100, 2)
                add(cid, d, d, interest, "INTEREST CHARGE", "BANK_FEES",
                    "BANK_FEES_INTEREST_CHARGE", "interest", False)
                debt = run[cid][1] + post_d[cid][i]
            bal = round(debt / 100, 2)
            minimum = (0.0 if bal <= 0 else
                       min(bal, max(25.0, round(0.01 * bal + interest, 2))))
            stmts[cid].append(Statement(cid, d, bal, minimum,
                                        d + dt.timedelta(days=c["grace"]),
                                        interest, carried))
            paid_since[cid] = 0
        # end of the day
        for a in accts:
            run[a][0] += avail_d[a][i]
            run[a][1] += post_d[a][i]
            posted[a][d] = run[a][1] / 100
            available[a][d] = run[a][0] / 100
        checking[d] = run["chk"][0] / 100
        # an overdraft fee the next day
        if (p.overdraft_fee and run["chk"][0] < 0 and d + DAY <= end
                and od_month.get((d.year, d.month), 0) < 3):
            od_month[(d.year, d.month)] = od_month.get((d.year, d.month),
                                                       0) + 1
            add("chk", d + DAY, d + DAY, p.overdraft_fee, "OVERDRAFT FEE",
                "BANK_FEES", "BANK_FEES_OVERDRAFT_FEES", "fee", False)
        # the household looks at its money
        if d <= react_until and (react_end is None or d > react_end):
            nxt = next((x for x in next_income if x > d), d + 14 * DAY)
            due = sum(amt for day, _n, _p, amt, acct, _c in upcoming_bills
                      if d < day <= nxt and acct == "chk")
            for cid, c in cards.items():
                last = stmts[cid][-1] if stmts[cid] else None
                if (last and c.get("pays_from", "chk") == "chk"
                        and d < last.due <= nxt
                        and c["paying"] == "autopay_full"):
                    due += max(0.0, last.balance - paid_since[cid] / 100)
            proj = run["chk"][0] / 100 - due
            if proj < p.react_buffer:
                react_end = d + 7 * DAY
                react_cut = plan.cuts[d]
                rescued = 0.0
                if p.react_from_savings and d + DAY <= end:
                    room = run["sav"][0] / 100
                    need = math.ceil((p.react_buffer - proj) / 100) * 100
                    rescued = float(min(room, need))
                    if rescued >= 50:
                        transfer("sav", "chk", d + DAY, rescued,
                                 "MOVE TO EVERYDAY ACCOUNT",
                                 "TRANSFER FROM SAVINGS",
                                 "TRANSFER_OUT_ACCOUNT_TRANSFER",
                                 "TRANSFER_IN_ACCOUNT_TRANSFER")
                    else:
                        rescued = 0.0
                reactions.append((d, react_end, react_cut, rescued))
        d += DAY
    return {"ledger": ledger, "statements": stmts, "checking": checking,
            "posted": posted, "available": available,
            "reactions": reactions, "opening": opening}


def _monthly(ledger: list, start: dt.date, end: dt.date, kinds: tuple,
             sign: int) -> float:
    """The mean per whole month of the checking-side amounts of `kinds`."""
    months = [(y, m) for y, m in _months(start, end)
              if dt.date(y, m, 1) >= start
              and _clamp(y, m, 31) <= end]
    if not months:
        return 0.0
    keep = set(months)
    tot = sum(sign * t.amount for t in ledger
              if t.kind in kinds and (t.date.year, t.date.month) in keep)
    return tot / len(months)


def simulate(persona: Persona | str, seed: int, *,
             start: dt.date = DEFAULT_START,
             end: dt.date = DEFAULT_END,
             one_off_scale: float = 1.0) -> World:
    """`one_off_scale` multiplies how often one-offs come (a sensitivity
    knob: does a result hold when the simulator's own assumption moves?)."""
    p = PERSONAS[persona] if isinstance(persona, str) else persona
    if one_off_scale != 1.0:
        import dataclasses
        p = dataclasses.replace(p, one_off_rate=p.one_off_rate
                                * one_off_scale)
    plan = _draw(p, seed, start, end)
    r = _realize(p, seed, plan, react_until=end)
    accounts = [{"id": "chk", "name": "Everyday Checking", "type":
                 "depository", "subtype": "checking", "mask": "ck1"}]
    if p.two_checking:
        accounts.append({"id": "chk2", "name": "Bills Checking",
                         "type": "depository", "subtype": "checking",
                         "mask": "ck2"})
    accounts.append({"id": "sav", "name": "Rainy Day Savings",
                     "type": "depository", "subtype": "savings",
                     "mask": "sv1"})
    for k, c in enumerate(p.cards):
        accounts.append({"id": c["id"], "name": c["name"], "type": "credit",
                         "subtype": "credit card", "mask": f"cc{k + 1}"})
    income = _monthly([t for t in r["ledger"] if t.account in ("chk",
                                                               "chk2")],
                      start, end, ("paycheck",), -1)
    spend = _monthly(r["ledger"], start, end,
                     ("bill", "variable", "one_off", "cash", "fee",
                      "interest"), 1)
    return World(p, seed, start, end, accounts, r["ledger"],
                 r["statements"], r["checking"], r["posted"],
                 r["available"], plan.schedule, r["reactions"],
                 round(income, 2), round(spend, 2), _plan=plan)
