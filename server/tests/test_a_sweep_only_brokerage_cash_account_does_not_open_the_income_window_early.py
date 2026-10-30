"""The Income tab's window opens where income is measured.

A brokerage's cash account (a depository account in a login that also
holds an investment account) is summed only for the deposits stamped
INCOME, and the coverage rule the Year and Month lenses use leaves it out
of the question "from when is income known". The Income tab's "All"
window and its per-month pace must follow the same rule: one that only
ever received sweeps, years before the first paycheck, would otherwise
open "All" at its first sweep and divide the income by months in which
no paycheck was ever seen.
"""

import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import TODAY, add_txn, make_db, write_config


class SweepOnlyBrokerageCashAccount(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, access_token)"
            " VALUES ('itb','test','Contoso Brokerage','tok-b')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
            " VALUES ('bcash','itb','Contoso Cash','depository','cash management',0),"
            " ('bport','itb','Contoso Portfolio','investment','brokerage',10000)")
        # sweeps into the brokerage cash account, years before any paycheck
        for y in (2021, 2022, 2023):
            add_txn(self.conn, dt.date(y, 3, 10), -400.0, "SWEEP FROM PORTFOLIO",
                    primary="TRANSFER_IN", account="bcash")
        # the checking account's paychecks: the three complete months
        # before today
        for m in (4, 5, 6):
            add_txn(self.conn, dt.date(TODAY.year, m, 5), -3000.0,
                    "NORTHWIND PAYROLL", account="chk", primary="INCOME",
                    detailed="INCOME_WAGES")
        self.first_paycheck_month = dt.date(TODAY.year, 4, 1)

    def test_all_opens_at_the_first_measured_income_month(self):
        w = reporting.income_window(self.conn, TODAY, "all")
        self.assertEqual(w["from"], self.first_paycheck_month.isoformat())

    def test_the_pace_divides_by_the_measured_months_only(self):
        w = reporting.income_window(self.conn, TODAY, "all")
        self.assertEqual(w["months"], 3)
        self.assertAlmostEqual(w["total"], 9000.0, places=2)

    def test_income_stamped_in_the_brokerage_cash_account_is_still_summed(self):
        add_txn(self.conn, dt.date(TODAY.year, 6, 20), -500.0,
                "FABRIKAM DIVIDEND", account="bcash", primary="INCOME")
        w = reporting.income_window(self.conn, TODAY, "all")
        self.assertAlmostEqual(w["total"], 9500.0, places=2)

    def test_a_household_banking_only_at_a_brokerage_still_has_a_window(self):
        self.conn.execute("DELETE FROM transactions WHERE account_id='chk'")
        add_txn(self.conn, dt.date(2024, 2, 5), -3000.0, "NORTHWIND PAYROLL",
                account="bcash", primary="INCOME", detailed="INCOME_WAGES")
        self.conn.execute("DELETE FROM accounts WHERE id IN ('chk','card')")
        w = reporting.income_window(self.conn, TODAY, "all")
        self.assertEqual(w["from"], "2021-03-01")


if __name__ == "__main__":
    unittest.main()
