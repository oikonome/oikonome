"""The Why's recovery line: OVER BUDGET must come with a way back — the
per-day cap per bucket that still ends the month inside that bucket's own
budget — and a bucket already through its budget gets $0 (no-spend days),
never a rounded-up allowance it cannot honour. Any other verdict carries no
recovery line at all.
"""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import add_txn, make_db, write_config


class OverBudgetRecoveryLine(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_over_budget_names_a_daily_cap_per_bucket(self):
        write_config(self.conn, food_monthly=930, other_monthly=930,
                     dynamic_variable_budget=False)
        today = dt.date(2026, 8, 16)             # 16 days left incl. today
        # food well over pace but with budget left; other barely touched
        add_txn(self.conn, today, 700, "GROCER A", primary="FOOD_AND_DRINK")
        add_txn(self.conn, today, 620, "SHOP B",
                primary="GENERAL_MERCHANDISE")
        st = budget.month_status(self.conn, today)
        self.assertEqual(st["verdict"], "OVER BUDGET")
        rec = st["why"]["recovery"]
        self.assertIsNotNone(rec)
        self.assertIn("last 16 days", rec)
        # floor((930-700)/16) = 14 ; floor((930-620)/16) = 19
        self.assertIn("$14/day on Food", rec)
        self.assertIn("$19/day on Everything else", rec)

    def test_spent_out_bucket_gets_zero_not_a_rounded_allowance(self):
        write_config(self.conn, food_monthly=500, other_monthly=900,
                     dynamic_variable_budget=False)
        today = dt.date(2026, 8, 16)
        add_txn(self.conn, today, 640, "GROCER A",     # food blown entirely
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, today, 550, "SHOP B",       # other over pace too
                primary="GENERAL_MERCHANDISE")
        st = budget.month_status(self.conn, today)
        self.assertEqual(st["verdict"], "OVER BUDGET")
        rec = st["why"]["recovery"]
        self.assertIn("Food", rec)
        self.assertIn("$0 day", rec)
        self.assertNotIn("/day on Food", rec)
        # the bucket with budget left still gets its cap:
        # floor((900-550)/16) = 21
        self.assertIn("$21/day on Everything else", rec)

    def test_zero_budget_bucket_with_spend_counts_as_spent_out(self):
        """A $0-budget custom bucket carrying real spend is valid config —
        and when that spend is what pushed the month OVER, the headline
        names the bucket, so the recovery sentence must speak to it too.
        Its honest cap is $0/day (every remaining day is a no-spend day
        there), never silence."""
        write_config(self.conn, food_monthly=900, other_monthly=900,
                     dynamic_variable_budget=False,
                     custom_buckets=[{"name": "Vice", "parent": "other",
                                      "monthly": 0,
                                      "merchants": ["CASINO"]}])
        today = dt.date(2026, 8, 16)
        add_txn(self.conn, today, 1200, "CASINO ROYALE",
                primary="GENERAL_MERCHANDISE")
        st = budget.month_status(self.conn, today)
        self.assertEqual(st["verdict"], "OVER BUDGET")
        self.assertIn("Vice", st["why"]["headline"])
        rec = st["why"]["recovery"]
        self.assertIsNotNone(rec)
        self.assertIn("Vice", rec)
        self.assertIn("$0 day", rec)
        self.assertNotIn("/day on Vice", rec)

    def test_no_recovery_line_unless_over(self):
        write_config(self.conn, food_monthly=900, other_monthly=900,
                     dynamic_variable_budget=False)
        today = dt.date(2026, 8, 16)
        add_txn(self.conn, today, 40, "GROCER A", primary="FOOD_AND_DRINK")
        st = budget.month_status(self.conn, today)
        self.assertNotEqual(st["verdict"], "OVER BUDGET")
        self.assertIsNone(st["why"]["recovery"])


if __name__ == "__main__":
    unittest.main()
