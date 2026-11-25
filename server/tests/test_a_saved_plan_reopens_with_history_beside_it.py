"""Setup run again opens the budget step on the household's saved plan,
with history's figure beside each row. The hint beside "Everything else"
is the server's (suggestions.unallocated_monthly): the non-food pool less
what each saved everything-else category took, month by month, then
suggested by the same rule as every row — so a client never works it out
from two other figures, and a sparse saved category's one big month does
not swallow the remainder."""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 15)


class SavedPlanHintTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        add_txn(self.conn, dt.date(2025, 12, 3), 50.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK", account="chk")
        for mo in range(1, 7):
            add_txn(self.conn, dt.date(2026, mo, 8), 200.0, "FUEL STOP",
                    primary="TRANSPORTATION", account="chk")
            add_txn(self.conn, dt.date(2026, mo, 9), 300.0, "HARDWARE BARN",
                    primary="HOME_IMPROVEMENT", account="chk")

    def tearDown(self):
        self.conn.close()

    def test_everything_else_hint_leaves_out_the_saved_categories(self):
        write_config(self.conn, food_monthly=400, other_monthly=900,
                     custom_buckets=[
                         {"name": "Fuel", "parent": "other", "monthly": 150,
                          "categories": ["TRANSPORTATION"], "merchants": []}])
        sug = budget.suggest_budgets(self.conn, TODAY)["suggestions"]
        self.assertEqual(sug["other_monthly"], 500.0)
        self.assertEqual(sug["buckets"]["Fuel"], 200.0)
        self.assertEqual(sug["unallocated_monthly"], 300.0)

    def test_a_sparse_saved_category_does_not_swallow_the_remainder(self):
        # one $600 month of gifts: its own figure is that month (its active
        # months only), and taking it off the pool's figure read as $0
        add_txn(self.conn, dt.date(2026, 3, 20), 600.0, "GIFT SHOP",
                primary="GIFTS", account="chk")
        write_config(self.conn, food_monthly=400, other_monthly=900,
                     custom_buckets=[
                         {"name": "Fuel", "parent": "other", "monthly": 150,
                          "categories": ["TRANSPORTATION"], "merchants": []},
                         {"name": "Gifts", "parent": "other", "monthly": 50,
                          "categories": ["GIFTS"], "merchants": []}])
        sug = budget.suggest_budgets(self.conn, TODAY)["suggestions"]
        self.assertEqual(sug["buckets"]["Gifts"], 600.0)
        self.assertEqual(sug["unallocated_monthly"], 300.0)

    def test_food_parent_categories_do_not_come_off_everything_else(self):
        write_config(self.conn, food_monthly=400, other_monthly=900,
                     custom_buckets=[
                         {"name": "Coffee", "parent": "food", "monthly": 40,
                          "categories": ["COFFEE"], "merchants": []}])
        sug = budget.suggest_budgets(self.conn, TODAY)["suggestions"]
        self.assertEqual(sug["unallocated_monthly"], sug["other_monthly"])

    def test_without_saved_categories_the_hint_is_the_whole_pool(self):
        write_config(self.conn)
        sug = budget.suggest_budgets(self.conn, TODAY)["suggestions"]
        self.assertEqual(sug["unallocated_monthly"], 500.0)


if __name__ == "__main__":
    unittest.main()
