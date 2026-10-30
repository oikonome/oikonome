"""The ledger's month header states the month's income as every other
money surface does, and says what the plan still expects.

"in" and "net" are the ledger's own month (reporting.month_flows): income
counted by the date it posted, the definition the Month lens hero, the
Year cells, Cash Flow and the monthly email share. A paycheck due on a
weekend 1st that posts on the Friday before belongs to that earlier month
on every one of those pages, so the header must say the same, or the same
check reads in two months. "expected" is the plan's still-due half for the
current month: the checks due before month end that have not landed.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import budget, reporting
from oikonome.web import api

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 8, 15)           # August 1 2026 is a Saturday


class LedgerHeaderAgreesWithTheLedger(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Acme Payroll", 3000.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 9, 1), income=True,
                 last_seen=dt.date(2026, 7, 31))
        add_bill(self.conn, "Pension Fund", 1000.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 8, 20), income=True,
                 last_seen=dt.date(2026, 7, 20))
        # July's own check, then August's paid early on Friday the 31st
        add_txn(self.conn, dt.date(2026, 7, 1), -3000.0, "ACME PAYROLL",
                primary="INCOME", account="chk", merchant="Acme Payroll")
        add_txn(self.conn, dt.date(2026, 7, 20), -1000.0, "PENSION FUND",
                primary="INCOME", account="chk", merchant="Pension Fund")
        add_txn(self.conn, dt.date(2026, 7, 31), -3000.0, "ACME PAYROLL",
                primary="INCOME", account="chk", merchant="Acme Payroll")
        add_txn(self.conn, dt.date(2026, 8, 3), 50.0, "GROCER",
                account="card")

    def tearDown(self):
        self.conn.close()

    def _month(self, m):
        with mock.patch.object(api, "_today", return_value=TODAY):
            return api._transactions_payload(self.conn, y=2026, m=m)

    def test_in_and_net_are_the_ledgers_month(self):
        for m in (7, 8):
            out = self._month(m)
            flows = reporting.month_flows(self.conn, 2026, m)
            self.assertEqual(out["in_sum"], flows["in"], m)
            self.assertEqual(out["net_sum"], flows["net"], m)
        # the weekend-shifted check sits in July, where it posted
        self.assertEqual(self._month(7)["in_sum"], 7000.0)

    def test_expected_is_the_plans_still_due_for_this_month_only(self):
        out = self._month(8)
        self.assertEqual(out["in_expected"], budget.expected_income_remaining(
            self.conn, budget.load_config(self.conn), TODAY))
        self.assertGreater(out["in_expected"], 0)
        self.assertIsNone(self._month(7)["in_expected"])


if __name__ == "__main__":
    unittest.main()
