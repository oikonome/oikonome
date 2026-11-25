"""A paycheck or a bill scheduled on the day the forecast is made — any
weekday, a bank holiday, the business day after one — is never dropped:
its landing day may roll to before today (a Saturday payday walked in on
Friday), and with no matching deposit yet it is expected tomorrow at the
earliest; a bill rolled forward is walked out on the next business day
(specs/today-figures.md, "Paydays and bills land on business days").
Figures and names are invented."""

import datetime as dt
import unittest

from oikonome.engine import business_days, forecast

from .util import add_bill, make_db, write_config

D = dt.date
AS_OF = ([D(2026, 7, 13) + dt.timedelta(days=i) for i in range(7)]
         + [D(2026, 9, 7), D(2026, 9, 8), D(2026, 11, 26), D(2026, 12, 25),
            D(2026, 12, 26), D(2027, 1, 1)])


class OnTheAsOfDay(unittest.TestCase):
    def test_every_weekday_and_holiday(self):
        for as_of in AS_OF:
            for income in (True, False):
                conn = make_db()
                try:
                    write_config(conn, food_monthly=0, other_monthly=0)
                    if income:
                        add_bill(conn, "NORTHSTAR PAYROLL", 2000.0,
                                 income=True, frequency="WEEKLY", interval=2,
                                 next_due=as_of,
                                 last_seen=as_of - dt.timedelta(days=14))
                    else:
                        add_bill(conn, "RIVERTOWN WATER", 80.0,
                                 next_due=as_of,
                                 last_seen=as_of - dt.timedelta(days=31))
                    forecast.forget_learned()
                    f = forecast.build(conn, today=as_of)
                finally:
                    conn.close()
                name = "NORTHSTAR PAYROLL" if income else "RIVERTOWN WATER"
                ev = [D.fromisoformat(d) for d, _a, p in f["events"]
                      if p.startswith(name)]
                with self.subTest(as_of=as_of, income=income):
                    self.assertTrue(ev, f["events"])
                    first = min(ev)
                    tomorrow = as_of + dt.timedelta(days=1)
                    self.assertGreaterEqual(first, tomorrow)
                    # never later than the next business day from tomorrow
                    self.assertLessEqual(
                        first, business_days.next_business_day(tomorrow))
