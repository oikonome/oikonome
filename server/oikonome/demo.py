"""demo-instance seeding — ten years of synthetic household
life, generated from rules (never fixtures) so every deployment gets a
fresh but realistic dataset. One archetype — a salaried household —
with income, housing, card mix, merchants, amounts, and balances drawn
per-run from a seeded RNG (the seed is returned/printed; the same seed
reproduces the same household).

Everything goes through the engine's own conventions: positive = money
out, deposits negative, card payments LOAN_PAYMENTS_CREDIT_CARD_PAYMENT,
transfers TRANSFER_*, bills saved via bills.save_bill so cadences
and next-dues are real. All merchants are fictional.

Entry: `oikonome demo-seed` (CLI) — refuses on an instance that already
has users; the oikonome.sh `demo` command wraps install + seed.
"""

from __future__ import annotations

import logging

import datetime as dt
import math
import random
import secrets
import statistics
import string

from . import ext
from .engine.compat import jsonb

# ---- the archetype's fixed shape (amounts randomized per run) ---------------

GROCERS = ["Green Basket Market", "Costless Wholesale", "Corner Grocer"]
DINING = ["Bistro Verde", "Taco Fuego", "Golden Noodle House",
          "Slice Brothers Pizza", "Harvest Table"]
COFFEE = "Daily Grind Coffee"
GAS = ["Petro Stop", "FuelCo Station"]
ONLINE = "Rivermart Online"
FUN = ["CinePlex 12", "Parkview Lanes", "City Aquarium"]
MEDICAL = "Cedar Family Clinic"
TRAVEL = [("Skyway Airlines", 300, 900), ("Grand Vista Hotel", 250, 700)]
PHARMACY = "Lakeside Pharmacy"
HOUSEHOLD = ["Bright Home Goods", "Happy Paws Pet Supply", "TrimTown Salon"]
KIDS = "Little Sprouts Activities"
# deliberately NEVER saved as a bill — the real recurring detector finds
# it in the ledger and proposes it, so approve/reject demos the live flow
CLOUD = "PixelCloud Storage"

# ---- the side business (surfaces: entities, Schedule C, equity) ----
# A salaried household with a real side practice — the archetype that makes
# the business features legible without pretending the demo is a company.
# Every name, the EIN and the agent are FICTIONAL, like the rest of the
# demo: this instance is public.
BIZ_NAME = "Fernwood Studio LLC"
BIZ_EIN = "88-0142596"                    # synthetic, not a real EIN
BIZ_AGENT = "Cascade Registered Agents LLC"
BIZ_STATE = "OR"
BIZ_CLIENTS = ["Harbor Point Media", "Lantern Health Group",
               "Riverbend Outfitters", "Foxglove Ceramics",
               "Northgate Dental", "Blue Spruce Landscaping"]
BIZ_SOFTWARE = [("Palette Pro Creative Suite", 45.00),
                ("Figment Design", 12.00),
                ("Domain & hosting — Grovehost", 20.00),
                ("Sendwise Email", 10.00)]
BIZ_VENDORS = [("Paper Trail Print Shop", 40, 260, "print run for a client"),
               ("Rivermart Online", 25, 180, "office supplies"),
               ("Contract Studio Co", 400, 1400, "subcontracted illustration"),
               ("Ledger & Loft CPA", 250, 650, "bookkeeping"),
               ("Guild Insurance", 42, 42, "liability policy")]
BIZ_MEALS = ["Bistro Verde", "Daily Grind Coffee", "Harvest Table"]

FUNDS = [("EVG", "Evergreen Growth Fund"), ("SPX5", "Summit 500 Index"),
         ("TBND", "Total Bond Market")]
COINS = [("BTC", "Bitcoin"), ("ETH", "Ethereum")]


log = logging.getLogger(__name__)

def _months(start: dt.date, end: dt.date):
    d = start.replace(day=1)
    while d <= end:
        yield d
        d = (dt.date(d.year + 1, 1, 1) if d.month == 12
             else dt.date(d.year, d.month + 1, 1))


def _clamp_day(d: dt.date, day: int) -> dt.date:
    import calendar
    return d.replace(day=min(day, calendar.monthrange(d.year, d.month)[1]))


