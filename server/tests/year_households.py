"""Households for the Year-is-its-months properties
(specs/today-figures.md, "The Year is its months, occurrence by
occurrence").

Each builder makes a household whose January to June are closed and
frozen and whose July (tests.util TODAY) is running, around one shape a
bill occurrence can take across a month line:

* `late_on_the_2nd`    — a monthly bill due the 30th, every payment on
                         the 2nd of the month after;
* `early_in_the_month_before` — a bill due the 1st, paid a few days
                         before, in the month before;
* `two_dates_one_late` — a bill due the 2nd and the 27th, the May 27
                         payment posted on June 3;
* `missed_in_a_closed_month` — a monthly bill paid every month but
                         April;
* `partial_withdrawal` — a savings goal paid into every month, part of
                         it taken back in April;
* `still_late`         — a bill due June 25 still unpaid on July 15.

Read on July 2, while June's bills are still carried late:

* `same_date_legacy_archived` — a bill due the 2nd and the 27th, paid on
                         the 2nd, and another bill due the 27th left
                         unpaid then archived, June frozen before stable
                         ids;
* `paid_then_merchant_changed` — a bill June counted paid, whose
                         merchant was changed after June closed;
* `one_to_two_dates`   — a bill frozen in June on the 29th, its schedule
                         now the 27th and the 29th, both carried late.

Each of the first six may also be read early in the month
(`read_on`), so the late, moved and re-dated rules are reached.

`ordinary(seed)` is a tests.money_households household with its closed
months frozen: none of its bills is paid across a month line (they are
not paid at all), so no occurrence there straddles two months.

Only the fixtures' public helpers are used, so the code before the Year
read its months by key builds the same households
(tests/reference_year_figures.py); `figures(conn)` is everything the
Year page prints, which an ordinary household must keep to the cent.
"""

from __future__ import annotations

import calendar
import datetime as dt

from .money_households import Household
from .util import TODAY, add_bill, add_txn, freeze_month, write_config

EARLY = TODAY.replace(day=2)

Y = TODAY.year
CLOSED = range(1, TODAY.month)


def _base(conn, **over):
    write_config(conn, food_monthly=600, other_monthly=500,
                 budgeted_income_monthly=5200, **over)
    # a ledger that began before the year, so no month is a first month
    add_txn(conn, dt.date(Y - 1, 11, 3), 12.0, "CORNER GROCER",
            primary="FOOD_AND_DRINK")
    for m in range(1, TODAY.month + 1):
        add_txn(conn, dt.date(Y, m, 5), -2700.0, "PAYROLL ACME",
                primary="INCOME", account="chk")
        add_txn(conn, dt.date(Y, m, 9), 41.0 + m, "CORNER GROCER",
                primary="FOOD_AND_DRINK")


def _freeze(conn):
    for m in CLOSED:
        freeze_month(conn, Y, m)
    conn.commit()


def _last(y, m):
    return dt.date(y, m, calendar.monthrange(y, m)[1])


