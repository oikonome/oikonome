"""The interest-and-fees tile prints the server's figure for this year.

Both clients worked "this year's cost" out as `ytd ? ytd.net : 0`. The
reply now carries `paid_ytd` — the year's interest and fees less refunds,
$0 with nothing charged — which the tile prints; a year whose refunds
passed its charges reads "refunded $12", never "paid −$12"."""

import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 17)


class CashCostsPaidThisYear(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_nothing_charged_this_year_is_zero(self):
        add_txn(self.conn, dt.date(2025, 3, 5), 30.0, "INTEREST CHARGE",
                account="card", primary="BANK_FEES")
        r = reporting.compute_cash_costs(self.conn, TODAY)
        self.assertIsNone(r["ytd"])
        self.assertEqual(r["paid_ytd"], 0.0)

    def test_this_years_cost_is_the_ytd_net(self):
        add_txn(self.conn, dt.date(2026, 3, 5), 30.0, "INTEREST CHARGE",
                account="card", primary="BANK_FEES")
        r = reporting.compute_cash_costs(self.conn, TODAY)
        self.assertEqual(r["paid_ytd"], r["ytd"]["net"])
        self.assertGreater(r["paid_ytd"], 0)


if __name__ == "__main__":
    unittest.main()