class _Gen:
    """One seeded household. Collects transactions per account, then
    writes everything in one pass."""

    def __init__(self, rng: random.Random, years: int, today: dt.date):
        self.rng = rng
        self.years = years
        self.today = today
        self.start = today - dt.timedelta(days=int(years * 365.25))
        self.txns: list[dict] = []          # {id fields…}
        self.n = 0

        r = rng
        # a comfortable-but-ordinary household: expected income lands
        # around $6.6-8.1k take-home/mo — high enough that the plan
        # ($1,500 groceries, savings + vacation contributions) fits
        # without a negative plan surplus, low enough to stay ordinary
        self.gross0 = round(r.uniform(110_000, 135_000), -3)
        self.raise_pct = r.uniform(0.02, 0.032)
        self.take_home_ratio = 0.72
        # the demo's job is showing features — always a homeowner, so the
        # property layer (house + mortgage + vehicle) is never empty
        self.mortgage = True
        th = self.take_home_m(today.year)
        self.housing = round(th * r.uniform(0.22, 0.27) / 25) * 25
        # income-proportional draws so the leftover (and with it the
        # monthly brokerage sweep) stays structurally positive — a $0
        # taxable brokerage is a dead Net Worth / Fees surface
        self.food_m = round(th * r.uniform(0.13, 0.18) / 10) * 10
        self.other_m = round(th * r.uniform(0.16, 0.22) / 10) * 10
        self.savings_m = round(th * r.uniform(0.05, 0.08) / 50) * 50
        self.retire_pct = r.uniform(0.06, 0.10)
        self.growth = r.uniform(0.06, 0.09)          # yearly, investments
        self.birth_year = r.randint(1974, 1992)

        # ---- property + loans (net worth's property layer) ---------------
        # mortgage: work the principal back from the P&I payment the
        # household already makes, at a plausible fixed rate
        self.mort_rate = r.uniform(0.0325, 0.0575)
        self.mort_age_m = int(r.uniform(5, 10) * 12)     # months paid so far
        i = self.mort_rate / 12
        # ~78% of the monthly payment is P&I (the rest escrows tax +
        # insurance), so the implied principal doesn't overshoot
        self.mort_principal = round(
            self.housing * 0.78
            * ((1 + i) ** 360 - 1) / (i * (1 + i) ** 360), -3)
        self.mort_balance = round(
            self.mort_principal * ((1 + i) ** 360 - (1 + i) ** self.mort_age_m)
            / ((1 + i) ** 360 - 1), 2)
        # 20% down at purchase, appreciated ~2-3.5%/yr since
        self.home_value = round(self.mort_principal / 0.8
                                * (1 + r.uniform(0.02, 0.035))
                                ** (self.mort_age_m / 12), -3)
        # a financed car bought a few years ago (60-month loan, still open)
        self.car_price = round(r.uniform(24_000, 38_000), -2)
        self.car_bought = today - dt.timedelta(
            days=int(r.uniform(2.0, 4.2) * 365))
        ci = 0.065 / 12
        financed = self.car_price * 0.8
        self.car_payment = round(
            financed * ci * (1 + ci) ** 60 / ((1 + ci) ** 60 - 1), 2)
        paid = min(60, (today.year - self.car_bought.year) * 12
                   + today.month - self.car_bought.month)
        self.car_loan_balance = round(
            financed * ((1 + ci) ** 60 - (1 + ci) ** paid)
            / ((1 + ci) ** 60 - 1), 2)
        self.car_value = round(self.car_price
                               * 0.85 ** ((today - self.car_bought).days
                                          / 365.25), -2)

        # ---- market path for investment prices ---------------------------
        # one shared monthly random walk (all funds ride it, crypto rides
        # an amplified one) so the reconstructed net-worth trend shows
        # correlated growth AND real drawdowns instead of a flat back-cast
        self._mkt: dict[str, float] = {}
        self._cmkt: dict[str, float] = {}
        lvl, clvl = 1.0, 1.0
        months = list(_months(self.start - dt.timedelta(days=45), today))
        crash = months[int(len(months) * r.uniform(0.45, 0.7))]
        for m in months:
            # mean-reverting in log space: the wobble (and the crash dip)
            # stays bounded, so the per-symbol long-run growth dominates
            # and the decade trend RISES through its drawdowns
            lvl = max(0.35, (lvl * (1 + r.gauss(0.0, 0.028))) ** 0.97)
            clvl = max(0.08, (clvl * (1 + r.gauss(0.0, 0.11))) ** 0.95)
            if m == crash:                       # one visible bear market
                lvl *= 0.80
                clvl *= 0.55
            self._mkt[m.strftime("%Y-%m")] = lvl
            self._cmkt[m.strftime("%Y-%m")] = clvl
        self._mkt_today = lvl
        self._cmkt_today = clvl
        # per-symbol final prices + long-run growth
        self.px_final = {"EVG": round(r.uniform(80, 200), 2),
                         "SPX5": round(r.uniform(250, 480), 2),
                         "TBND": round(r.uniform(40, 90), 2),
                         "BTC": round(r.uniform(45_000, 95_000), 2),
                         "ETH": round(r.uniform(1_800, 4_800), 2)}
        self.px_growth = {"EVG": r.uniform(0.08, 0.12),
                          "SPX5": r.uniform(0.07, 0.10),
                          "TBND": r.uniform(0.01, 0.03),
                          "BTC": r.uniform(0.25, 0.55),
                          "ETH": r.uniform(0.20, 0.50)}
        # accumulated shares per (account, symbol) — becomes today's holdings
        self.hold: dict[tuple[str, str], float] = {}

    def price(self, sym: str, d: dt.date) -> float:
        """Fictional but coherent price history: long-run growth discounted
        back from today's price, times the shared market walk."""
        yrs = (self.today - d).days / 365.25
        base = self.px_final[sym] / (1 + self.px_growth[sym]) ** yrs
        walk = self._cmkt if sym in ("BTC", "ETH") else self._mkt
        walk_today = (self._cmkt_today if sym in ("BTC", "ETH")
                      else self._mkt_today)
        f = walk.get(d.strftime("%Y-%m"), walk_today) / walk_today
        return max(0.01, round(base * f, 2))

    def buy_security(self, aid: str, d: dt.date, dollars: float, sym: str,
                     name: str, primary: str = "TRANSFER_IN") -> None:
        """An investment-account transaction that carries share-level raw
        data — reporting._reconstruct_trend walks these to rebuild the
        net-worth line, so every buy must record (symbol, shares, price)."""
        if d > self.today or d < self.start:
            return                # mirror add(): dropped txn, no shares
        p = self.price(sym, d)
        qty = round(dollars / p, 4)
        self.hold[(aid, sym)] = self.hold.get((aid, sym), 0.0) + qty
        self.add(aid, d, -round(dollars, 2), name, primary=primary,
                 raw={"shares": qty, "symbol": sym, "price": p})

    # income for a calendar year (raises compound from the first year)
    def gross(self, year: int) -> float:
        y0 = self.start.year
        return self.gross0 * (1 + self.raise_pct) ** max(0, year - y0)

    def take_home_m(self, year: int) -> float:
        return self.gross(year) * self.take_home_ratio / 12

    def add(self, account: str, date: dt.date, amount: float, name: str,
            merchant: str | None = None, primary: str | None = None,
            detailed: str | None = None, raw: dict | None = None) -> None:
        if date > self.today or date < self.start:
            return
        self.n += 1
        self.txns.append(dict(
            id=f"demo:{self.n:06d}", account_id=account, date=date,
            amount=round(amount, 2), name=name, merchant=merchant,
            primary=primary, detailed=detailed, raw=raw))

    # ---- generators ---------------------------------------------------------

    def payroll(self):
        """Biweekly paychecks + per-paycheck 401(k) contribution+match."""
        r = self.rng
        # anchor paydays on a Friday near the start
        d = self.start + dt.timedelta(days=(4 - self.start.weekday()) % 7)
        while d <= self.today:
            net = self.gross(d.year) * self.take_home_ratio / 26
            self.add("demo-chk", d, -round(net + r.uniform(-3, 3), 2),
                     "ACME MANUFACTURING PAYROLL",
                     merchant="Acme Manufacturing", primary="INCOME")
            contrib = self.gross(d.year) * self.retire_pct / 26
            # each paycheck's contribution+match buys a fund (rotating) so
            # the 401(k) has a real share history for the net-worth trend
            sym, _ = FUNDS[(d.toordinal() // 14) % len(FUNDS)]
            self.buy_security("demo-401k", d, round(contrib * 1.5, 2),
                              sym, "Contribution + employer match")
            d += dt.timedelta(days=14)

    def housing_and_bills(self):
        r = self.rng
        payee = ("Maple Street Mortgage Co" if self.mortgage
                 else "Hillcrest Property Mgmt")
        bills = [
            (payee, 1, self.housing, 0,
             "LOAN_PAYMENTS" if self.mortgage else "RENT_AND_UTILITIES"),
            ("City Power & Light", 12, 120, 55, "RENT_AND_UTILITIES"),
            ("Clearwater Utility District", 18, 65, 12, "RENT_AND_UTILITIES"),
            ("FiberLink Internet", 20, 70, 0, "RENT_AND_UTILITIES"),
            ("CellOne Mobile", 22, 95, 5, "RENT_AND_UTILITIES"),
            ("SafeGuard Life", 15, 46, 0, "INSURANCE"),
            ("TrashCo Sanitation", 6, 28, 3, "RENT_AND_UTILITIES"),
            ("PetShield Pet Insurance", 17, 32, 0, "INSURANCE"),
        ]
        for m in _months(self.start, self.today):
            # the car payment runs from purchase until the 60-month loan ends
            car_end = _clamp_day(dt.date(self.car_bought.year + 5,
                                         self.car_bought.month, 1),
                                 self.car_bought.day)
            if self.car_bought <= _clamp_day(m, self.car_bought.day) < car_end:
                self.add("demo-chk",
                         _clamp_day(m, min(self.car_bought.day, 28)),
                         self.car_payment, "GUARDIAN AUTO FINANCE",
                         merchant="Guardian Auto Finance",
                         primary="LOAN_PAYMENTS")
            for name, day, base, jitter, cat in bills:
                amt = base + (jitter and r.uniform(-jitter, jitter))
                if name == "City Power & Light":   # seasonal (AC + heating)
                    amt = base + 60 * abs(math.cos((m.month - 1)
                                                   * math.pi / 6)) \
                          + r.uniform(-10, 10)
                self.add("demo-chk", _clamp_day(m, day), amt,
                         name.upper(), merchant=name, primary=cat)
            # savings + auto-insurance every 6 months
            self.add("demo-chk", _clamp_day(m, 3), self.savings_m,
                     "TRANSFER TO SAVINGS", primary="TRANSFER_OUT")
            self.add("demo-sav", _clamp_day(m, 3), -self.savings_m,
                     "TRANSFER FROM CHECKING", primary="TRANSFER_IN")
            # a labeled vacation carve-out, ~16 recent months of $200 —
            # the "Vacation" goal's token matching picks these up, so the
            # goal demos mid-flight (~60-65% of its $5k target)
            d4 = _clamp_day(m, 4)
            if 0 <= (self.today - d4).days < 16 * 30:
                self.add("demo-chk", d4, 200.0, "VACATION FUND TRANSFER",
                         primary="TRANSFER_OUT")
                self.add("demo-sav", d4, -200.0, "VACATION FUND TRANSFER",
                         primary="TRANSFER_IN")
            if m.month in (3, 9):
                self.add("demo-chk", _clamp_day(m, 12),
                         620 + r.uniform(-40, 40), "AUTOSHIELD INSURANCE",
                         merchant="AutoShield Insurance",
                         primary="INSURANCE")

    def card_spending(self):
        """Variable spend on two cards; statements paid from checking the
        following month. Returns per-card unpaid current-cycle balances."""
        r = self.rng
        subs = [("StreamFest", 15.99, 8, "ENTERTAINMENT"),
                ("TuneBox Music", 10.99, 12, "ENTERTAINMENT"),
                ("IronWorks Gym", 39.00, 2, "PERSONAL_CARE"),
                ("FlixBox Video", 18.99, 11, "ENTERTAINMENT"),
                ("CloudSafe Backup", 5.99, 25, "GENERAL_SERVICES"),
                ("NewsWire Daily", 12.99, 3, "ENTERTAINMENT")]
        cycle: dict[str, float] = {"demo-visa": 0.0, "demo-amex": 0.0}
        last_stmt: dict[str, float] = dict(cycle)
        for m in _months(self.start, self.today):
            month_spend: dict[str, float] = {"demo-visa": 0.0,
                                             "demo-amex": 0.0}

            def buy(day, amount, name, merchant, cat, card=None):
                card = card or ("demo-visa" if r.random() < 0.7
                                else "demo-amex")
                d = _clamp_day(m, day)
                if d > self.today:
                    return
                self.add(card, d, amount, name.upper(), merchant=merchant,
                         primary=cat)
                month_spend[card] += amount

            for name, price, day, cat in subs:
                buy(day, price, name, name, cat, card="demo-visa")
            # an unbilled subscription that started ~8 months ago — bait
            # for the real recurring detector (see _configure)
            if (self.today - m).days < 8 * 30:
                # ≥$5 — the detector's occurrence floor ignores tinier sums
                buy(17, 6.99, CLOUD, CLOUD, "ENTERTAINMENT",
                    card="demo-visa")
            # volume tuned to a real two-card household: ~90–120 txns/mo
            # across all accounts — much thinner reads unlived-in.
            # grocery volume tuned so the post-carve-out Food average lives
            # near its $1,500 budget line rather than well under it
            for _ in range(r.randint(10, 14)):      # groceries + top-ups
                g = r.choice(GROCERS)
                buy(r.randint(1, 28), r.uniform(40, 200), g, g,
                    "FOOD_AND_DRINK")
            for _ in range(r.randint(12, 20)):      # the coffee habit
                buy(r.randint(1, 28), r.uniform(4.5, 9.5), COFFEE,
                    COFFEE, "FOOD_AND_DRINK")
            for _ in range(r.randint(7, 12)):       # dining out
                dnm = r.choice(DINING)
                buy(r.randint(1, 28), r.uniform(18, 85), dnm, dnm,
                    "FOOD_AND_DRINK")
            for _ in range(r.randint(4, 7)):        # gas
                g = r.choice(GAS)
                buy(r.randint(1, 28), r.uniform(35, 70), g, g,
                    "TRANSPORTATION")
            for _ in range(r.randint(5, 10)):       # online shopping
                buy(r.randint(1, 28), r.uniform(12, 160), ONLINE, ONLINE,
                    "GENERAL_MERCHANDISE")
            for _ in range(r.randint(2, 5)):        # pharmacy + household
                nm = PHARMACY if r.random() < 0.4 else r.choice(HOUSEHOLD)
                buy(r.randint(1, 28), r.uniform(9, 85), nm, nm,
                    "GENERAL_MERCHANDISE")
            for _ in range(r.randint(1, 3)):        # something fun
                f = r.choice(FUN)
                buy(r.randint(1, 28), r.uniform(20, 90), f, f,
                    "ENTERTAINMENT")
            if r.random() < 0.5:                    # kids' activities
                buy(r.randint(1, 28), r.uniform(20, 120), KIDS, KIDS,
                    "GENERAL_SERVICES")
            if r.random() < 0.35:                   # occasional medical
                buy(r.randint(1, 28), r.uniform(25, 240), MEDICAL, MEDICAL,
                    "MEDICAL")
            if m.month in (r.randint(5, 7), 12):    # travel-ish months
                nm, lo, hi = r.choice(TRAVEL)
                buy(r.randint(1, 20), r.uniform(lo, hi), nm, nm, "TRAVEL")

            # pay LAST month's statements from checking on the 5th
            pay_day = _clamp_day(m, 5)
            stmt_closed = dict(last_stmt)     # the most recent CLOSED cycle
            owed = dict(last_stmt)            # …and how much of it is unpaid
            for card, stmt in last_stmt.items():
                if stmt > 0.5 and pay_day <= self.today:
                    label = ("SUMMIT SAPPHIRE EPAY" if card == "demo-visa"
                             else "NORTHLINE CARD PAYMENT")
                    self.add("demo-chk", pay_day, round(stmt, 2), label,
                             primary="LOAN_PAYMENTS",
                             detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
                    self.add(card, pay_day, -round(stmt, 2),
                             "PAYMENT RECEIVED — THANK YOU",
                             primary="LOAN_PAYMENTS",
                             detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
                    owed[card] = 0.0
            last_stmt = dict(month_spend)
            for c in cycle:
                cycle[c] = month_spend[c]
        self.card_open = cycle                       # current open cycle
        # `last_stmt` after the loop is the CURRENT (partial) month, which is
        # not a statement at all — using it as one made the card balance wrong
        # every day of the month, but visibly so only on the 1st: mid-month
        # balance = open + last_stmt double-counted the open cycle, and on the
        # 1st both halves were the barely-started current month, so the demo
        # showed $0 card debt and the whole liabilities story went blank one
        # day in thirty. Two different numbers were being conflated:
        self.card_owed = owed              # unpaid → what the card BALANCE is
        self.card_stmt = stmt_closed       # last closed → liabilities.raw

        # a fresh clinic charge the household expects back — _configure
        # flags it so the awaiting-reimbursement alert + Reimburse page demo
        self.add("demo-visa",
                 self.today - dt.timedelta(days=r.randint(7, 16)),
                 round(r.uniform(180, 420), 2), MEDICAL.upper(),
                 merchant=MEDICAL, primary="MEDICAL")

        # budgets ≈ the household's own behavior: derive food/other/coffee
        # (and the other carve-out buckets below) from the generated
        # last-12-months averages so the verdict reads lived-in (usually on
        # plan, sometimes over) instead of arbitrary
        cutoff = self.today - dt.timedelta(days=365)
        billed = {s[0].upper() for s in subs} | {CLOUD.upper()}
        dining_names = {d.upper() for d in DINING}
        gas_names = {g.upper() for g in GAS}
        pets_name = "Happy Paws Pet Supply".upper()
        food = other = coffee = 0.0
        dining = gas = online = pets = 0.0
        for t in self.txns:
            if (t["account_id"] not in ("demo-visa", "demo-amex")
                    or t["date"] < cutoff or t["amount"] < 0
                    or t["name"] in billed):
                continue
            if t["name"] == COFFEE.upper():
                coffee += t["amount"]
            elif t["name"] in dining_names:
                dining += t["amount"]
            elif t["name"] in gas_names:
                gas += t["amount"]
            elif t["name"] == ONLINE.upper():
                online += t["amount"]
            elif t["name"] == pets_name:
                pets += t["amount"]
            if t["primary"] == "FOOD_AND_DRINK":
                food += t["amount"]
            else:
                other += t["amount"]
        self.other_m = round(other / 12 * r.uniform(0.95, 1.1) / 10) * 10
        self.coffee_m = round(coffee / 12 * r.uniform(1.0, 1.15) / 5) * 5
        # carve-out siblings — one lonely Coffee bucket undersells the
        # budget page. Each budget sits just above its own average so the
        # bars usually read on-plan, occasionally over
        self.dining_m = round(dining / 12 * r.uniform(0.95, 1.1) / 10) * 10
        self.gas_m = round(gas / 12 * r.uniform(0.95, 1.1) / 10) * 10
        self.online_m = round(online / 12 * r.uniform(0.95, 1.1) / 10) * 10
        self.pets_m = round(pets / 12 * r.uniform(1.0, 1.2) / 5) * 5
        # the Food line (post-carve-outs) shows a round, human $1,500;
        # food_monthly carries the carve-outs on top, and the grocery
        # volume above keeps actuals living near the line
        self.food_m = 1500 + self.coffee_m + self.dining_m

    def business(self):
        """The side practice's own checking account: client revenue, the
        expenses a Schedule C actually has, and owner draws. Runs over the
        last ~3 years (the practice is younger than the household) so the
        P&L, Schedule C buckets and equity views all have multi-year
        history without pretending it has always existed."""
        r = self.rng
        start = max(self.start, self.today - dt.timedelta(days=int(3.2 * 365)))
        self.biz_start = start
        # opening capital contribution — the owner funds the account
        self.add("demo-biz", start, -round(r.uniform(2000, 3500), 2),
                 "OWNER CONTRIBUTION", merchant="Owner transfer",
                 primary="TRANSFER_IN")
        d = start.replace(day=1)
        while d <= self.today:
            month_in = month_out = 0.0
            # 1-3 client invoices a month. Sized as a genuine SIDE practice
            # (~$25-45k/yr) next to the household's salary — at 2-4 invoices
            # of $900-4200 it billed $90-110k/yr, which reads as someone's
            # full-time job and made the household's money story incoherent.
            for _ in range(r.randint(1, 3)):
                client = r.choice(BIZ_CLIENTS)
                day = _clamp_day(d, r.randint(3, 27))
                amt = round(r.uniform(600, 2600), 2)
                month_in += amt
                self.add("demo-biz", day, -amt,
                         f"{client.upper()} — INVOICE PAYMENT",
                         merchant=client, primary="INCOME",
                         detailed="INCOME_OTHER_INCOME")
            # recurring software (the real recurring detector picks these up)
            for name, amt in BIZ_SOFTWARE:
                month_out += amt
                self.add("demo-biz", _clamp_day(d, 6), amt, name.upper(),
                         merchant=name, primary="GENERAL_SERVICES",
                         detailed="GENERAL_SERVICES_OTHER_GENERAL_SERVICES")
            # a couple of vendor/expense hits
            for _ in range(r.randint(1, 3)):
                vendor, lo, hi, _why = r.choice(BIZ_VENDORS)
                amt = round(r.uniform(lo, hi), 2)
                month_out += amt
                self.add("demo-biz", _clamp_day(d, r.randint(2, 26)),
                         amt, vendor.upper(),
                         merchant=vendor, primary="GENERAL_MERCHANDISE")
            # client meals (Schedule C meals bucket — 50% deductible)
            if r.random() < 0.7:
                m = r.choice(BIZ_MEALS)
                amt = round(r.uniform(18, 95), 2)
                month_out += amt
                self.add("demo-biz", _clamp_day(d, r.randint(4, 25)),
                         amt, m.upper(), merchant=m,
                         primary="FOOD_AND_DRINK",
                         detailed="FOOD_AND_DRINK_RESTAURANT")
            # quarterly estimated tax + a monthly owner draw to personal
            if d.month in (1, 4, 6, 9):
                amt = round(r.uniform(700, 1900), 2)
                month_out += amt
                self.add("demo-biz", _clamp_day(d, 15), amt,
                         "IRS USATAXPYMT — ESTIMATED TAX",
                         merchant="US Treasury", primary="GENERAL_SERVICES")
            # The draw follows the month's NET, the way a single-member LLC
            # actually runs: profit is swept to personal and the business
            # account keeps a working float. A fixed draw let the balance
            # accumulate to six figures, which is not what a side practice
            # looks like (and it distorted household net worth).
            draw = round(max(0.0, (month_in - month_out)
                             * r.uniform(0.70, 0.90)), 2)
            if draw < 50:
                d = (dt.date(d.year + 1, 1, 1) if d.month == 12
                     else dt.date(d.year, d.month + 1, 1))
                continue
            self.add("demo-biz", _clamp_day(d, 28), draw, "OWNER DRAW",
                     merchant="Owner transfer", primary="TRANSFER_OUT")
            self.add("demo-chk", _clamp_day(d, 28), -draw,
                     "TRANSFER FROM FERNWOOD STUDIO",
                     merchant="Owner transfer", primary="TRANSFER_IN")
            d = (dt.date(d.year + 1, 1, 1) if d.month == 12
                 else dt.date(d.year, d.month + 1, 1))

    def fees(self):
        """BANK_FEES rows. Two go to the Net Worth investment-fees section
        and are deliberately different SHAPES, so the platform comparison is
        a real one: the brokerage's advisory fee is basis points on a
        growing balance, the 401(k)'s is a flat per-participant
        recordkeeping charge that does not scale at all. The annual card
        membership and stray ATM fees are ordinary spending, not investment
        drag, and must not appear in that section.
        """
        r = self.rng
        quarters = [m for m in _months(self.start, self.today)
                    if m.month in (1, 4, 7, 10)]
        # payroll() buys into demo-401k every fortnight from self.start, so
        # the plan always holds something by the first quarter-end — no
        # balance guard needed here (unlike the brokerage, which can be $0)
        for m in quarters:
            self.add("demo-401k", _clamp_day(m, 15), 9.75,
                     "PARTICIPANT RECORDKEEPING FEE",
                     merchant="Meridian Retirement Services",
                     primary="BANK_FEES",
                     detailed="BANK_FEES_OTHER_BANK_FEES")
        for i, m in enumerate(quarters, 1):
            if self.brokerage_in <= 0:
                break
            # ≈0.25%/yr on a roughly linearly-accumulating balance
            fee = round(0.000625 * self.brokerage_in * i / len(quarters), 2)
            if fee >= 1:
                self.add("demo-brok", _clamp_day(m, 4), fee,
                         "EVERGREEN ADVISORY FEE",
                         merchant="Evergreen Brokerage",
                         primary="BANK_FEES",
                         detailed="BANK_FEES_OTHER_BANK_FEES")
        for y in range(self.start.year + 1, self.today.year + 1):
            self.add("demo-visa", dt.date(y, 2, 9), 95.0,
                     "ANNUAL MEMBERSHIP FEE", merchant="Summit Card Co",
                     primary="BANK_FEES",
                     detailed="BANK_FEES_OTHER_BANK_FEES")
        for _ in range(r.randint(4, 9)):
            d = self.start + dt.timedelta(
                days=r.randint(0, (self.today - self.start).days))
            self.add("demo-chk", d, round(r.uniform(3.0, 4.5), 2),
                     "NON-NETWORK ATM FEE", merchant="Northwind Bank",
                     primary="BANK_FEES", detailed="BANK_FEES_ATM_FEES")

    def investing(self):
        """Monthly sweep of the leftover to brokerage (bought into funds so
        the trend has share history), an annual Roth contribution, and
        sporadic crypto buys."""
        r = self.rng
        self.brokerage_in = 0.0
        # a standing dollar-cost-average habit plus whatever the month left
        # over — the habit floor keeps the taxable brokerage alive on every
        # seed (a $0 brokerage is a dead Net Worth / Fees surface)
        base = round(self.take_home_m(self.today.year)
                     * r.uniform(0.025, 0.05) / 25) * 25
        for mi, m in enumerate(_months(self.start, self.today)):
            leftover = (self.take_home_m(m.year) - self.housing
                        - self.savings_m - self.food_m - self.other_m
                        - self.car_payment - 520)
            sweep = base + round(max(0.0, leftover * 0.6) / 25) * 25
            if sweep >= 50:
                d = _clamp_day(m, 26)
                self.add("demo-chk", d, sweep, "EVERGREEN BROKERAGE TRANSFER",
                         merchant="Evergreen Brokerage",
                         primary="TRANSFER_OUT")
                sym, _ = FUNDS[mi % 2]           # EVG / SPX5 alternate
                self.buy_security("demo-brok", d, sweep, sym, "ACH DEPOSIT")
                self.brokerage_in += sweep
            # Roth IRA: one contribution every January
            if m.month == 1:
                amt = round(r.uniform(3_000, 6_500), -2)
                d = _clamp_day(m, r.randint(5, 25))
                self.add("demo-chk", d, amt, "ROTH IRA CONTRIBUTION",
                         merchant="Evergreen Brokerage",
                         primary="TRANSFER_OUT")
                sym, _ = FUNDS[1 + (m.year % 2)]  # SPX5 / TBND alternate
                self.buy_security("demo-roth", d, amt, sym,
                                  "CONTRIBUTION — SETTLED")
        self.crypto_in = 0.0
        for _ in range(r.randint(6, 14)):
            d = self.start + dt.timedelta(
                days=r.randint(0, (self.today - self.start).days))
            amt = round(r.uniform(150, 1500), 2)
            self.add("demo-chk", d, amt, "CRYPTOX PURCHASE",
                     merchant="CryptoX", primary="TRANSFER_OUT")
            coin, _ = COINS[0] if r.random() < 0.65 else COINS[1]
            self.buy_security("demo-cry", d, amt, coin, f"BUY {coin}")
            self.crypto_in += amt


def _generate(g: _Gen) -> _Gen:
    """The generator sequence, in order — the ONE place it is written down.
    seed() and the test fixture both call this: the fixture used to repeat
    the list by hand and silently missed `business()` when it was added,
    so the demo tests passed against a household with no business in it."""
    g.payroll()
    g.housing_and_bills()
    g.card_spending()
    g.investing()
    g.fees()
    g.business()
    return g


# Seed and reset are single-flight across the whole instance: the hourly
# cron, a slow seed and an operator running the CLI can overlap, and two
# seeds that both saw an empty instance would each create a tenant — after
# which every later reset refuses ("needs exactly one tenant") and the
# public demo stays vandalized. A session-level advisory lock on the admin
# connection that does the work serializes them; it is released when that
# connection closes, so a crash mid-seed cannot wedge the next run.
_SEED_LOCK = "oikonome:demo-seed"


def _take_seed_lock(admin) -> None:
    admin.execute("SELECT pg_advisory_lock(hashtext(%s))", (_SEED_LOCK,))


def seed(seed: int | None = None, email: str = "demo@example.com",
         password: str | None = None, years: int = 10,
         viewer: str | None = None) -> dict:
    """Create the demo tenant + owner and generate the household.
    Refuses when the instance already has a tenant or a user — unless
    `viewer` names a second login: then this is a demonstration
    household on an instance that may hold other tenants (synthetic data,
    demo_mode OFF so the native app can sign in, a view-only second login,
    and whatever rows an installed add-on wants such a household to
    carry)."""
    from .db import tenancy

    admin = tenancy.admin_connect()
    try:
        _take_seed_lock(admin)
        return _seed_locked(admin, seed=seed, email=email,
                            password=password, years=years,
                            viewer=viewer)
    finally:
        admin.close()


def _seed_locked(admin, *, seed: int | None, email: str,
                 password: str | None, years: int,
                 viewer: str | None = None) -> dict:
    """The seed body; `admin` holds the seed lock for its whole duration."""
    from .auth import passwords
    from .db import tenancy
    from .engine import bills, budget

    seed = seed if seed is not None else secrets.randbelow(1_000_000)
    rng = random.Random(seed)
    # the credential is NOT part of the reproducible household — same-seed
    # re-runs get identical data but a fresh password; it is printed and
    # pre-filled on the login page either way
    password = password or "".join(
        secrets.choice(string.ascii_letters + string.digits)
        for _ in range(14))
    today = dt.date.today()

    # A tenant with no user yet is still a tenant — a half-finished seed,
    # or a signup that has not verified. Refusing on tenants, not just
    # users, is what keeps a second seed from ever making it two.
    if not viewer:
        if admin.execute("SELECT 1 FROM tenants LIMIT 1").fetchone():
            raise SystemExit("this instance already has a tenant — demo-seed "
                             "only runs on a fresh install")
        if admin.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise SystemExit("this instance already has users — demo-seed "
                             "only runs on a fresh install")
    elif admin.execute("SELECT 1 FROM users WHERE email IN (%s, %s)",
                       (email, viewer)).fetchone():
        raise SystemExit("a user with that email already exists")
    tid = tenancy.create_tenant(
        admin, DEMO_HOUSEHOLD_NAME if viewer else "Demo Household")
    admin.execute(
        "INSERT INTO users (tenant_id, email, password_hash, "
        "verified_at) VALUES (%s,%s,%s,now())",
        (tid, email, passwords.hash_password(password)))
    viewer_password = None
    if viewer:
        viewer_password = "".join(
            secrets.choice(string.ascii_letters + string.digits)
            for _ in range(14))
        # this login is excused from the second-factor rule — it is a shared
        # demonstration login with no authenticator behind it; the
        # household it opens is synthetic
        admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role, "
            "verified_at, second_factor_waived) "
            "VALUES (%s,%s,%s,'viewer',now(),TRUE)",
            (tid, viewer, passwords.hash_password(viewer_password)))
        ext.gate.on_demo_household(admin, tid)

    g = _generate(_Gen(rng, years, today))

    conn = tenancy.tenant_connect(tid)
    try:
        _write(conn, g, rng, today)
        _configure(conn, g, rng, today, budget, bills,
                   login=None if viewer else (email, password),
                   rng_seed=seed, demo_mode=not viewer)
    finally:
        conn.close()
    out = {"ok": True, "seed": seed, "email": email, "password": password,
           "tenant_id": tid, "transactions": len(g.txns), "years": years}
    if viewer:
        out.update({"viewer_email": viewer,
                    "viewer_password": viewer_password})
    return out


def reset(seed_value: int | None = None, years: int = 10) -> dict:
    """Wipe the demo household and regenerate it in place — the hourly
    pristine reset. Re-seed, not restore: the generator is deterministic per
    seed and date-relative, so the same seed replays an identical,
    always-current household. Login credentials survive (same
    email/password, pre-filled on the login page); sessions are dropped by
    design — a reset IS a fresh start. Refuses on anything that isn't a
    single-tenant demo instance.
    """
    from .db import tenancy
    from .engine import budget

    # one admin connection holds the seed lock from the tenant check
    # through delete and re-seed — the check is only meaningful if nothing
    # else can create a tenant between it and the seed
    admin = tenancy.admin_connect()
    try:
        _take_seed_lock(admin)
        rows = admin.execute("SELECT id FROM tenants").fetchall()
        if len(rows) != 1:
            raise SystemExit("demo-reset needs exactly one tenant "
                             f"(found {len(rows)})")
        tid = str(rows[0]["id"])
        conn = tenancy.tenant_connect(tid)
        try:
            cfg = budget.load_config(conn)
        finally:
            conn.close()
        if not cfg.get("demo_mode"):
            raise SystemExit("this instance is not a demo — refusing to "
                             "reset")
        login = cfg.get("demo_login") or {}
        email = login.get("email") or "demo@example.com"
        password = login.get("password")   # None ⇒ seed mints a fresh one
        if seed_value is None:
            seed_value = cfg.get("demo_seed")
        years = cfg.get("demo_years") or years

        # anything the demo tenant holds at an external service goes
        # first — otherwise the wipe orphans it (a Plaid Item, say) with no
        # row left to find it by. Best-effort and usually a no-op: a demo
        # box normally has no such keys.
        try:
            from .erasure import release_external
            release_external(tid)
        except Exception:                                # noqa: BLE001
            log.warning("demo reset: external release failed", exc_info=True)
        tenancy.delete_tenant_rows(admin, tid)
        r = _seed_locked(admin, seed=seed_value, email=email,
                         password=password, years=years)
    finally:
        admin.close()
    r["reset"] = True
    return r


# The name the demonstration seed gives the tenant. refresh_demo_household
# treats the name as one of the marks of such a household, so the seed and
# the refresh must agree on it.
DEMO_HOUSEHOLD_NAME = "Demonstration Household"
DEMO_HOUSEHOLD_NAMES = (DEMO_HOUSEHOLD_NAME,)


def refresh_demo_household(viewer_email: str, *, years: int = 10,
                     seed: int | None = None) -> dict:
    """Regenerate the demonstration household in place, as of today,
    keeping both logins exactly as they are.

    The generator is date-relative, so a household seeded in one month stops
    dead at that month: opened weeks later, the current month holds no
    transactions, every budget bar reads zero and the transactions screen
    says "No transactions". Re-seeding from scratch fixes the dates but
    mints a new password and drops any enrolled second factor, both of
    which the logins' holders already rely on. So this wipes the DATA and leaves the tenant, its
    users, their credentials and their enrolments untouched.

    It refuses on anything that is not such a household, on three counts
    that no ordinary household can satisfy together: the login's second
    factor is WAIVED (nothing but this seed sets that — a real account is
    held to the rule), the tenant carries a demonstration name, and every
    aggregator item it holds is one of the generator's synthetic ones. The
    role is deliberately NOT part of the test: the household may hold one
    owner login rather than the owner + viewer pair the seed makes locally,
    and a guard that disagrees with the thing it guards is a guard nobody
    can use.
    """
    from .db import tenancy
    from .engine import bills, budget
    from psycopg import sql

    admin = tenancy.admin_connect()
    try:
        _take_seed_lock(admin)
        row = admin.execute(
            "SELECT u.tenant_id, u.second_factor_waived, t.name "
            "FROM users u JOIN tenants t ON t.id = u.tenant_id "
            "WHERE lower(u.email) = lower(%s)", (viewer_email,)).fetchone()
        if not row:
            raise SystemExit(f"no user {viewer_email}")
        if not row["second_factor_waived"]:
            raise SystemExit(f"{viewer_email} is held to the second-factor "
                             "rule, so it is not a demonstration login — "
                             "refusing")
        if row["name"] not in DEMO_HOUSEHOLD_NAMES:
            raise SystemExit(f"tenant is named {row['name']!r}, not "
                             f"{DEMO_HOUSEHOLD_NAME!r} — refusing")
        tid = str(row["tenant_id"])

        # Every item must be one of the generator's. This is the guard that
        # makes the command safe to point at a live multi-tenant host: a
        # household with a real Plaid item cannot pass it.
        real = [r["id"] for r in admin.execute(
            "SELECT id FROM items WHERE tenant_id = %s", (tid,)).fetchall()
            if not str(r["id"]).startswith("demo-")]
        if real:
            raise SystemExit(f"tenant {tid} has non-synthetic items "
                             f"({len(real)}) — refusing")

        seed = seed if seed is not None else secrets.randbelow(1_000_000)
        rng = random.Random(seed)
        today = dt.date.today()

        # Discovery, like delete_tenant_rows — but that function is a FULL
        # teardown and its exclusion set is sized for one. Reusing it here
        # wipes two things this command exists to preserve: the device
        # tokens the native app signs in with (so a session in progress is
        # thrown back to a login screen by a scheduled refresh) and the rows
        # an installed add-on names in ``demo_keep_tables``, whose absence
        # reads as a household the add-on has never seen.
        #
        # So the set is explicit and says WHY each name is in it, rather
        # than inherited from a function with different intent. Everything
        # not named here is generated household data and goes.
        keep = {
            "tenants", "users", "sessions",   # the household and its logins
            "device_tokens",                  # the phone's standing sign-in
            "api_tokens",                     # scoped collector credentials
            "tenant_keys",                    # the tenant's encryption keys
        } | set(ext.gate.demo_keep_tables())  # whatever an add-on must keep
        names = {r["table_name"] for r in admin.execute(
            """SELECT DISTINCT table_name FROM information_schema.columns
               WHERE table_schema='public' AND column_name='tenant_id'"""
        ).fetchall()} - keep
        ordered = sorted(names - set(tenancy.DELETE_LAST)) + \
            [t for t in tenancy.DELETE_LAST if t in names]
        with admin.transaction():
            for t in ordered:
                admin.execute(sql.SQL("DELETE FROM {} WHERE tenant_id = %s")
                              .format(sql.Identifier(t)), (tid,))
            # an add-on's rows are kept above so a crash here cannot lose
            # them, and re-stamped here so the household starts from the
            # state the gate wants, whatever the last run left behind
            ext.gate.on_demo_household(admin, tid)

        g = _generate(_Gen(rng, years, today))
        conn = tenancy.tenant_connect(tid)
        try:
            _write(conn, g, rng, today)
            _configure(conn, g, rng, today, budget, bills,
                       login=None, rng_seed=seed, demo_mode=False)
        finally:
            conn.close()
    finally:
        admin.close()
    return {"ok": True, "refreshed": True, "tenant_id": tid, "seed": seed,
            "years": years, "transactions": len(g.txns),
            "through": today.isoformat()}


def _write(conn, g: _Gen, rng: random.Random, today: dt.date) -> None:
    # NB: investment institution names must never share a token with the
    # employer/payroll name — budget.cash_events keys its "sold investments
    # to cover spending" heuristic on investment-institution tokens, and a
    # shared token ("Acme") made every paycheck read as shortfall funding
    items = [
        ("demo-bank", "Northwind Bank"),
        # the side practice banks somewhere else, the way a real one does
        ("demo-biz-item", "Cedar Ridge Business Bank"),
        ("demo-visa-item", "Summit Card Co"),
        ("demo-amex-item", "Northline Financial"),
        ("demo-brok-item", "Evergreen Brokerage"),
        ("demo-401k-item", "Meridian Retirement Services"),
        ("demo-roth-item", "Evergreen Brokerage"),
        ("demo-cry-item", "CryptoX"),
        ("demo-mort-item", "Maple Street Mortgage Co"),
        ("demo-auto-item", "Guardian Auto Finance"),
    ]
    for iid, inst in items:
        conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, status, raw)"
            " VALUES (%s,'csv',%s,'ok','{}'::jsonb)"
            " ON CONFLICT (tenant_id, id) DO NOTHING", (iid, inst))

    # cash balances: plausible draws rather than exact flow walks — only
    # TODAY's balance is stored, and a bounded checking + partially spent
    # savings reads truer than ten years of untouched accumulation.
    # Investment balances are the SUM of the accumulated holdings at
    # today's prices, so accounts, holdings, and the reconstructed trend
    # all agree with the ledger's share history.
    chk_bal = round(g.take_home_m(today.year)
                    * rng.uniform(0.6, 1.6), 2)
    sav_bal = round(g.savings_m * len(list(_months(g.start, today)))
                    * rng.uniform(0.55, 0.85), 2)
    # the business account's balance is its own ledger, netted (positive =
    # money out by the demo's convention, so the sum is inverted)
    biz_bal = round(-sum(t["amount"] for t in g.txns
                         if t["account_id"] == "demo-biz"), 2)

    def held(aid: str) -> float:
        return round(sum(qty * g.price(sym, today)
                         for (a, sym), qty in g.hold.items() if a == aid), 2)
    brok_bal, k401_bal = held("demo-brok"), held("demo-401k")
    roth_bal, cry_bal = held("demo-roth"), held("demo-cry")

    accounts = [
        ("demo-chk", "demo-bank", "Everyday Checking", "depository",
         "checking", "4021", chk_bal),
        ("demo-sav", "demo-bank", "Rainy Day Savings", "depository",
         "savings", "4022", sav_bal),
        ("demo-visa", "demo-visa-item", "Summit Sapphire", "credit",
         "credit card", "7314", round(g.card_open["demo-visa"]
                                      + g.card_owed["demo-visa"], 2)),
        ("demo-amex", "demo-amex-item", "Northline Cash Rewards", "credit",
         "credit card", "9962", round(g.card_open["demo-amex"]
                                      + g.card_owed["demo-amex"], 2)),
        # the side practice's own account — entity-owned, so the hard
        # separation (business money out of the personal budget) is real
        ("demo-biz", "demo-biz-item", "Fernwood Studio Checking",
         "depository", "checking", "8815", biz_bal),
        ("demo-brok", "demo-brok-item", "Taxable Brokerage", "investment",
         "brokerage", None, brok_bal),
        ("demo-401k", "demo-401k-item", "Meridian 401(k)", "investment",
         "401k", None, k401_bal),
        ("demo-roth", "demo-roth-item", "Roth IRA", "investment",
         "roth", None, roth_bal),
        ("demo-cry", "demo-cry-item", "CryptoX Wallet", "investment",
         "crypto", None, cry_bal),
        # live loan accounts feed the net-worth property layer (and the
        # Accounts page) with real balances
        ("demo-mort", "demo-mort-item", "Home Mortgage", "loan",
         "mortgage", "3308", g.mort_balance),
        ("demo-auto", "demo-auto-item", "Auto Loan", "loan",
         "auto", "5521", g.car_loan_balance),
    ]
    for aid, iid, name, typ, sub, mask, bal in accounts:
        conn.execute(
            """INSERT INTO accounts (id, item_id, name, type, subtype, mask,
                                     balance_current, balance_available,
                                     updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,now())
               ON CONFLICT (tenant_id, id) DO NOTHING""",
            (aid, iid, name, typ, sub, mask, bal,
             bal if typ == "depository" else None))

    for t in g.txns:
        conn.execute(
            """INSERT INTO transactions (id, account_id, date, amount, name,
                   merchant_name, category_primary, category_detailed,
                   pending, removed, raw, category_source)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,'import')
               ON CONFLICT (tenant_id, id) DO NOTHING""",
            (t["id"], t["account_id"], t["date"], t["amount"], t["name"],
             t["merchant"], t["primary"], t["detailed"],
             jsonb(t["raw"] or {})))

    # card liabilities: due date + last statement → forecast card scenarios
    for aid in ("demo-visa", "demo-amex"):
        due = (today.replace(day=1) + dt.timedelta(days=35)).replace(day=5)
        conn.execute(
            """INSERT INTO liabilities (account_id, raw)
               VALUES (%s,%s) ON CONFLICT (tenant_id, account_id)
               DO UPDATE SET raw = EXCLUDED.raw""",
            (aid, jsonb({"next_payment_due_date": due.isoformat(),
                         "last_statement_balance":
                             round(g.card_stmt[aid], 2)})))

    # holdings = the shares the ledger actually accumulated, at today's
    # prices — the same numbers _reconstruct_trend walks backward from
    names = dict(FUNDS) | dict(COINS)
    for (aid, sym), qty in sorted(g.hold.items()):
        price = g.price(sym, today)
        conn.execute(
            """INSERT INTO holdings (account_id, symbol, name, quantity,
                                     price, value, as_of)
               VALUES (%s,%s,%s,%s,%s,%s,now())""",
            (aid, sym, names[sym], round(qty, 4), price,
             round(qty * price, 2)))

    # the tax/income spine → Cash Flow reported income + career chart
    for y in range(g.start.year, today.year + 1):
        wages = round(g.gross(y), 2)
        conn.execute(
            """INSERT INTO income_annual (year, total_income, agi,
                   taxable_income, tax_paid, wages, primary_wages,
                   ss_earnings, medicare_earnings, joint)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,0)
               ON CONFLICT (tenant_id, year) DO NOTHING""",
            (y, wages, round(wages * 0.96, 2), round(wages * 0.82, 2),
             round(wages * rng.uniform(0.16, 0.21), 2), wages, wages,
             round(min(wages, 168_600), 2), wages))

    _receipts(conn, g, rng, today)
    _business(conn, g, rng, today)
    _notes(conn, g, rng, today)


# notes a real person actually leaves on a transaction — a mix of receipts
# to chase, splits to remember, and business justification (the note is what
# makes a Schedule C line defensible a year later)
_NOTES_PERSONAL = [
    "Split with Sam — they Venmo'd their half",
    "Birthday gift, not groceries",
    "Price looked high — check the receipt against the shelf tag",
    "Annual renewal; cancel next year if unused",
    "Reimbursed by insurance on the 14th",
    "Bought for the kids' school project",
    "This is the deposit, balance due on pickup",
    "Warranty is 2 years — receipt in the folder",
    "Duplicate charge? watch for the reversal",
]
_NOTES_BUSINESS = [
    "Client work — Harbor Point rebrand, billable",
    "Deductible: software used only for client projects",
    "Client lunch — discussed the Q3 retainer (50% deductible)",
    "Subcontractor invoice #{n} — 1099 vendor",
    "Print run billed back to the client at cost",
    "Home-office supplies — kept for the Schedule C",
    "Estimated tax payment, federal",
    "Liability policy — annual premium, business only",
]


def _business(conn, g: _Gen, rng: random.Random, today: dt.date) -> None:
    """The business surfaces, populated: the entity itself, its account, a
    handful of business costs paid on a PERSONAL card (the case the
    per-transaction override exists for), and the equity movements behind
    the owner's capital account. Every identifier is fictional — this
    instance is public.
    """
    from .engine import entities, equity

    formed = getattr(g, "biz_start", today - dt.timedelta(days=1100))
    ent = entities.create_entity(
        conn, name=BIZ_NAME, structure="single_member_llc", state=BIZ_STATE,
        formation_date=formed - dt.timedelta(days=21),
        business_start_date=formed,
        ein=BIZ_EIN, registered_agent=BIZ_AGENT, fiscal_year_end="12-31")
    eid = ent["id"]
    conn.execute(
        """INSERT INTO entity_membership (entity_id, member_name,
                                          ownership_pct, is_manager)
           VALUES (%s,%s,100,TRUE)""", (eid, "Demo Owner"))
    # the whole account belongs to the entity
    entities.assign_account(conn, "demo-biz", eid)

    # …and a few business costs paid on a personal card: the per-transaction
    # override, which is what a real single-member LLC's books look like
    personal_biz = [t for t in g.txns
                    if t["account_id"] in ("demo-visa", "demo-amex")
                    and t["amount"] > 0
                    # never the unbilled subscription: tagging one of its
                    # months as the business's breaks the monthly chain the
                    # recurring detector is meant to find (which month gets
                    # tagged depends on the calendar, so the demo's "pending
                    # proposal" came and went with the date)
                    and t["merchant"] == ONLINE]
    for t in rng.sample(personal_biz, min(6, len(personal_biz))):
        entities.assign_transaction(conn, t["id"], eid)

    # equity: the opening contribution, then the monthly draws already in
    # the ledger, recorded against the capital account
    opening = next((-t["amount"] for t in g.txns
                    if t["account_id"] == "demo-biz"
                    and t["name"] == "OWNER CONTRIBUTION"), None)
    if opening:
        # the SAME number the ledger shows, not a second random draw
        equity.record_movement(
            conn, eid, kind="contribution", amount=round(opening, 2),
            date=formed,
            note="Opening capital — funded the business checking account")
    # EVERY draw, not a recent slice: the capital account is cumulative, so
    # recording a subset would make the equity page disagree with the ledger
    # it sits next to.
    draws = sorted((t for t in g.txns if t["account_id"] == "demo-biz"
                    and t["name"] == "OWNER DRAW"),
                   key=lambda t: t["date"])
    for t in draws:
        equity.record_movement(conn, eid, kind="draw", amount=t["amount"],
                               date=t["date"], txn_id=t["id"],
                               note="Monthly owner draw")
    # one reimbursement: the owner paid a business cost personally
    if personal_biz:
        t = personal_biz[0]
        equity.record_movement(
            conn, eid, kind="reimbursement", amount=t["amount"],
            date=t["date"], txn_id=t["id"],
            note="Business supplies paid on the personal card")


def _notes(conn, g: _Gen, rng: random.Random, today: dt.date) -> None:
    """Free-text notes scattered over the ledger — enough that the feature
    is visibly in use (and searchable), nowhere near every row."""
    biz_ids = {t["id"] for t in g.txns if t["account_id"] == "demo-biz"}
    spend = [t for t in g.txns if t["amount"] > 0]
    if not spend:
        return
    # ~35 personal + ~25 business notes, weighted to the recent past so the
    # default views show them
    recent = [t for t in spend
              if t["date"] >= today - dt.timedelta(days=420)] or spend
    personal = [t for t in recent if t["id"] not in biz_ids]
    business = [t for t in recent if t["id"] in biz_ids]
    picks = [(t, rng.choice(_NOTES_PERSONAL))
             for t in rng.sample(personal, min(35, len(personal)))]
    picks += [(t, rng.choice(_NOTES_BUSINESS).format(n=rng.randint(100, 399)))
              for t in rng.sample(business, min(25, len(business)))]
    for t, note in picks:
        conn.execute(
            """INSERT INTO transaction_notes (txn_id, note)
               VALUES (%s,%s)
               ON CONFLICT (tenant_id, txn_id) DO UPDATE SET note=EXCLUDED.note""",
            (t["id"], note))


def _configure(conn, g: _Gen, rng: random.Random, today: dt.date,
               budget, bills,
               login: tuple[str, str] | None = None,
               rng_seed: int | None = None,
               demo_mode: bool = True) -> None:
    housing_payee = ("Maple Street Mortgage Co" if g.mortgage
                     else "Hillcrest Property Mgmt")
    next_m = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)

    def recent(merchant: str, fallback: float) -> float:
        """Median of the payee's last 3 ledger amounts — the same window
        bills.run's drift check uses, so seeding bills with it means
        ZERO auto-applied drifts (and no drift alerts) on a fresh demo:
        the Today strip stays at exactly reimb + proposals instead of a
        noisy pile of notifications."""
        amts = [t["amount"] for t in g.txns
                if t["merchant"] == merchant and t["amount"] > 0]
        return (round(statistics.median(amts[-3:]), 2) if len(amts) >= 3
                else round(fallback, 2))

    monthly_bills = [          # (payee, amount, due day) — `bills` is the module
        (housing_payee, g.housing, 1),
        ("City Power & Light", recent("City Power & Light", 150), 12),
        ("Clearwater Utility District",
         recent("Clearwater Utility District", 65), 18),
        ("FiberLink Internet", 70, 20),
        ("CellOne Mobile", recent("CellOne Mobile", 95), 22),
        ("SafeGuard Life", 46, 15),
        ("TrashCo Sanitation", recent("TrashCo Sanitation", 28), 6),
        ("PetShield Pet Insurance", 32, 17),
        ("Guardian Auto Finance", g.car_payment,
         min(g.car_bought.day, 28)),
        ("StreamFest", 15.99, 8),
        ("TuneBox Music", 10.99, 12),
        ("IronWorks Gym", 39.00, 2),
        ("FlixBox Video", 18.99, 11),
        ("CloudSafe Backup", 5.99, 25),
        ("NewsWire Daily", 12.99, 3),
    ]
    for payee, amount, day in monthly_bills:
        due = _clamp_day(today if today.day < day else next_m, day)
        bills.save_bill(conn, payee=payee, amount=round(amount, 2),
                            frequency="MONTHLY", interval=1,
                            next_due=due, source="demo",
                            last_seen=today - dt.timedelta(days=20),
                            merchant=payee)
    bills.save_bill(conn, payee="AutoShield Insurance",
                        amount=recent("AutoShield Insurance", 620),
                        frequency="MONTHLY", interval=6,
                        next_due=_clamp_day(today + dt.timedelta(days=60), 12),
                        source="demo", merchant="AutoShield Insurance",
                        last_seen=today - dt.timedelta(days=100))
    # income series (biweekly paychecks)
    net = round(g.gross(today.year) * g.take_home_ratio / 26, 2)
    bills.save_bill(conn, payee="Acme Manufacturing Payroll", amount=net,
                        frequency="WEEKLY", interval=2,
                        next_due=today + dt.timedelta(days=(4 - today.weekday()) % 7 + 7),
                        source="demo", income=True,
                        merchant="Acme Manufacturing",
                        last_seen=today - dt.timedelta(days=7))

    # the real recurring detector, on the seeded ledger: PixelCloud (unbilled
    # by design) becomes a genuine pending proposal, and any amount drifts
    # it auto-applies show up exactly as they would on a live instance
    bills.run(conn, today)

    # the awaiting-reimbursement story: flag the recent clinic charge
    med = conn.execute(
        "SELECT id, amount FROM transactions WHERE merchant_name = %s "
        "ORDER BY date DESC LIMIT 1", (MEDICAL,)).fetchone()
    if med:
        conn.execute(
            "INSERT INTO reimburse_flags (txn_id, expected) VALUES (%s,%s) "
            "ON CONFLICT (tenant_id, txn_id) DO NOTHING",
            (med["id"], med["amount"]))

    # the Rules page: a lived-in mix of provenances — corrections made
    # by hand, model classifications, seed matches. Categories mirror
    # what the generator already assigned, so applying them is a no-op on
    # the ledger; the page just gets real rows to show.
    for merch, cat, src in [
            ("Daily Grind Coffee", "FOOD_AND_DRINK", "user"),
            ("Harvest Table", "FOOD_AND_DRINK", "user"),
            ("Cedar Family Clinic", "MEDICAL", "llm"),
            ("Rivermart Online", "GENERAL_MERCHANDISE", "llm"),
            ("Happy Paws Pet Supply", "GENERAL_MERCHANDISE", "llm"),
            ("Little Sprouts Activities", "GENERAL_SERVICES", "llm"),
            ("Petro Stop", "TRANSPORTATION", "seed"),
            ("StreamFest", "ENTERTAINMENT", "seed")]:
        conn.execute(
            """INSERT INTO merchant_categories (merchant, category_primary,
                                                source)
               VALUES (%s,%s,%s)
               ON CONFLICT (tenant_id, merchant) DO NOTHING""",
            (merch, cat, src))

    _alert_history(conn, rng, today)

    # a plausible SSA-style benefit schedule (steady earner at this income)
    ss67 = round(g.gross(today.year) * 0.28 / 12 / 10) * 10
    ss_estimates = {"62": round(ss67 * 0.70 / 10) * 10, "67": ss67,
                    "70": round(ss67 * 1.24 / 10) * 10}

    # custom carve-out buckets over the generated merchants, so every one
    # lights up with real spend. `merchants` are lowercase match rules for
    # budget.merchant_matcher: multi-word entries match as word-boundary
    # phrases ("golden noodle" hits GOLDEN NOODLE HOUSE), single tokens as
    # token subsets ("rivermart" hits RIVERMART ONLINE). Each monthly is
    # this household's own last-12-months average (card_spending); a
    # zero-average bucket is dropped rather than shown as a dead $0 bar.
    buckets = [
        {"name": "Coffee", "parent": "food", "monthly": g.coffee_m,
         "categories": [], "merchants": ["daily grind"]},
        {"name": "Dining out", "parent": "food", "monthly": g.dining_m,
         "categories": [],
         "merchants": ["bistro verde", "taco fuego", "golden noodle",
                       "slice brothers", "harvest table"]},
        {"name": "Gas", "parent": "other", "monthly": g.gas_m,
         "categories": [], "merchants": ["petro stop", "fuelco"]},
        {"name": "Online shopping", "parent": "other", "monthly": g.online_m,
         "categories": [], "merchants": ["rivermart"]},
        {"name": "Pets", "parent": "other", "monthly": g.pets_m,
         "categories": [], "merchants": ["happy paws"]},
    ]

    # the net-worth property layer: house + car as manual assets (the
    # mortgage and auto-loan balances come live from the loan accounts)
    manual_assets = [
        {"name": "Home", "kind": "property", "value": g.home_value,
         "as_of": today.isoformat()},
        {"name": "Family SUV", "kind": "vehicle", "value": g.car_value,
         "as_of": today.isoformat()},
    ]

    with budget.config_txn(conn) as cfg:
        if login:
            # the login page pre-fills these (GET /api/demo-login). The seeder
            # already prints them and the data is synthetic, so storing the
            # password in plain config reveals nothing new on a demo instance.
            cfg["demo_login"] = {"email": login[0], "password": login[1]}
        cfg.update({
            # locks data doors + account security on the public demo; a
            # demonstration household on a live instance runs without it
            "demo_mode": demo_mode,
            # the hourly reset replays THIS household — same seed,
            # date-relative generator ⇒ always-current pristine demo
            "demo_seed": rng_seed,
            "demo_years": g.years,
            "food_monthly": g.food_m,
            "other_monthly": g.other_m,
            "budgeted_income_monthly": round(g.take_home_m(today.year) / 10) * 10,
            "checking_account_id": "demo-chk",
            "manual_assets": manual_assets,
            "birthdate": f"{g.birth_year}-06-15",
            "ss_estimates": ss_estimates,
            "custom_buckets": [b for b in buckets if b["monthly"] > 0],
            # Two goals, both plausible (a six-figure target reads absurd):
            # "Savings" stays name-keyed (the money map's Savings row + every
            # lens key on that exact name — an "Emergency fund" name left the
            # row invisible) but is OPEN-ENDED (target 0: "$X saved", no
            # made-up finish line); "Vacation" is the relatable in-progress
            # goal — $5k target, fed by the token-matched VACATION FUND
            # transfers housing_and_bills generates (~60-65% funded).
            "savings_goals": [{"name": "Savings", "target": 0,
                               "monthly_plan": g.savings_m,
                               "account_id": "demo-sav", "tokens": [],
                               "start_balance": 0},
                              {"name": "Vacation", "target": 5000,
                               "monthly_plan": 200,
                               "account_id": "demo-sav",
                               "tokens": ["vacation"],
                               "start_balance": 0}],
            "wizard_done": True,
            "wizard_steps": {k: "done" for k in
                             ("connect", "bills", "budgets", "email", "finish")},
        })


