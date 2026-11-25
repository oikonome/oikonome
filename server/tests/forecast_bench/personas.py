"""The household kinds the benchmark simulates, as parameter sets.

A persona is a dict of knobs turned into a frozen `Persona`; a new kind of
household is one more entry in `_SPECS`. All amounts, payees and merchants
are invented.

Knobs (defaults in `Persona`):

* `checking_open`, `savings_open` — opening balances. `two_checking` adds a
  second checking account `chk2` (the bill account) opened at
  `chk2_open`; `pin` pins the forecast's checking account (`checking_account_id`).
* `incomes` — income streams, each a dict:
    `payer`, `amount`, `cadence` one of
      weekly | biweekly (first payday `anchor` days after the world's
        start) |
      semimonthly (the 15th and the last day) |
      monthly (`day`) |
      weekday_rule (`nth` `weekday` of the month, e.g. the 2nd Wednesday) |
      quarterly (`day` of `months`) |
      gig (deposits on `weekdays`, a dry week with probability `p_dry`)
    `cv` the spread of each deposit (normal, as a share of `amount`),
    `late` (probability, max days) for a payer that is often late,
    `start_day` / `end_day` (days from the world's start) for a stream that
    begins or stops, `account`, `category` (Plaid primary, detailed),
    `plantable` — whether a household could enter it as a series (False
    for gig work and bonuses: no stable cadence or amount),
    `year_start` {share, checks} — the first `checks` deposits of each
    new calendar year smaller by `share` (deductions that reset).
* `bills` — scheduled bills, each a dict: `payee`, `amount`, `cadence`
  monthly | quarterly | semiannual | annual, `day` (and `month` for the
  longer cadences), `account` (`chk`, `chk2` or a card id), `category`,
  `cv`, `seasonal` heating | cooling, `drift` (yearly step-up share).
* `cards` — credit cards: `id`, `name`, `close_day`, `grace` (days from
  close to due), `apr`, `open` (debt at the start), `paying`
  autopay_full | hand | minimum | fixed, `fixed` (the fixed payment),
  `hand_weeks` (a hand payer pays the whole balance on a payday every
  that many weeks), `lead` (days before the due date a minimum or fixed
  payer pays), `weight` (share of the card spending), `pays_from`.
* Spending: `food_monthly`, `other_monthly` (the true mean variable spend),
  `card_share` (the share charged to cards), `cash_monthly` (cash
  withdrawals), `weekend_factor`, `payday_boost` (the 4 days after a
  payday), `one_off_rate` (per month), `one_off_range`.
* Savings: `sweep_per_payday` (a fixed transfer to savings each payday),
  `month_end_keep` (on the 1st, everything above it moves to savings),
  `bills_transfer` (two checking: moved to `chk2` each payday).
* Reaction: `react_buffer` — when the household's own projection (checking
  less the bills before its next payday) drops under it, it cuts
  discretionary spending by `react_cut` for a week and, with
  `react_from_savings`, moves money in from savings.
* `overdraft_fee`, `calendar` (us | other), `history_days` (a household
  that linked its bank only that many days before the first forecast).
* `claimed` — the design claims to handle it: the hard ceilings apply.
  `known_weak` — the reason it does not yet; it carries its baseline only.
"""

from __future__ import annotations

from dataclasses import dataclass

