"""The money map's Bills row opens the month's bill payments.

The variable rows (Food, Everything else, a carve-out) open the rows the
engine counted under `?bucket=`; the Bills row used to be the one label
with nothing behind it. `bucket_ledger(st, "fixed")` is the fixed bucket's
own rows — occurrence-matched payments and envelope swipes — so the Bills
row on Today, the Month lens and the Year lens drills in like the rest."""

import unittest

from oikonome.engine import budget

from .util import TODAY, add_bill, add_txn, make_db, write_config


class BillsRowLedgerTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "City Power", 100.0, frequency="MONTHLY",
                 next_due=TODAY.replace(day=3), last_seen=TODAY.replace(day=3))
        add_txn(self.conn, TODAY.replace(day=3), 100.0, "CITY POWER",
                primary="GENERAL_SERVICES")
        add_txn(self.conn, TODAY.replace(day=4), 40.0, "HARBOR GROCER",
                primary="FOOD_AND_DRINK")

    def tearDown(self):
        self.conn.close()

    def _id(self, name):
        return self.conn.execute("SELECT id FROM transactions WHERE name=%s",
                                 (name,)).fetchone()["id"]

    def test_the_bills_row_lists_the_bill_payment_and_nothing_else(self):
        st = budget.month_status(self.conn, TODAY)
        info = budget.bucket_ledger(st, "fixed")
        self.assertIsNotNone(info)
        self.assertEqual(info["label"], "Bills")
        self.assertIn(self._id("CITY POWER"), info["txn_ids"])
        self.assertNotIn(self._id("HARBOR GROCER"), info["txn_ids"])
        self.assertAlmostEqual(info["amount"], 100.0)
        # and the food row still does not list the bill
        food = budget.bucket_ledger(st, "food")
        self.assertNotIn(self._id("CITY POWER"), food["txn_ids"])

    def test_today_links_the_bills_row(self):
        from oikonome.web import report, todayview
        st = report.gather(self.conn, TODAY)
        row = next(r for r in todayview.money_map(st)["rows"]
                   if r["label"] == "Bills")
        self.assertIn("bucket=fixed", row["to"])


if __name__ == "__main__":
    unittest.main()
