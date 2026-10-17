"""A month no saved budget stands behind keeps a spendable figure an older
app can subtract from.

The Month lens sends `verdict: null` and `budget_set: false` for such a
month, and the current web and native clients show the variable spend
alone. A native build already in the stores predates that branch: it
always prints `variable_budget - variable_actual` as "$X left of $Y" or,
when negative, "$X over $Y to spend" in red. A null spendable reaches it
as $0, so every month with spending read as over a $0 budget. The figure
therefore stays numeric and never below the month's variable spend:
there is no budget to be over, so no client may say there is.
"""

import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import lenses

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 10, 16)


def _spend(conn, start: dt.date, end: dt.date, food=40.0, other=60.0):
    d = start
    while d <= end:
        add_txn(conn, d, food, "CORNER GROCER", primary="FOOD_AND_DRINK")
        add_txn(conn, d, other, "HARDWARE BARN")
        d += dt.timedelta(days=1)


class UnbudgetedMonthSpendableTests(unittest.TestCase):

    def _assert_never_over(self, m: dict):
        self.assertIsNone(m["verdict"])
        self.assertFalse(m["budget_set"])
        self.assertEqual(m["variance"], 0.0)
        self.assertIsInstance(m["variable_budget"], float)
        # the arithmetic an older app does on the hero
        self.assertGreaterEqual(m["variable_budget"] - m["variable_actual"],
                                0.0)

    def test_a_provisional_seed_month_never_reads_over(self):
        conn = make_db()
        try:
            _spend(conn, dt.date(2026, 10, 10), TODAY)
            self.assertFalse(budget.budgets_set(conn))
            m = lenses.month_summary(conn, 2026, 10, TODAY)
            self._assert_never_over(m)
            # the seed's paced share when it covers the spend, the same
            # figure the Today payload carries as its spendable
            st = lenses.month_state(conn, 2026, 10, TODAY, memo={})
            self.assertAlmostEqual(
                m["variable_budget"],
                round(max(st["variable_budget_paced"],
                          st["variable_actual"]), 2), 2)
        finally:
            conn.close()

    def test_a_saved_zero_budget_month_never_reads_over_zero(self):
        conn = make_db()
        try:
            _spend(conn, dt.date(2026, 9, 1), TODAY)
            write_config(conn, food_monthly=0, other_monthly=0)
            self.assertFalse(budget.budgets_set(conn))
            for y, mo in ((2026, 10), (2026, 9)):
                with self.subTest(month=mo):
                    m = lenses.month_summary(conn, y, mo, TODAY)
                    self.assertGreater(m["variable_actual"], 0)
                    self._assert_never_over(m)
        finally:
            conn.close()

    def test_a_budgeted_month_keeps_its_paced_spendable(self):
        conn = make_db()
        try:
            _spend(conn, dt.date(2026, 9, 1), TODAY, food=60.0, other=90.0)
            write_config(conn)
            m = lenses.month_summary(conn, 2026, 10, TODAY)
            self.assertTrue(m["budget_set"])
            st = lenses.month_state(conn, 2026, 10, TODAY, memo={})
            # an overspent budgeted month still reads over: the floor is
            # only for a month with nothing to judge it against
            self.assertAlmostEqual(m["variable_budget"],
                                   round(st["variable_budget_paced"], 2), 2)
            self.assertLess(m["variable_budget"], m["variable_actual"])
        finally:
            conn.close()

    def test_an_unbudgeted_year_cell_has_the_shape_of_a_no_snapshot_month(self):
        # the store build already draws a closed month with no snapshot as
        # a muted cash-flow cell; the month in progress with no saved
        # budget must arrive in exactly that shape to draw the same way
        conn = make_db()
        try:
            # under a closed month of ledger: the seed stays provisional
            _spend(conn, dt.date(2026, 9, 20), TODAY)
            self.assertFalse(budget.budgets_set(conn))
            y = lenses.year_summary(conn, 2026, TODAY)
            live, closed = y["cells"][9], y["cells"][8]
            self.assertEqual(live["status"], "net")
            self.assertEqual(closed["status"], "net")
            self.assertEqual(set(live), set(closed))
            self.assertIsNone(live["verdict"])
            self.assertIsNone(live["variable_budget"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
