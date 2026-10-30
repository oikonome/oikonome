"""A closed month with no frozen schedule stands on the income the ledger
realised, not on its own paychecks.

The Year lens reports a STANDING plan surplus — what an ordinary month
leaves — because the month it reads from may be a three-paycheck month, a
refund month, a month a check slipped out of. A closed month judged before
schedules were frozen has no series of its own to average, and today's
series do not speak for it (a job that started later would count at its
scheduled rate, one that ended would vanish). Reporting the month's own
surplus instead made one lucky month read as every month: a biweekly
household's three-check month promised a whole extra paycheck a month.
The ledger itself says what an ordinary month brought in: the average of
the whole months before it, each read the way the month itself was.
"""

import datetime as dt
import unittest

from oikonome.engine import budget, forecast

from .util import add_bill, add_txn, make_db, write_config

CHECK = 2000.0
FIRST_CHECK = dt.date(2024, 7, 5)       # a Friday; the ledger's first day
LAST_CHECK = dt.date(2025, 12, 26)
# the biweekly schedule pays three checks in May 2025 (and in November 2024)
THREE_CHECK_END = dt.date(2025, 5, 31)


def _checks():
    d = FIRST_CHECK
    while d <= LAST_CHECK:
        yield d
        d += dt.timedelta(days=14)


class StandingSurplusFromTheLedger(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=1000, other_monthly=1000,
                     budgeted_income_monthly=4000)
        for d in _checks():
            add_txn(self.conn, d, -CHECK, "NORTHWIND PAYROLL",
                    primary="INCOME", account="chk")
        add_bill(self.conn, "Northwind Payroll", CHECK, frequency="WEEKLY",
                 interval=2, income=True, next_due=dt.date(2026, 1, 9),
                 last_seen=LAST_CHECK)

    def tearDown(self):
        self.conn.close()

    def _closed(self, end):
        st = budget.month_status(self.conn, end, closed=True)
        return st, forecast.standing_surplus(self.conn, st, end, closed=True)

    def test_a_three_check_month_stands_on_the_years_average(self):
        st, standing = self._closed(THREE_CHECK_END)
        # its own figure: 3 x 2000 - 1000 food - 1000 other
        self.assertAlmostEqual(st["income"], 3 * CHECK, places=2)
        self.assertAlmostEqual(st["plan_surplus"], 4000.0, places=2)
        # the ledger began 5 July 2024, so August is its first whole month:
        # ten whole months through May, two of them three-check months,
        # (8 x 4000 + 2 x 6000) / 10 = 4400 a month
        self.assertAlmostEqual(standing, 4000.0 - 6000.0 + 4400.0, places=2)

    def test_a_two_and_a_three_check_month_stand_alike(self):
        _, three = self._closed(THREE_CHECK_END)
        _, two = self._closed(dt.date(2025, 6, 30))
        self.assertLess(abs(three - two), CHECK / 4)

    def test_a_ledger_too_short_to_average_keeps_the_months_own(self):
        # September 2024 is the ledger's second whole month: two months say
        # no more than the month does
        st, standing = self._closed(dt.date(2024, 9, 30))
        self.assertEqual(standing, st["plan_surplus"])


if __name__ == "__main__":
    unittest.main()
