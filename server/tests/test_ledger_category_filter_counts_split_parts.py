"""The ledger's category filter agrees with the category totals on a split.

Once a charge is split, its parts ARE the row for every per-category reader:
Spending counts each part under its own category and nothing under the
row's original one. The ledger filter is the door those totals open, so it
must find the same rows and sum the same money. It used to match the row's
own category OR any part, and sum the whole charge: a $300 row with primary
FOOD split GENERAL_MERCHANDISE $100 + HOME $200 was still listed under FOOD
for $300, and under GENERAL_MERCHANDISE for $300 — the one charge adding up
to $900 across three filters.
"""

import unittest

from oikonome.engine import reporting, splits
from oikonome.web import data

from .util import TODAY, add_txn, make_db, write_config

FOOD, OTHER, HOME = "FOOD_AND_DRINK", "GENERAL_MERCHANDISE", "HOME_IMPROVEMENT"


class LedgerSplitFilterTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        mid = TODAY.replace(day=5)
        self.big = add_txn(self.conn, mid, 300.0, "HARVEST MARKET",
                           primary=FOOD, account="chk")
        self.plain = add_txn(self.conn, mid, 50.0, "RIVERTON PANTRY", primary=FOOD,
                             account="chk")
        splits.set_split(self.conn, self.big, [
            {"category": OTHER, "amount": 100},
            {"category": HOME, "amount": 200}])

    def _filter(self, category):
        rows, total, amount_sum, _amz, spend, _hits = data.search_transactions(
            self.conn, "", category=category)
        return {r["id"] for r in rows}, total, amount_sum, spend

    def test_the_original_category_no_longer_lists_the_split_row(self):
        ids, total, amount_sum, spend = self._filter(FOOD)
        self.assertEqual(ids, {self.plain})
        self.assertEqual(total, 1)
        self.assertAlmostEqual(amount_sum, 50.0)
        self.assertAlmostEqual(spend["sum"], 50.0)

    def test_a_part_s_category_counts_only_that_part(self):
        ids, total, amount_sum, spend = self._filter(OTHER)
        self.assertEqual(ids, {self.big})
        self.assertAlmostEqual(amount_sum, 100.0)
        self.assertAlmostEqual(spend["sum"], 100.0)
        ids, _, amount_sum, spend = self._filter(HOME)
        self.assertEqual(ids, {self.big})
        self.assertAlmostEqual(amount_sum, 200.0)
        self.assertAlmostEqual(spend["sum"], 200.0)

    def test_the_filters_add_up_to_the_spending_totals(self):
        tot = dict(reporting.compute_spending(self.conn, TODAY)["category_totals"])
        for cat in (FOOD, OTHER, HOME):
            _, _, _, spend = self._filter(cat)
            self.assertAlmostEqual(spend["sum"],
                                   tot.get(cat.replace("_", " "), 0.0), msg=cat)

    def test_an_uncategorised_row_that_was_split_is_not_uncategorised(self):
        bare = add_txn(self.conn, TODAY.replace(day=6), 40.0, "CORNER SHOP",
                       primary=None, account="chk")
        splits.set_split(self.conn, bare, [
            {"category": OTHER, "amount": 10}, {"category": HOME, "amount": 30}])
        ids, *_ = self._filter(data.UNCATEGORIZED)
        self.assertNotIn(bare, ids)

    def test_a_category_used_only_by_a_part_is_offered_as_a_filter(self):
        splits.set_split(self.conn, self.big, [
            {"category": OTHER, "amount": 100},
            {"category": "Garden", "amount": 200}])
        self.assertIn("Garden", data.all_categories(self.conn))


if __name__ == "__main__":
    unittest.main()
