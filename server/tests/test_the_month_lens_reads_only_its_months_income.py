"""The Month lens reads the income of the month it shows, not of every month.

Its hero states the month's bank deposits. Grouping the whole ledger's
income by month and keeping one row made every Month-lens view (and every
step back a month) pay for every bank row the household ever had, so the
cost grew with the ledger's age. The figure must be the same as the
grouped one; only the read is bounded to the month.
"""
import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import reporting
from oikonome.web import lenses

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 15)
BASE = 1000.0 + 0.37


class MonthLensIncome(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for m in range(3, 8):
            amt = BASE + 10 * m
            add_txn(self.conn, dt.date(2026, m, 1), -amt, "ACME PAYROLL",
                    account="chk", primary="INCOME")
            add_txn(self.conn, dt.date(2026, m, 15), -amt - 4, "ACME PAYROLL",
                    account="chk", primary="INCOME")
            add_txn(self.conn, dt.date(2026, m, 9), 47.0, "CORNER GOODS")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_the_hero_states_the_months_deposits(self):
        grouped = {r["p"]: r["amt"]
                   for r in reporting._income_by(self.conn, "month")}
        for m in (5, 6, 7):
            r = lenses.month_summary(self.conn, 2026, m, TODAY)
            self.assertAlmostEqual(r["income"]["actual"],
                                   grouped[f"2026-{m:02d}"], places=2)

    def test_the_month_is_read_without_the_whole_ledgers_income(self):
        with mock.patch.object(reporting, "_income_by",
                               side_effect=AssertionError(
                                   "whole-ledger income read")):
            r = lenses.month_summary(self.conn, 2026, 6, TODAY)
        self.assertAlmostEqual(r["income"]["actual"],
                               2 * (BASE + 60) + 4, places=2)


if __name__ == "__main__":
    unittest.main()
