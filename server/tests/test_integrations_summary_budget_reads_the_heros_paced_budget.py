"""The integrations summary's month budget is the money the hero judges.

A ledger that begins inside the month may spend only the budget's share
from its first day to month end (budget.pace_window,
specs/short-ledger-pacing.md). The hero, the tiles and the lens rows read
that share; a summary that reported the whole month's budget would hand a
monitor or an assistant room the app itself says the month never had, and
its budget minus its spend would disagree with the hero's "left".
"""

import datetime as dt
import unittest

from oikonome.web import integrations, report

from .util import add_ledger_history, add_txn, make_db, write_config

TODAY = dt.date(2026, 10, 16)           # day 16 of a 31-day October
FIRST = dt.date(2026, 10, 10)           # the ledger's first day


class SummaryMonthBudgetIsPaced(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=900)

    def tearDown(self):
        self.conn.close()

    def test_a_ledger_begun_mid_month_reports_the_paced_budget(self):
        add_txn(self.conn, FIRST, 40.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
        st = report.gather(self.conn, TODAY)
        self.assertLess(st["month_share"], 1.0)
        s = integrations.summary(self.conn, TODAY, st=st)
        self.assertAlmostEqual(s["month"]["budget"],
                               round(st["variable_budget_paced"], 2), 2)
        self.assertLess(s["month"]["budget"], 1500.0)

    def test_an_established_ledger_reports_the_whole_budget(self):
        add_ledger_history(self.conn, dt.date(2026, 10, 1))
        add_txn(self.conn, FIRST, 40.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
        s = integrations.summary(self.conn, TODAY)
        self.assertEqual(s["month"]["budget"], 1500.0)


if __name__ == "__main__":
    unittest.main()
