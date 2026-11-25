"""The plan's income is decided once, on the server, and says where it
came from.

The Budget page's header and its planner used to plan on two incomes: the
header counted this month's paychecks while the planner counted the figure
typed in its box, so "excess" differed between them. Now month_status
names the source of the one income it counts, and the planner shows that
figure — letting it be typed only when it IS the typed figure. A source
that disagrees with the income would put the two back on different
numbers.
"""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 15)


class PlanIncomeSource(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _status(self):
        return budget.month_status(self.conn, TODAY)

    def test_nothing_to_plan_on_has_no_source(self):
        write_config(self.conn)
        st = self._status()
        self.assertIsNone(st["income_source"])
        self.assertFalse(st["income"])

    def test_a_typed_figure_is_the_typed_source(self):
        write_config(self.conn, budgeted_income_monthly=5000)
        st = self._status()
        self.assertEqual(st["income_source"], "typed")
        self.assertAlmostEqual(st["income"], 5000.0, places=2)

    def test_an_active_scenario_wins_over_the_typed_figure(self):
        write_config(self.conn, budgeted_income_monthly=5000,
                     income_scenarios=[{"name": "base", "take_home": 2000,
                                        "cadence": "biweekly"}],
                     active_scenario="base")
        st = self._status()
        self.assertEqual(st["income_source"], "scenario")
        self.assertAlmostEqual(st["income"],
                               budget.active_monthly_income(
                                   budget.load_config(self.conn)), places=2)

    def test_tracked_paychecks_win_and_the_typed_figure_is_not_read(self):
        write_config(self.conn, budgeted_income_monthly=9999)
        d = dt.date(2025, 6, 1)
        while d <= TODAY:
            add_txn(self.conn, d, -4000.0, "LANTERN WORKS PAYROLL",
                    primary="INCOME", account="chk")
            d = (d + dt.timedelta(days=32)).replace(day=1)
        add_bill(self.conn, "Lantern Works Payroll", 4000.0, income=True,
                 next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        st = self._status()
        self.assertEqual(st["income_source"], "paychecks")
        self.assertNotAlmostEqual(st["income"], 9999.0, places=2)
        self.assertAlmostEqual(
            st["income"],
            (st["income_received"] or 0) + (st["income_expected"] or 0),
            places=2)


if __name__ == "__main__":
    unittest.main()
