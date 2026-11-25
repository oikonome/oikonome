"""A month from before the household had a budget is not judged, and
today's bills are not written into it.

The Month view judged such a month against today's budget and laid
today's bill schedule over it: a month years before a bill existed read
"over budget" because bills that came later used up a budget that came
later, and listed them as due and not posted. The Year grid, for the same
month, showed only what came in and went out. One function now answers
for both (`lenses.month_standing`): a closed month is judged when its
budget snapshot exists, and otherwise shows in, out and the difference
with no bill schedule at all.
"""
import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import lenses

from .util import TODAY, add_bill, add_txn, make_db, write_config

OLD = (TODAY.year - 1, 3)            # a closed month, long before the bill


class AMonthFromBeforeTheBudgetTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        # a bill with a payment already seen: its schedule has no first
        # date to stop at, so projected backward it lands in every month
        add_bill(self.conn, "Harbor Loan", 900.0,
                 next_due=TODAY.replace(day=20).isoformat(),
                 last_seen=(TODAY.replace(day=20)
                            - dt.timedelta(days=30)).isoformat())
        y, m = OLD
        add_txn(self.conn, dt.date(y, m, 4), 60.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(y, m, 18), 40.0, "BOOK NOOK")

    def _month(self, y=OLD[0], m=OLD[1]):
        return lenses.month_summary(self.conn, y, m, today=TODAY)

    def test_the_month_gets_no_verdict(self):
        d = self._month()
        self.assertIsNone(d["verdict"])
        self.assertFalse(d["budget_set"])
        self.assertEqual(d["standing"], "before_budget")
        self.assertEqual(d["standing_label"], "no budget then")
        self.assertIn("before you had a budget", d["standing_text"])
        self.assertIsNone(d["projection"])

    def test_none_of_todays_bills_are_written_into_it(self):
        b = self._month()["bills"]
        self.assertEqual((b["planned"], b["awaiting"], b["posted"]),
                         (0.0, 0.0, 0.0))
        self.assertEqual((b["overdue"], b["paid"], b["envelopes"]),
                         ([], [], []))

    def test_it_shows_what_went_out_and_no_plan(self):
        d = self._month()
        self.assertEqual(d["spend_total"], 100.0)
        self.assertEqual(d["variable_actual"], 100.0)
        self.assertEqual(d["plan_rows"], [])
        self.assertIsNone(d["income"]["budgeted"])
        self.assertIsNone(d["plan_surplus"])
        self.assertIsNone(d["variable_scale"])
        self.assertEqual(sum(a for _c, a, _k in d["by_category"]), 100.0)

    def test_the_month_and_the_year_agree_on_every_month(self):
        # one closed month the household did budget for
        y, m = OLD
        self.conn.execute(
            "INSERT INTO budget_snapshots (year, month, config) "
            "SELECT %s, %s, config FROM tenant_settings", (y, m + 1))
        add_txn(self.conn, dt.date(y, m + 1, 9), 25.0, "BOOK NOOK")
        cells = {c["m"]: c for c in lenses.year_summary(
            self.conn, y, today=TODAY)["cells"]}
        for month in range(1, 13):
            d = self._month(y, month)
            self.assertEqual(d["verdict"] is None,
                             cells[month]["status"] == "net", month)
        self.assertEqual(self._month(y, m + 1)["standing"], "judged")
        self.assertEqual(cells[m + 1]["status"], "final")

    def test_a_snapshot_that_cannot_be_read_is_no_budget(self):
        y, m = OLD
        self.conn.execute(
            "INSERT INTO budget_snapshots (year, month, config) "
            "VALUES (%s, %s, '{\"food_monthly\": \"lots\"}')", (y, m))
        self.assertEqual(self._month()["standing"], "before_budget")
        cells = lenses.year_summary(self.conn, y, today=TODAY)["cells"]
        self.assertEqual(cells[m - 1]["status"], "net")

    def test_the_month_in_progress_is_judged_by_the_saved_budget(self):
        d = self._month(TODAY.year, TODAY.month)
        self.assertEqual(d["standing"], "judged")
        self.assertIsNotNone(d["verdict"])
        self.assertIsNone(d["standing_text"])
        # and its bills are its own
        self.assertEqual(d["bills"]["planned"], 900.0)

    def test_a_household_with_no_saved_budget_reads_as_before(self):
        self.conn.execute("UPDATE tenant_settings SET config = "
                          "config - 'food_monthly' - 'other_monthly'")
        d = self._month(TODAY.year, TODAY.month)
        self.assertEqual(d["standing"], "no_budget")
        self.assertEqual(d["standing_label"], "no budget set")
        self.assertIn("Budget page", d["standing_text"])
        self.assertIsNone(d["verdict"])
        self.assertTrue(budget.budgets_set(self.conn) is False)


if __name__ == "__main__":
    unittest.main()