def _alert_history(conn, rng: random.Random, today: dt.date) -> None:
    """~18 months of believable alerts_log rows (message formats match
    engine/alerts.py builders) so the Alerts page has a past. All inactive
    — the LIVE strip comes from real conditions (reimb flag, proposals)."""
    r = rng

    def hist(kind, severity, message, days_ago, span=3, dismissed=0):
        first = today - dt.timedelta(days=days_ago)
        conn.execute(
            """INSERT INTO alerts_log (kind, severity, message, first_seen,
                   last_seen, active, dismissed)
               VALUES (%s,%s,%s,%s,%s,0,%s)
               ON CONFLICT (tenant_id, kind, message) DO NOTHING""",
            (kind, severity, message, first,
             min(today, first + dt.timedelta(days=span)), dismissed))

    elec = round(125 + r.uniform(4, 22), 2)
    hist("drift", "info",
         f"City Power & Light plan updated ${elec - 12.40:,.2f} → "
         f"${elec:,.2f} (tracks recent amounts).", r.randint(380, 460))
    mob = round(89 + r.uniform(3, 9), 2)
    hist("drift", "info",
         f"CellOne Mobile plan updated ${mob - 6.00:,.2f} → ${mob:,.2f} "
         f"(tracks recent amounts).", r.randint(200, 280))
    hist("drift", "info",
         "StreamFest plan updated $13.99 → $15.99 (tracks recent amounts).",
         r.randint(120, 180))
    d1 = today - dt.timedelta(days=r.randint(300, 340))
    hist("anomaly", "warn",
         f"Possible double charge: FiberLink Internet $70.00 ×2 "
         f"({d1:%m/%d} + {d1 + dt.timedelta(days=1):%m/%d})",
         (today - d1).days, span=4, dismissed=1)
    hotel = round(r.uniform(380, 720), 2)
    hist("anomaly", "warn",
         f"First-ever charge from Grand Vista Hotel: ${hotel:,.2f} — "
         f"verify you recognize it", r.randint(170, 230), span=5)
    pulled = round(r.uniform(1200, 2600), -1)
    hist("funding", "bad",
         f"${pulled:,.0f} pulled from investments this month (1 transfer) "
         f"to cover spending — ${pulled:,.0f} year to date (excl. major "
         f"purchases). Getting this to $0 is the goal.",
         r.randint(260, 320), span=6)
    xfer = round(r.uniform(2500, 6000), -2)
    d2 = today - dt.timedelta(days=r.randint(70, 110))
    hist("transfer", "info",
         f"Major investment transfer this month: ${xfer:,.0f} on "
         f"{d2:%m/%d} — treated as a one-time asset purchase, not counted "
         f"in the shortfall metric.", (today - d2).days, span=8)
    prev = round(r.uniform(120, 320), 2)
    d3 = today - dt.timedelta(days=r.randint(230, 270))
    hist("reimb", "info",
         f"1 charge awaiting reimbursement (${prev:,.0f}, oldest "
         f"{d3:%m/%d}) — match on the Reimburse page when the check "
         f"arrives.", (today - d3).days, span=18)
    hist("proposals", "info",
         "2 proposed recurring changes awaiting review on the Bills "
         "page.", r.randint(340, 400), span=4)


