"""Money coming back counts as income only when what it undid was spending.

A returned card autopay and a transfer back from a broker are the
household's own money returning: neither undid spending, so neither is
money earned. A bounced utility or mortgage payment undid a payment that
counted as spending, so its return stays income and the month's net is
what it would be had neither row posted. A deposit tagged as wages, or
whose description names payroll, is pay whoever sends it; a shop's refund
stays income as before.
"""

import unittest

from oikonome.engine import reporting

from .util import TODAY, add_txn, make_db, write_config


class MoneyBack(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.day = TODAY.replace(day=3)
        # the month is measured: an ordinary paycheck landed
        add_txn(self.conn, TODAY.replace(day=1), -2000.0, "Proseware Payroll",
                primary="INCOME", detailed="INCOME_WAGES", account="chk")

    def _in(self):
        return reporting.month_flows(self.conn, TODAY.year, TODAY.month)["in"]

    def _dep(self, amount, name, primary, detailed=None):
        add_txn(self.conn, self.day, -amount, name, primary=primary,
                detailed=detailed, account="chk")

    def test_a_returned_card_autopay_is_not_income(self):
        self._dep(310.0, "Card Autopay Returned", "LOAN_PAYMENTS",
                  "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        self.assertAlmostEqual(self._in(), 2000.0, places=2)

    def test_a_bounced_bill_payment_leaves_the_months_net_unchanged(self):
        f0 = reporting.month_flows(self.conn, TODAY.year, TODAY.month)
        # a water bill paid, then returned by the bank
        add_txn(self.conn, self.day, 88.0, "Northwind Water",
                primary="RENT_AND_UTILITIES", account="chk")
        self._dep(88.0, "ACH Return Northwind Water", "RENT_AND_UTILITIES")
        # a mortgage payment made, then reversed
        add_txn(self.conn, self.day, 1400.0, "Mortgage Payment",
                primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_MORTGAGE_PAYMENT", account="chk")
        self._dep(1400.0, "Mortgage Payment Reversal", "LOAN_PAYMENTS",
                  "LOAN_PAYMENTS_MORTGAGE_PAYMENT")
        f = reporting.month_flows(self.conn, TODAY.year, TODAY.month)
        self.assertAlmostEqual(f["net"], f0["net"], places=2)

    def test_money_back_from_a_brokerage_is_not_income(self):
        self._dep(5000.0, "Contoso Brokerage Moneylink", "INCOME")
        self._dep(750.0, "ETRADE ACH Credit", "INCOME")
        self._dep(120.0, "Webull Withdrawal", "INCOME")
        self.assertAlmostEqual(self._in(), 2000.0, places=2)

    def test_pay_from_a_brokers_namesake_is_income_but_its_money_back_is_not(self):
        self._dep(1700.0, "Merrill Lynch Payroll", "INCOME")
        self._dep(640.0, "Merrill Lynch Transfer", "INCOME")
        self.assertAlmostEqual(self._in(), 3700.0, places=2)

    def test_a_shops_refund_is_income(self):
        self._dep(25.0, "Refund Fabrikam Store", "GENERAL_MERCHANDISE")
        self._dep(19.0, "Fabrikam Store Returned Item", "GENERAL_MERCHANDISE")
        self._dep(12.0, "Purchase Reversal Fabrikam Store",
                  "GENERAL_MERCHANDISE")
        self.assertAlmostEqual(self._in(), 2056.0, places=2)

    def test_the_income_tab_reads_the_same_definition(self):
        self._dep(310.0, "Card Autopay Returned", "LOAN_PAYMENTS",
                  "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        w = reporting.income_window(self.conn, TODAY, "cur")
        self.assertAlmostEqual(w["total"], 2000.0, places=2)


if __name__ == "__main__":
    unittest.main()
