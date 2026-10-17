"""The net-worth hero change never counts a mortgage's principal without
the cash that paid it.

The financial trend rebuilds every month without a recorded month-end
from today's cash: a payment's cash leaving is invisible there. The
property layer walks the loan back through its rows, so the month before
a payment carries more debt. Added together over a rebuilt month, the
principal reads as a gain with no outflow beside it: a payment month
that lost money shows a rise. Wherever the financial point is a rebuild,
the loan is held where the financial layer holds cash, so both layers are
equally blind to the payment; where it is recorded, the payment reads as
the interest it cost.
"""
import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import add_txn, make_db

TODAY = dt.date(2026, 10, 10)


class RebuiltMonthPrincipalTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()          # checking 5,000, card 250
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('inv','it1','Brokerage','investment',NULL,1000),"
            "('mtg','it1','Home Loan','loan','mortgage',200000)")
        self.conn.execute(
            "INSERT INTO holdings (account_id, symbol, quantity, price) "
            "VALUES ('inv','VTI',10,100)")
        # an investment ledger older than the window, prices flat: the
        # trend has a point per month and the market moved nothing
        add_txn(self.conn, "2026-07-02", -100.0, "DIVIDEND",
                account="inv", primary="INCOME")
        self._payment("2026-10-01")

    def tearDown(self):
        self.conn.close()

    def _payment(self, day):
        # a full statement: the payment and the lender's interest, so the
        # loan fell by the 500 of principal
        add_txn(self.conn, day, -2000.0, "MORTGAGE PAYMENT", account="mtg",
                primary="LOAN_PAYMENTS")
        add_txn(self.conn, day, 1500.0, "INTEREST CHARGED", account="mtg",
                primary="LOAN_PAYMENTS")

    def _change(self):
        ft = reporting.compute_networth(self.conn, today=TODAY)["full_trend"]
        self.assertGreaterEqual(len(ft), 2)
        return round(ft[-1][1] - ft[-2][1], 2)

    def test_a_household_with_no_recorded_month_shows_no_principal_gain(self):
        # the first-month household: last month is a rebuild with today's
        # cash, so the payment's 2,000 never left it; the loan's 500 must
        # not arrive alone as a +500 month
        self.assertEqual(self._change(), 0.0)

    def test_a_recorded_last_month_reads_the_payment_as_its_cost(self):
        # last month-end recorded with the 2,000 still in checking: the
        # month lost the payment and gained its principal back
        self.conn.execute(
            "INSERT INTO networth_recorded (month, total) "
            "VALUES ('2026-09', 7750.0)")
        self.assertEqual(self._change(), -1500.0)

    def test_months_after_the_last_record_hold_the_loan_with_the_cash(self):
        # recorded in August, September rebuilt from August's level: the
        # September payment's cash is still in that point, so its
        # principal must be too, and the change spans both payments' cost
        self._payment("2026-09-01")
        self.conn.execute(
            "INSERT INTO networth_recorded (month, total) "
            "VALUES ('2026-08', 9750.0)")
        self.assertEqual(self._change(), -4000.0 + 1000.0)


if __name__ == "__main__":
    unittest.main()
