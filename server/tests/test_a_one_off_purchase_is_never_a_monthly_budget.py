"""The budget wizard's suggested categories: a category is offered as a
monthly budget of its own only when it had spending in at least half of
the finished months in the window (budget.steady_category). A single
purchase, or two in one stretch, stays inside Everything else — offered
as "$600 a month" it plans money that will not be spent again."""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 15)          # window: Jan–Jun finished, July partial


class OneOffPurchaseTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # the ledger starts before the window, so all six months are whole
        add_txn(self.conn, dt.date(2025, 12, 3), 60.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK", account="chk")
        for mo in range(1, 7):
            add_txn(self.conn, dt.date(2026, mo, 9), 300.0, "CORNER GROCER",
                    primary="FOOD_AND_DRINK", account="chk")

    def tearDown(self):
        self.conn.close()

    def _names(self):
        return {c["category"]: c for c in
                budget.suggest_budgets(self.conn, TODAY)["bucket_candidates"]}

    def test_a_single_purchase_is_not_offered(self):
        add_txn(self.conn, dt.date(2026, 3, 20), 612.37, "TYRE DEPOT",
                primary="GENERAL_SERVICES", account="chk")
        self.assertNotIn("GENERAL_SERVICES", self._names())

    def test_two_months_of_six_is_not_offered(self):
        add_txn(self.conn, dt.date(2026, 2, 4), 540.10, "DESK WAREHOUSE",
                primary="HOME_IMPROVEMENT", account="chk")
        add_txn(self.conn, dt.date(2026, 3, 4), 122.50, "DESK WAREHOUSE",
                primary="HOME_IMPROVEMENT", account="chk")
        self.assertNotIn("HOME_IMPROVEMENT", self._names())

    def test_half_the_finished_months_is_offered(self):
        for mo in (1, 3, 5):
            add_txn(self.conn, dt.date(2026, mo, 12), 160.0, "FUEL STOP",
                    primary="TRANSPORTATION", account="chk")
        got = self._names()
        self.assertIn("TRANSPORTATION", got)
        self.assertEqual(got["TRANSPORTATION"]["monthly"], 160.0)

    def test_the_month_in_progress_does_not_count_as_a_finished_month(self):
        # one purchase this month and one in a finished month: one of six
        add_txn(self.conn, dt.date(2026, 7, 2), 400.0, "GADGET SHOP",
                primary="ENTERTAINMENT", account="chk")
        add_txn(self.conn, dt.date(2026, 6, 2), 400.0, "GADGET SHOP",
                primary="ENTERTAINMENT", account="chk")
        self.assertNotIn("ENTERTAINMENT", self._names())

    def test_the_rule_reads_finished_months_only(self):
        complete = {"2026-01": True, "2026-02": True, "2026-03": False}
        self.assertTrue(budget.steady_category(
            {"2026-01": 10.0, "2026-02": 0.0, "2026-03": 0.0}, complete))
        self.assertFalse(budget.steady_category(
            {"2026-01": 0.0, "2026-02": 0.0, "2026-03": 90.0}, complete))
        # nothing finished yet: no way to tell a habit from a one-off
        self.assertTrue(budget.steady_category({"2026-07": 50.0},
                                               {"2026-07": False}))


if __name__ == "__main__":
    unittest.main()