# line-item pools for the seeded receipts
_GROC_ITEMS = ["Whole milk 1gal", "Large eggs 12ct", "Chicken breast",
               "Bananas", "Sourdough bread", "Baby spinach", "Ground coffee",
               "Cheddar cheese", "Pasta 16oz", "Greek yogurt", "Apples 3lb"]
_PHARM_ITEMS = ["Ibuprofen 200mg", "Vitamin D3", "Toothpaste",
                "Allergy relief 30ct", "Bandages"]
_OFFICE_ITEMS = [("USB-C hub", "business"), ("Printer paper", "business"),
                 ("Notebook 3-pack", "business"), ("Desk organizer", ""),
                 ("HDMI cable", "")]


def _receipt_svg(merchant: str, date, items, total: float) -> bytes:
    """A simple receipt-looking SVG — self-contained, no vision model
    needed, renders in the receipt viewer's plain <img>."""
    h = 150 + 24 * len(items)
    lines = "".join(
        f'<text x="24" y="{118 + i * 24}" font-size="14" '
        f'font-family="monospace">{desc[:28]}</text>'
        f'<text x="356" y="{118 + i * 24}" font-size="14" '
        f'font-family="monospace" text-anchor="end">{amt:,.2f}</text>'
        for i, (desc, amt, _tag) in enumerate(items))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="380" '
           f'height="{h}" viewBox="0 0 380 {h}">'
           f'<rect width="380" height="{h}" fill="#fffdf5"/>'
           f'<text x="190" y="40" font-size="18" font-family="monospace" '
           f'text-anchor="middle" font-weight="bold">{merchant}</text>'
           f'<text x="190" y="64" font-size="13" font-family="monospace" '
           f'text-anchor="middle">{date} · thank you</text>'
           f'<line x1="24" y1="84" x2="356" y2="84" stroke="#888" '
           f'stroke-dasharray="4"/>{lines}'
           f'<line x1="24" y1="{h - 44}" x2="356" y2="{h - 44}" '
           f'stroke="#888" stroke-dasharray="4"/>'
           f'<text x="24" y="{h - 20}" font-size="15" '
           f'font-family="monospace" font-weight="bold">TOTAL</text>'
           f'<text x="356" y="{h - 20}" font-size="15" '
           f'font-family="monospace" font-weight="bold" '
           f'text-anchor="end">${total:,.2f}</text></svg>')
    return svg.encode()


