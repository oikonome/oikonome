"""A spending window and its /mo pace begin where spend begins. Rows that
are never spend — 401k plan history imported back years before the bank
feed, loan rows — must not open the "All" window early or stretch its
divisor: two years of spend divided by ten years of months reads at a
fifth of the real pace. And a household whose first spend is in the month
in progress still sees that month, whatever older non-spend rows it has."""
import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import add_txn, make_db


class SpendWindowAnchorsOnSpend(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('k401','it1','Plan','investment',"
            "'401k',0) ON CONFLICT (tenant_id, id) DO NOTHING")
        self.today = dt.date(2026, 10, 9)
        # plan contributions back to 2016: transfers on an investment account
        add_txn(self.conn, dt.date(2016, 1, 15), -400.0, "PLAN CONTRIBUTION",
                account="k401", primary="TRANSFER_IN")

    def tearDown(self):
        self.conn.close()

    def test_all_opens_at_the_first_spend_row(self):
        for i in range(24):
            y, m = divmod(2024 * 12 + 9 + i, 12)     # 2024-10 .. 2026-09
            add_txn(self.conn, dt.date(y, m + 1, 10), 2000.0, "GROCER",
                    account="card")
        w = reporting.spending_window(self.conn, self.today, "all")
        self.assertEqual(w["from"], "2024-10-01")
        self.assertEqual(w["months"], 24)
        w5 = reporting.spending_window(self.conn, self.today, "5y")
        self.assertEqual(w5["months"], 24)

    def test_first_spend_this_month_shows_the_live_month(self):
        add_txn(self.conn, dt.date(2026, 10, 3), 80.0, "GROCER",
                account="card")
        w = reporting.spending_window(self.conn, self.today, "1y")
        self.assertTrue(w["live_month"])
        self.assertEqual(w["total"], 80.0)


if __name__ == "__main__":
    unittest.main()
