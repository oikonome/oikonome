"""Money moved by direct deposit or ACH lands on business days, so the
cash forecast walks a paycheck scheduled on a weekend or a US bank holiday
in on the business day before (unless the series' own deposits show it
arrives after) and a bill scheduled on one out on the business day after.
Only the forecast's dates move: the month plan still counts a paycheck in
the month it pays. Figures and names are invented."""

import datetime as dt
import unittest

from oikonome.engine import budget, business_days, forecast
from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date


class BankCalendarTests(unittest.TestCase):
    def test_the_next_three_years_of_bank_holidays(self):
        want = {
            2026: [D(2026, 1, 1), D(2026, 1, 19), D(2026, 2, 16),
                   D(2026, 5, 25), D(2026, 6, 19), D(2026, 7, 4),
                   D(2026, 9, 7), D(2026, 10, 12), D(2026, 11, 11),
                   D(2026, 11, 26), D(2026, 12, 25)],
            # Independence Day on a Sunday is observed on the Monday;
            # Juneteenth and Christmas on a Saturday are not moved
            2027: [D(2027, 1, 1), D(2027, 1, 18), D(2027, 2, 15),
                   D(2027, 5, 31), D(2027, 6, 19), D(2027, 7, 5),
                   D(2027, 9, 6), D(2027, 10, 11), D(2027, 11, 11),
                   D(2027, 11, 25), D(2027, 12, 25)],
            2028: [D(2028, 1, 1), D(2028, 1, 17), D(2028, 2, 21),
                   D(2028, 5, 29), D(2028, 6, 19), D(2028, 7, 4),
                   D(2028, 9, 4), D(2028, 10, 9), D(2028, 11, 11),
                   D(2028, 11, 23), D(2028, 12, 25)],
        }
        for year, days in want.items():
            self.assertEqual(sorted(business_days.bank_holidays(year)), days)

    def test_rolling(self):
        bd = business_days
        # Saturday holiday: the Friday before is open
        self.assertTrue(bd.is_business_day(D(2026, 7, 3)))
        self.assertEqual(bd.previous_business_day(D(2026, 9, 7)),
                         D(2026, 9, 4))
        self.assertEqual(bd.next_business_day(D(2026, 7, 25)),
                         D(2026, 7, 27))
        self.assertEqual(bd.next_business_day(D(2027, 7, 4)), D(2027, 7, 6))
        self.assertEqual(bd.add_business_days(D(2026, 11, 24), 3),
                         D(2026, 11, 30))


class ForecastBusinessDayTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0)
        self.conn.execute("UPDATE accounts SET balance_available=1000, "
                          "balance_current=1000 WHERE id='chk'")
        self.conn.execute("UPDATE accounts SET balance_current=0 "
                          "WHERE id='card'")

    def tearDown(self):
        self.conn.close()

    def _events(self, payee):
        f = forecast.build(self.conn, today=TODAY, days=60)
        return f, [(d, a) for d, a, p in f["events"] if p == payee]

    def test_a_saturday_payday_lands_the_friday_before(self):
        add_bill(self.conn, "BRIGHTLINE PAYROLL", 2400.0, income=True,
                 next_due=D(2026, 8, 1), last_seen=D(2026, 7, 1))
        f, ev = self._events("BRIGHTLINE PAYROLL")
        self.assertEqual(ev[0], ("2026-07-31", 2400.0))
        # Labor Day: the Friday before
        self.assertEqual(ev[1][0], "2026-09-01")
        # left at month end reads the walk, so it now holds the check
        self.assertEqual(dict(f["pace_stmt"]["series"])["2026-07-31"],
                         1000.0 + 2400.0)

    def test_a_holiday_payday_lands_the_business_day_before(self):
        add_bill(self.conn, "BRIGHTLINE PAYROLL", 2400.0, income=True,
                 next_due=D(2026, 9, 7), last_seen=D(2026, 8, 7))
        _f, ev = self._events("BRIGHTLINE PAYROLL")
        self.assertIn(("2026-09-04", 2400.0), ev)

    def test_the_series_own_history_wins(self):
        # the last time payday fell on a Sunday the deposit came Monday
        for d in (D(2026, 1, 30), D(2026, 3, 2), D(2026, 4, 1),
                  D(2026, 5, 1), D(2026, 6, 1), D(2026, 7, 1)):
            add_txn(self.conn, d, -2400.0, "BRIGHTLINE PAYROLL",
                    primary="INCOME", account="chk")
        add_bill(self.conn, "BRIGHTLINE PAYROLL", 2400.0, income=True,
                 next_due=D(2026, 8, 1), last_seen=D(2026, 7, 1))
        _f, ev = self._events("BRIGHTLINE PAYROLL")
        self.assertEqual(ev[0][0], "2026-08-03")

    def test_a_weekend_or_holiday_bill_posts_the_business_day_after(self):
        add_bill(self.conn, "RIVERTOWN WATER", 80.0,
                 next_due=D(2026, 7, 25), last_seen=D(2026, 6, 25))
        add_bill(self.conn, "HILLCREST LOAN", 310.0,
                 next_due=D(2026, 9, 7), last_seen=D(2026, 8, 7))
        f = forecast.build(self.conn, today=TODAY, days=60)
        got = {(d, p) for d, _a, p in f["events"]}
        self.assertIn(("2026-07-27", "RIVERTOWN WATER"), got)
        self.assertIn(("2026-09-08", "HILLCREST LOAN"), got)
        self.assertNotIn(("2026-07-25", "RIVERTOWN WATER"), got)

    def test_the_month_plan_counts_the_check_in_the_month_it_pays(self):
        add_bill(self.conn, "BRIGHTLINE PAYROLL", 2400.0, income=True,
                 next_due=D(2026, 8, 1), last_seen=D(2026, 7, 1))
        cfg = budget.load_config(self.conn)
        parts = budget._month_income_parts(self.conn, cfg, TODAY,
                                           closed=False)
        self.assertEqual((parts["expected"], parts["expected_dates"]),
                         (0.0, []))
        st = report.gather(self.conn, TODAY)
        ctx = todayview.build_context(st)
        # "paychecks still due" is the plan's July: the check is August's
        self.assertEqual((st["income_due_total"], st["income_due_dates"]),
                         (0.0, []))
        # the timeline and the month-end tile carry the rolled check
        self.assertEqual(ctx["month_end"]["balance"], 1000.0 + 2400.0)
        self.assertIn(("2026-07-31", 2400.0, "BRIGHTLINE PAYROLL"),
                      [(r[0], r[1], r[2]) for r in ctx["fc_rows"]])


if __name__ == "__main__":
    unittest.main()