def late_on_the_2nd(conn):
    _base(conn)
    add_bill(conn, "Harbor Lofts", 1450.0, next_due=dt.date(Y, 7, 30),
             last_seen=dt.date(Y, 6, 30))
    for m in range(1, TODAY.month + 1):
        add_txn(conn, dt.date(Y, m, 2), 1450.0, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
    _freeze(conn)


def early_in_the_month_before(conn):
    _base(conn)
    add_bill(conn, "Kestrel Power", 120.0, next_due=dt.date(Y, 8, 1),
             last_seen=dt.date(Y, 7, 1))
    pays = [dt.date(Y - 1, 12, 28)] + [_last(Y, m) - dt.timedelta(days=2)
                                       for m in range(1, TODAY.month)]
    for d in pays:
        add_txn(conn, d, 120.0, "KESTREL POWER", account="chk",
                primary="RENT_AND_UTILITIES")
    _freeze(conn)


def two_dates_one_late(conn):
    _base(conn)
    add_bill(conn, "Willow Daycare", 285.0, by_month_day=[2, 27],
             next_due=dt.date(Y, 7, 27), last_seen=dt.date(Y, 7, 2))
    for m in range(1, TODAY.month + 1):
        add_txn(conn, dt.date(Y, m, 2), 285.0, "WILLOW DAYCARE",
                account="chk")
        if m < TODAY.month:
            day = dt.date(Y, 6, 3) if m == 5 else dt.date(Y, m, 27)
            add_txn(conn, day, 285.0, "WILLOW DAYCARE", account="chk")
    _freeze(conn)


def missed_in_a_closed_month(conn):
    _base(conn)
    add_bill(conn, "Phone Line", 65.0, next_due=dt.date(Y, 7, 10),
             last_seen=dt.date(Y, 6, 10))
    for m in range(1, TODAY.month + 1):
        if m != 4:
            add_txn(conn, dt.date(Y, m, 10), 65.0, "PHONE LINE",
                    account="chk")
    _freeze(conn)


def partial_withdrawal(conn):
    conn.execute(
        "INSERT INTO accounts (id,item_id,name,type,subtype,"
        "balance_current) VALUES ('sav','it1','Test Savings','depository',"
        "'savings',2400) ON CONFLICT (tenant_id, id) DO NOTHING")
    _base(conn, savings_goals=[{"name": "Savings", "target": 9000,
                                "monthly_plan": 300, "tokens": [],
                                "start_balance": 0, "account_id": "sav"}])
    for m in range(1, TODAY.month + 1):
        d = dt.date(Y, m, 6)
        add_txn(conn, d, 300.0, "MOVE TO SAVINGS", primary="TRANSFER_OUT",
                account="chk")
        add_txn(conn, d, -300.0, "MOVE FROM CHECKING",
                primary="TRANSFER_IN", account="sav")
    d = dt.date(Y, 4, 18)
    add_txn(conn, d, 120.0, "MOVE OUT OF SAVINGS", primary="TRANSFER_OUT",
            account="sav")
    add_txn(conn, d, -120.0, "MOVE IN FROM SAVINGS", primary="TRANSFER_IN",
            account="chk")
    _freeze(conn)


def still_late(conn):
    _base(conn)
    add_bill(conn, "Auto Cover", 210.0, next_due=dt.date(Y, 6, 25),
             last_seen=dt.date(Y, 5, 25))
    for m in range(1, 6):
        add_txn(conn, dt.date(Y, m, 25), 210.0, "AUTO COVER", account="chk")
    _freeze(conn)


STRADDLE_BILLS = (("Harbor Lofts", 1450.0, {"days": [30]}),
                  ("Kestrel Power", 120.0, {"days": [1]}),
                  ("Willow Daycare", 285.0, {"days": [2, 27]}),
                  ("Phone Line", 65.0, {"days": [10]}),
                  ("Auto Cover", 210.0, {"days": [28]}))


def straddling(conn, seed: int):
    """Two to four bills of STRADDLE_BILLS, each occurrence from December
    on paid a few days either side of its date, some missed, some paid
    twice, the amount now and then a little off: the shapes above, mixed
    at random (the same household for the same seed), read on the
    running month's day it returns."""
    import random
    rng = random.Random(seed)
    # the running month read early, while last month's bills are still
    # carried, or later on
    today = TODAY.replace(day=rng.choice((1, 2, 3, 5, 8, TODAY.day)))
    _base(conn)
    for payee, amount, how in rng.sample(STRADDLE_BILLS, rng.randint(2, 4)):
        days = how["days"]
        first = dt.date(Y, 7, min(days[0], 28))
        kw = ({"by_month_day": days} if len(days) > 1 else {})
        add_bill(conn, payee, amount, next_due=first,
                 last_seen=first - dt.timedelta(days=31), **kw)
        for y, m in [(Y - 1, 12)] + [(Y, mo) for mo in range(1, 8)]:
            for d in days:
                due = dt.date(y, m, min(d, calendar.monthrange(y, m)[1]))
                r = rng.random()
                if r < 0.15:
                    continue                    # missed
                pay = due + dt.timedelta(days=rng.randint(-5, 6))
                if pay > today:
                    continue
                amt = amount if rng.random() < 0.8 else round(
                    amount * rng.uniform(0.9, 1.1), 2)
                add_txn(conn, pay, amt, payee.upper(), account="chk",
                        primary="RENT_AND_UTILITIES")
                if r > 0.95:                    # paid twice
                    add_txn(conn, pay + dt.timedelta(days=1), amt,
                            payee.upper(), account="chk",
                            primary="RENT_AND_UTILITIES")
    _freeze(conn)
    return today


def _d(m, day):
    return dt.date(Y, m, min(day, calendar.monthrange(Y, m)[1]))


def _strip_uids(conn):
    """The closed months' frozen schedules as stored before bills had a
    stable id."""
    import json
    for m in CLOSED:
        row = conn.execute("SELECT bills FROM budget_snapshots WHERE "
                           "year=%s AND month=%s", (Y, m)).fetchone()
        if row is None or not row["bills"]:
            continue
        b = row["bills"]
        for r in b.get("rows") or ():
            r.pop("uid", None)
        conn.execute("UPDATE budget_snapshots SET bills=%s::jsonb WHERE "
                     "year=%s AND month=%s", (json.dumps(b), Y, m))
    conn.commit()


def _pay(conn, payee, amount, days):
    for d in days:
        add_txn(conn, d, amount, payee.upper(), account="chk",
                primary="RENT_AND_UTILITIES")


def same_date_legacy_archived(conn):
    _base(conn)
    add_bill(conn, "Willow Daycare", 150.0, by_month_day=[2, 27],
             next_due=_d(6, 27), last_seen=_d(6, 2))
    add_bill(conn, "Harbor Water", 150.0, next_due=_d(6, 27),
             last_seen=_d(5, 27))
    for m in range(1, 7):
        _pay(conn, "Willow Daycare", 150.0,
             [_d(m, 2)] + ([_d(m, 27)] if m < 6 else []))
        if m < 6:
            _pay(conn, "Harbor Water", 150.0, [_d(m, 27)])
    _pay(conn, "Willow Daycare", 150.0, [_d(7, 2)])
    _freeze(conn)
    _strip_uids(conn)
    from oikonome.engine import bills
    bills.archive_bill(conn, "Harbor Water")
    conn.commit()
    return EARLY


def paid_then_merchant_changed(conn):
    _base(conn)
    add_bill(conn, "Phone Line", 65.0, next_due=_d(6, 28),
             last_seen=_d(5, 28))
    _pay(conn, "Phone Line", 65.0, [_d(m, 28) for m in range(1, 7)])
    _freeze(conn)
    add_bill(conn, "Phone Line", 65.0, next_due=_d(6, 28),
             last_seen=_d(5, 28), merchant="NEWTEL WIRELESS")
    conn.commit()
    return EARLY


def one_to_two_dates(conn):
    _base(conn)
    add_bill(conn, "Willow Daycare", 285.0, next_due=_d(6, 29),
             last_seen=_d(5, 29))
    _pay(conn, "Willow Daycare", 285.0, [_d(m, 29) for m in range(1, 6)])
    _freeze(conn)
    add_bill(conn, "Willow Daycare", 285.0, by_month_day=[27, 29],
             next_due=_d(6, 27), last_seen=_d(5, 29))
    conn.commit()
    return EARLY


def read_on(conn, day: dt.date) -> dt.date:
    """The household as the ledger stood on `day`: no row after it."""
    conn.execute("DELETE FROM transactions WHERE date > %s", (day,))
    conn.commit()
    return day


SHAPES = {f.__name__: f for f in (
    late_on_the_2nd, early_in_the_month_before, two_dates_one_late,
    missed_in_a_closed_month, partial_withdrawal, still_late,
    same_date_legacy_archived, paid_then_merchant_changed,
    one_to_two_dates)}
# The days of the month the first six shapes are also read on, early in
# the month as well as on TODAY.
EARLY_DAYS = (1, 2, 3)


def ordinary(seed: int):
    """A money_households household with a plan, its closed months of
    the year frozen (the caller closes `.conn`)."""
    h = Household(seed, plan=True)
    for m in CLOSED:
        if any((d.year, d.month) == (Y, m) for d in h.months):
            freeze_month(h.conn, Y, m)
    h.conn.commit()
    return h


def build(kind: str, seed: int):
    """(conn, today, close) for household `seed` of `kind`: "ordinary",
    "straddling", or a SHAPES name (whose seed, when not 0, is the day of
    the running month it is read on)."""
    from .util import make_db
    if kind == "ordinary":
        h = ordinary(seed)
        return h.conn, TODAY, h.close
    conn = make_db()
    if kind == "straddling":
        return conn, straddling(conn, seed), conn.close
    today = SHAPES[kind](conn) or TODAY
    if seed:
        today = read_on(conn, TODAY.replace(day=seed))
    return conn, today, conn.close


def _no_ids(v):
    """A figure without the stable ids a build draws at random."""
    if isinstance(v, dict):
        return {k: _no_ids(x) for k, x in v.items() if k != "uid"}
    if isinstance(v, list):
        return [_no_ids(x) for x in v]
    return v


def figures(conn, today: dt.date = TODAY) -> dict:
    """What the Year page prints for `today`'s year, as JSON."""
    import json
    from unittest import mock

    from oikonome.web import lenses
    noon = dt.datetime.combine(today, dt.time(12))
    with mock.patch("oikonome.localtime.now_local", return_value=noon):
        yr = lenses.year_summary(conn, Y, today=today)
    return _no_ids(json.loads(json.dumps(yr, default=str)))