WAGES = ("INCOME", "INCOME_WAGES")
PENSION = ("INCOME", "INCOME_RETIREMENT_PENSION")
OTHER_INCOME = ("INCOME", "INCOME_OTHER_INCOME")
XFER_IN = ("TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER")
RENT = ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_RENT")
POWER = ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY")
WATER = ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_WATER")
PHONE = ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_TELEPHONE")
NET = ("RENT_AND_UTILITIES", "RENT_AND_UTILITIES_INTERNET_AND_CABLE")
INSURE = ("GENERAL_SERVICES", "GENERAL_SERVICES_INSURANCE")
CAR_LOAN = ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CAR_PAYMENT")
MORTGAGE = ("LOAN_PAYMENTS", "LOAN_PAYMENTS_MORTGAGE_PAYMENT")
SUBS = ("ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES")
TAX = ("GOVERNMENT_AND_NON_PROFIT", "GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT")
DUES = ("GENERAL_SERVICES", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES")


@dataclass(frozen=True)
class Persona:
    key: str
    title: str
    incomes: tuple = ()
    bills: tuple = ()
    cards: tuple = ()
    checking_open: float = 3000.0
    savings_open: float = 0.0
    two_checking: bool = False
    chk2_open: float = 500.0
    bills_transfer: float = 0.0
    pin: bool = False
    food_monthly: float = 700.0
    other_monthly: float = 900.0
    card_share: float = 0.7
    cash_monthly: float = 0.0
    weekend_factor: float = 1.4
    payday_boost: float = 1.5
    one_off_rate: float = 1.0
    one_off_range: tuple = (300.0, 3000.0)
    sweep_per_payday: float = 0.0
    month_end_keep: float | None = None
    react_buffer: float = 300.0
    react_cut: tuple = (0.2, 0.5)
    react_from_savings: bool = True
    overdraft_fee: float = 0.0
    calendar: str = "us"
    history_days: int | None = None
    claimed: bool = False
    known_weak: str | None = None


def _payroll(payer, amount, cadence="biweekly", **kw):
    return {"payer": payer, "amount": amount, "cadence": cadence,
            "cv": kw.pop("cv", 0.01), "category": kw.pop("category", WAGES),
            **kw}


def _bill(payee, amount, day, category, **kw):
    return {"payee": payee, "amount": amount, "day": day,
            "cadence": kw.pop("cadence", "monthly"), "category": category,
            **kw}


def _card(cid, name, close_day, paying, **kw):
    return {"id": cid, "name": name, "close_day": close_day,
            "paying": paying, "grace": kw.pop("grace", 25),
            "apr": kw.pop("apr", 0.24), "open": kw.pop("open", 0.0), **kw}


# the bills most households share (invented payees and amounts)
_RENT = _bill("Juniper Rentals", 1450, 1, RENT)
_POWER = _bill("Ridgeview Power", 128, 19, POWER, cv=0.12, drift=0.04)
_WATER = _bill("Rivertown Water", 55, 22, WATER, cv=0.08, drift=0.05)
_PHONE = _bill("Stonebridge Mobile", 85, 9, PHONE, drift=0.03)
_NET = _bill("Meadowlark Internet", 70, 14, NET, drift=0.06)
_INSURE = _bill("Foxglove Insurance", 140, 5, INSURE, drift=0.04)
_CAR = _bill("Kestrel Auto Loan", 320, 20, CAR_LOAN)
_STREAM = _bill("Glimmerbox Streaming", 18, 11, SUBS, account="card1")


_SPECS: dict[str, dict] = {
    "biweekly_hand_payer": dict(
        title="Biweekly salary, three cards paid in full by hand",
        incomes=(_payroll("Brightline Payroll", 2410.0, anchor=4),),
        bills=(_RENT, _POWER, _WATER, _PHONE, _NET, _INSURE, _STREAM),
        cards=(_card("card1", "Northwind card", 8, "hand", hand_weeks=(2, 4),
                     weight=0.6),
               _card("card2", "Harbor card", 21, "hand", hand_weeks=(2, 4),
                     weight=0.3),
               _card("card3", "Saltmarsh card", 2, "hand", hand_weeks=(2, 4),
                     weight=0.1)),
        checking_open=2600.0, savings_open=4000.0, card_share=0.8,
        claimed=True),
    "monthly_autopay": dict(
        title="Monthly salary, two cards on statement-in-full autopay",
        incomes=(_payroll("Brightline Payroll", 4700.0, "monthly", day=28),),
        bills=(_RENT, _POWER, _WATER, _PHONE, _NET, _INSURE, _STREAM),
        cards=(_card("card1", "Northwind card", 12, "autopay_full",
                     weight=0.7),
               _card("card2", "Harbor card", 25, "autopay_full", grace=23,
                     weight=0.3)),
        checking_open=3500.0, savings_open=6000.0, card_share=0.85,
        # passes the ceilings (long mode: nmae_30 0.117, bias_30 +0.096)
        claimed=True),
    "revolver_minimum": dict(
        title="Biweekly pay, two cards carrying a balance, minimum or fixed",
        incomes=(_payroll("Brightline Payroll", 1900.0, anchor=4),),
        bills=(_RENT, _POWER, _PHONE, _NET, _CAR, _STREAM),
        cards=(_card("card1", "Northwind card", 10, "minimum", open=6200.0,
                     apr=0.27, lead=3, weight=0.6),
               _card("card2", "Harbor card", 24, "fixed", fixed=450.0,
                     open=4100.0, apr=0.22, lead=5, weight=0.4)),
        checking_open=1200.0, savings_open=0.0, card_share=0.35,
        food_monthly=600.0, other_monthly=600.0, react_from_savings=False,
        known_weak="the usual walk pays each statement in full; a revolver "
                   "pays the minimum or a fixed amount"),
    "debit_only": dict(
        title="Biweekly pay, no cards, cash withdrawals",
        incomes=(_payroll("Brightline Payroll", 2050.0, anchor=4),),
        bills=(_RENT, _POWER, _WATER, _PHONE,
               _bill("Glimmerbox Streaming", 18, 11, SUBS)),
        cards=(), card_share=0.0, cash_monthly=240.0,
        checking_open=1800.0, savings_open=1500.0,
        food_monthly=650.0, other_monthly=700.0, claimed=True,
        known_weak="cash withdrawals are transfers, "
                   "not spending, and one-offs leave the pace with no "
                   "allowance, so the walk is rosy"),
    "two_earners": dict(
        title="Two earners: biweekly and semimonthly",
        incomes=(_payroll("Brightline Payroll", 1960.0, anchor=4),
                 _payroll("Copperleaf Clinic Payroll", 1600.0,
                          "semimonthly")),
        bills=(_RENT, _POWER, _WATER, _PHONE, _NET, _INSURE, _CAR, _STREAM),
        cards=(_card("card1", "Northwind card", 6, "autopay_full",
                     weight=0.5),
               _card("card2", "Harbor card", 19, "hand", hand_weeks=(2, 3),
                     weight=0.5)),
        checking_open=4200.0, savings_open=8000.0, card_share=0.7,
        food_monthly=1150.0, other_monthly=1500.0, claimed=True),
    "gig_weekly": dict(
        title="Weekly lumpy deposits from three gig platforms",
        incomes=(
            {"payer": "Quickcart Payouts", "amount": 440.0, "cadence": "gig",
             "weekdays": (1,), "p_dry": 0.15, "cv": 0.4,
             "category": OTHER_INCOME, "plantable": False},
            {"payer": "Ridealong Driver Pay", "amount": 350.0,
             "cadence": "gig", "weekdays": (3,), "p_dry": 0.2, "cv": 0.4,
             "category": OTHER_INCOME, "plantable": False},
            {"payer": "Taskhive Earnings", "amount": 215.0, "cadence": "gig",
             "weekdays": (4,), "p_dry": 0.3, "cv": 0.4,
             "category": OTHER_INCOME, "plantable": False}),
        bills=(_bill("Juniper Rentals", 1100, 1, RENT), _POWER, _PHONE, _NET,
               _STREAM),
        cards=(_card("card1", "Northwind card", 15, "autopay_full"),),
        checking_open=1500.0, savings_open=1200.0, card_share=0.5,
        food_monthly=500.0, other_monthly=450.0,
        known_weak="irregular income is never detected as a series, so the "
                   "walk carries outflows and no income"),
    "hourly_variable": dict(
        title="Hourly pay: weekly checks that vary with the hours",
        incomes=(_payroll("Brightline Payroll", 930.0, "weekly", anchor=4,
                          cv=0.15),),
        bills=(_RENT, _POWER, _WATER, _PHONE, _NET, _STREAM),
        cards=(_card("card1", "Northwind card", 17, "autopay_full"),),
        checking_open=1600.0, savings_open=1000.0, card_share=0.6,
        food_monthly=600.0, other_monthly=600.0,
        # passes the ceilings (long mode: nmae_30 0.167, bias_30 +0.067,
        # recall 0.91 over 70 dips)
        claimed=True),
    "commission_quarterly": dict(
        title="Monthly base pay plus a quarterly bonus",
        incomes=(_payroll("Brightline Payroll", 3600.0, "monthly", day=30),
                 {"payer": "Brightline Bonus", "amount": 4200.0,
                  "cadence": "quarterly", "months": (3, 6, 9, 12), "day": 15,
                  "cv": 0.45, "category": WAGES, "plantable": False}),
        bills=(_RENT, _POWER, _WATER, _PHONE, _NET, _INSURE, _CAR, _STREAM),
        cards=(_card("card1", "Northwind card", 9, "autopay_full",
                     weight=0.6),
               _card("card2", "Harbor card", 23, "hand", hand_weeks=(3, 5),
                     weight=0.4)),
        checking_open=3000.0, savings_open=5000.0, card_share=0.75,
        food_monthly=800.0, other_monthly=1000.0,
        known_weak="the quarterly bonus has no stable amount and is not "
                   "forecast"),
    "retiree": dict(
        title="Retiree: monthly benefit, pension, quarterly withdrawal",
        incomes=(
            _payroll("Retirement Benefit Deposit", 1900.0, "weekday_rule",
                     nth=2, weekday=2, category=PENSION, cv=0.0),
            _payroll("Pinewood Pension Trust", 1100.0, "monthly", day=1,
                     category=PENSION, cv=0.0),
            {"payer": "Harborview Brokerage", "amount": 1500.0,
             "cadence": "quarterly", "months": (1, 4, 7, 10), "day": 10,
             "cv": 0.0, "category": XFER_IN, "plantable": False}),
        bills=(_bill("Hollow Creek Property Tax", 1400, 15, TAX,
                     cadence="semiannual", month=4),
               _bill("Foxglove Insurance Annual", 1250, 3, INSURE,
                     cadence="annual", month=2),
               _bill("Foxglove Medicare Supplement", 230, 1, INSURE),
               _POWER, _WATER, _PHONE, _NET),
        cards=(_card("card1", "Northwind card", 20, "autopay_full"),),
        checking_open=5200.0, savings_open=20000.0, card_share=0.6,
        food_monthly=650.0, other_monthly=800.0, one_off_rate=0.6,
        claimed=True),
    "student_near_zero": dict(
        title="Student: small biweekly pay, a family transfer, near $0",
        incomes=(_payroll("Campus Dining Payroll", 470.0, anchor=4, cv=0.1),
                 _payroll("Family support transfer", 400.0, "monthly", day=3,
                          category=XFER_IN, cv=0.0)),
        bills=(_bill("Juniper Rentals", 650, 1, RENT),
               _bill("Stonebridge Mobile", 45, 9, PHONE),
               _bill("Glimmerbox Streaming", 11, 11, SUBS, account="card1")),
        cards=(_card("card1", "Northwind card", 14, "hand",
                     hand_weeks=(2, 2), weight=1.0),),
        checking_open=180.0, savings_open=0.0, card_share=0.3,
        food_monthly=380.0, other_monthly=270.0, one_off_rate=0.25,
        one_off_range=(150.0, 600.0), react_buffer=60.0,
        react_from_savings=False, overdraft_fee=34.0,
        known_weak="the pace is a flat daily average; spending is "
                   "front-loaded after payday and overdraft fees follow"),
    "payday_sweep": dict(
        title="Biweekly pay, a fixed transfer to savings each payday and a "
              "month-end sweep",
        incomes=(_payroll("Brightline Payroll", 2600.0, anchor=4),),
        bills=(_RENT, _POWER, _WATER, _PHONE, _NET, _INSURE, _STREAM),
        cards=(_card("card1", "Northwind card", 8, "autopay_full",
                     weight=0.7),
               _card("card2", "Harbor card", 26, "autopay_full", weight=0.3)),
        checking_open=2500.0, savings_open=9000.0, card_share=0.8,
        sweep_per_payday=400.0, month_end_keep=2600.0,
        known_weak="recurring transfers out of checking are neither bills "
                   "nor spending; the walk leaves them out"),
    "landlord": dict(
        title="Salary plus two rents, one often late; lumpy repairs",
        incomes=(_payroll("Brightline Payroll", 2400.0, anchor=4),
                 _payroll("Unit A rent", 1250.0, "monthly", day=1,
                          category=OTHER_INCOME, cv=0.0),
                 _payroll("Unit B rent", 1100.0, "monthly", day=1,
                          category=OTHER_INCOME, cv=0.0, late=(0.6, 12))),
        bills=(_bill("Copperfield Mortgage", 1900, 1, MORTGAGE),
               _bill("Hollow Creek Property Tax", 2100, 20, TAX,
                     cadence="semiannual", month=5),
               _POWER, _WATER, _PHONE, _NET, _INSURE),
        cards=(_card("card1", "Northwind card", 11, "autopay_full",
                     weight=0.6),
               _card("card2", "Harbor card", 27, "autopay_full", weight=0.4)),
        checking_open=6000.0, savings_open=12000.0, card_share=0.7,
        food_monthly=900.0, other_monthly=1100.0, one_off_rate=1.6,
        one_off_range=(250.0, 4000.0),
        known_weak="a late rent lands outside its due-date window; repairs "
                   "are lumpy"),
    "seasonal_utilities": dict(
        title="Monthly pay; heating doubles in winter, cooling in summer",
        incomes=(_payroll("Brightline Payroll", 4300.0, "monthly", day=25),),
        bills=(_RENT,
               _bill("Ridgeview Power", 152, 16, POWER, cv=0.08,
                     seasonal="cooling", drift=0.04),
               _bill("Hollow Creek Gas", 90, 24, POWER, cv=0.08,
                     seasonal="heating", drift=0.05),
               _WATER, _PHONE, _NET, _INSURE),
        cards=(_card("card1", "Northwind card", 13, "autopay_full"),),
        checking_open=3200.0, savings_open=5000.0, card_share=0.7,
        food_monthly=600.0, other_monthly=750.0, claimed=True),
    "annual_bills": dict(
        title="Biweekly pay; six annual and two semiannual bills",
        incomes=(_payroll("Brightline Payroll", 2500.0, anchor=4),),
        bills=(_RENT, _POWER, _WATER, _PHONE, _NET,
               _bill("Foxglove Auto Insurance", 780, 12, INSURE,
                     cadence="semiannual", month=3),
               _bill("Foxglove Renters Insurance", 190, 6, INSURE,
                     cadence="annual", month=2),
               _bill("Hollow Creek Vehicle Registration", 160, 20, TAX,
                     cadence="annual", month=4),
               _bill("Lakeside Warehouse Club", 120, 8, DUES,
                     cadence="annual", month=6, account="card1"),
               _bill("Pinecone Software", 99, 17, SUBS, cadence="annual",
                     month=9, account="card1"),
               _bill("Riverbend Dental Plan", 360, 2, INSURE,
                     cadence="annual", month=11),
               _bill("Hollow Creek Tax Preparer", 280, 28, DUES,
                     cadence="annual", month=3, account="card2"),
               _bill("Kestrel Roadside", 140, 10, INSURE,
                     cadence="semiannual", month=1, account="card2")),
        cards=(_card("card1", "Northwind card", 7, "autopay_full",
                     weight=0.6),
               _card("card2", "Harbor card", 22, "autopay_full", weight=0.4)),
        checking_open=3000.0, savings_open=6000.0, card_share=0.75,
        claimed=True),
    "new_household": dict(
        title="Biweekly pay; linked two weeks before the first forecast",
        incomes=(_payroll("Brightline Payroll", 2200.0, anchor=4),),
        bills=(_RENT, _POWER, _PHONE, _NET, _STREAM),
        cards=(_card("card1", "Northwind card", 16, "autopay_full"),),
        checking_open=2400.0, savings_open=3000.0, card_share=0.7,
        history_days=14,
        known_weak="two weeks of ledger: no series and no pace yet"),
    "thin_history": dict(
        title="Biweekly pay, two cards; linked five weeks before the first "
              "forecast",
        incomes=(_payroll("Brightline Payroll", 2350.0, anchor=4),),
        bills=(_RENT, _POWER, _WATER, _PHONE, _NET, _INSURE, _STREAM),
        cards=(_card("card1", "Northwind card", 10, "autopay_full",
                     weight=0.6),
               _card("card2", "Harbor card", 23, "hand", hand_weeks=(2, 4),
                     weight=0.4)),
        checking_open=2700.0, savings_open=3500.0, card_share=0.75,
        history_days=35,
        known_weak="a monthly bill seen once in five weeks is not in the "
                   "schedule yet, and one-offs leave the pace as for "
                   "everyone"),
    "job_change": dict(
        title="Biweekly employer, a three-week gap, then a semimonthly one",
        incomes=(_payroll("Brightline Payroll", 2300.0, anchor=4,
                          end_day=372),
                 _payroll("Northgate Works Payroll", 2650.0, "semimonthly",
                          start_day=393)),
        bills=(_RENT, _POWER, _WATER, _PHONE, _NET, _INSURE, _STREAM),
        cards=(_card("card1", "Northwind card", 9, "autopay_full",
                     weight=0.6),
               _card("card2", "Harbor card", 20, "autopay_full", weight=0.4)),
        checking_open=3000.0, savings_open=6000.0, card_share=0.75,
        # the stopped paycheck leaves the walk two cycles after its last
        # check (long mode: nmae_30 0.221, bias_30 +0.081)
        known_weak="the old paycheck is still walked through the gap "
                   "between jobs, so its dips are not warned of; detected "
                   "bills refuse the new payer as the twin of a merchant "
                   "sharing its first word"),
    "two_checking": dict(
        title="Salary into checking A; bills from checking B, funded by "
              "transfers",
        incomes=(_payroll("Brightline Payroll", 2500.0, anchor=4),),
        bills=tuple(dict(b, account="chk2") for b in
                    (_RENT, _POWER, _WATER, _PHONE, _NET, _INSURE)),
        cards=(_card("card1", "Northwind card", 12, "autopay_full",
                     pays_from="chk2"),),
        two_checking=True, chk2_open=600.0, bills_transfer=1150.0, pin=True,
        checking_open=2200.0, savings_open=4000.0, card_share=0.6,
        # passes the ceilings (long mode: nmae_30 0.223, bias_30 -0.093)
        claimed=True),
    "year_start_dip": dict(
        title="Biweekly pay whose first check of each year is smaller",
        # a salary: steady to the dollar, as a deduction reset needs
        incomes=(_payroll("Brightline Payroll", 2600.0, anchor=4, cv=0.002,
                          year_start={"share": 0.06, "checks": 1}),),
        bills=(_RENT, _POWER, _WATER, _PHONE, _NET, _INSURE, _STREAM),
        cards=(_card("card1", "Northwind card", 11, "autopay_full"),),
        checking_open=2600.0, savings_open=3000.0, card_share=0.7,
        food_monthly=650.0, other_monthly=800.0),
    "non_us_calendar": dict(
        title="Monthly pay on the 25th, a non-US holiday calendar",
        incomes=(_payroll("Brightline Payroll", 4100.0, "monthly", day=25),),
        bills=(_RENT, _POWER, _WATER, _PHONE, _NET, _INSURE),
        cards=(_card("card1", "Northwind card", 4, "autopay_full"),),
        checking_open=3000.0, savings_open=4000.0, card_share=0.7,
        food_monthly=600.0, other_monthly=750.0, calendar="other",
        # passes the ceilings although the engine knows US holidays only
        # (long mode: nmae_30 0.123, bias_30 +0.089, recall 0.80)
        claimed=True),
}

PERSONAS: dict[str, Persona] = {k: Persona(key=k, **v)
                                for k, v in _SPECS.items()}

# The default run (every `make test`): six sentinels in two modules.
DEFAULT_A = ("biweekly_hand_payer", "monthly_autopay", "revolver_minimum")
DEFAULT_B = ("debit_only", "payday_sweep", "student_near_zero")
DEFAULT = DEFAULT_A + DEFAULT_B