def _receipts(conn, g: _Gen, rng: random.Random, today: dt.date) -> None:
    """Three parsed receipts with line items, no vision model involved:
    the transaction receipt panel, the Items page, and the Business
    expense report (business-tagged lines, dated this month when the
    ledger allows) all light up."""
    r = rng

    def pick(pred):
        cands = [t for t in g.txns if t["amount"] > 0 and pred(t)]
        return max(cands, key=lambda t: t["date"]) if cands else None

    def lines(txn, pool, tags=None):
        """Draw items from the pool and scale them to the txn amount."""
        n = min(len(pool), r.randint(3, 5))
        chosen = r.sample(pool, n)
        weights = [r.uniform(0.5, 1.5) for _ in chosen]
        out, left = [], txn["amount"]
        for i, item in enumerate(chosen):
            if isinstance(item, tuple):
                desc, tag = item
            else:
                desc, tag = item, ""
            amt = (round(txn["amount"] * weights[i] / sum(weights), 2)
                   if i < n - 1 else round(left, 2))
            left = round(left - amt, 2)
            out.append((desc, amt, tag))
        return out

    month0 = today.replace(day=1)
    grocers = {x.upper() for x in GROCERS}
    cards = ("demo-visa", "demo-amex")
    picks = []
    t = pick(lambda t: t["name"] in grocers)
    if t:
        picks.append((t, lines(t, _GROC_ITEMS)))
    t = pick(lambda t: t["name"] == PHARMACY.upper())
    if t:
        picks.append((t, lines(t, _PHARM_ITEMS)))
    # the business receipt: prefer a current-month card charge so the
    # expense report's default month view has rows
    office = (pick(lambda t: t["account_id"] in cards and t["date"] >= month0
                   and t["name"] == ONLINE.upper())
              or pick(lambda t: t["account_id"] in cards
                      and t["date"] >= month0
                      and t["primary"] == "GENERAL_MERCHANDISE")
              or pick(lambda t: t["name"] == ONLINE.upper()))
    if office:
        picks.append((office, lines(office, _OFFICE_ITEMS)))

    # the transaction-level Business report: flag the office-supplies
    # charge (it carries the business-tagged receipt) and the latest
    # airline charge, the way a self-employed household would
    flag = [office] if office else []
    plane = pick(lambda t: t["merchant"] == TRAVEL[0][0])
    if plane:
        flag.append(plane)
    for t in flag:
        conn.execute(
            "INSERT INTO business_flags (txn_id) VALUES (%s) "
            "ON CONFLICT (tenant_id, txn_id) DO NOTHING", (t["id"],))

    # …plus a random scattering so the receipts feature looks USED rather
    # than staged: a couple of dozen across the last ~18 months, drawn from
    # whichever pool suits the merchant. Business-account receipts matter
    # most (they are what an expense report is built from), so they are
    # sampled at a higher rate than the household's.
    have = {t["id"] for t, _ in picks}
    horizon = today - dt.timedelta(days=540)
    def _pool(t):
        if t["account_id"] == "demo-biz":
            return _OFFICE_ITEMS
        if t["name"] in grocers:
            return _GROC_ITEMS
        if t["name"] == PHARMACY.upper():
            return _PHARM_ITEMS
        if t["primary"] in ("GENERAL_MERCHANDISE", "GENERAL_SERVICES"):
            return _OFFICE_ITEMS
        return None
    cands = [t for t in g.txns
             if t["amount"] > 5 and t["date"] >= horizon
             and t["id"] not in have and _pool(t)]
    biz = [t for t in cands if t["account_id"] == "demo-biz"]
    home = [t for t in cands if t["account_id"] != "demo-biz"]
    extra = (r.sample(biz, min(14, len(biz)))
             + r.sample(home, min(10, len(home))))
    for t in extra:
        picks.append((t, lines(t, _pool(t))))

    for txn, items in picks:
        rid = conn.execute(
            """INSERT INTO receipts (txn_id, image, mime, status, parsed,
                                     parsed_at)
               VALUES (%s,%s,%s,'parsed',%s,now()) RETURNING id""",
            (txn["id"],
             _receipt_svg(txn["merchant"] or txn["name"], txn["date"],
                          items, txn["amount"]),
             "image/svg+xml",
             jsonb({"merchant": txn["merchant"] or txn["name"],
                    "total": txn["amount"],
                    "date": txn["date"].isoformat()}))).fetchone()["id"]
        for i, (desc, amt, tag) in enumerate(items):
            conn.execute(
                """INSERT INTO receipt_items (receipt_id, line, description,
                                              qty, amount, tag)
                   VALUES (%s,%s,%s,1,%s,%s)""",
                (rid, i, desc, amt, tag))
