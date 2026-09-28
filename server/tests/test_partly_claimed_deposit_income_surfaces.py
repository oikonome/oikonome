"""Every income surface counts a partly claimed deposit at its unclaimed
part.

A deposit partial-linked to a charge repays that charge with part of it
and is income for the rest. The charge's spend is already netted, so an
income figure that counted the deposit whole would count the repayment
twice, and one that dropped it (because it is stamped a transfer) would
lose the part that really was income. The Income tab, the Cash Flow
picture and the interest-earned figure each run their own query; each
must land on the same net.
"""
import unittest

from oikonome.engine import reporting

from .util import TODAY, add_txn, make_db, write_config


class PartlyClaimedDepositIncomeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        c = self.conn
        self.pay = add_txn(c, TODAY, -1000.0, "EMPLOYER DIRECT DEP",
                           account="chk", primary="INCOME",
                           txn_id="dep-pay")
        self.interest = add_txn(c, TODAY, -100.0, "SAVINGS INTEREST PAID",
                                account="chk", primary="INCOME",
                                txn_id="dep-interest")
        add_txn(c, TODAY, 300.0, "CONFERENCE FEE", txn_id="exp-fee")
        add_txn(c, TODAY, 40.0, "BANK SUPPLIES", txn_id="exp-supplies")
        c.execute("INSERT INTO reimbursements (expense_id, reimburse_id, "
                  "partial, amount) VALUES ('exp-fee','dep-pay',1,300), "
                  "('exp-supplies','dep-interest',1,40)")

    def tearDown(self):
        self.conn.close()

    def _stamp_transfer(self):
        # a stamp that predates the partial-link rule, or came back from
        # an archive: the unclaimed part is still income
        self.conn.execute(
            "UPDATE transactions SET category_override='TRANSFER_IN' "
            "WHERE id IN ('dep-pay','dep-interest')")

    def _check(self):
        w = reporting.income_window(self.conn, TODAY, "1y")
        self.assertAlmostEqual(w["total"], 760.0, places=2)
        self.assertAlmostEqual(sum(a for _, a in w["by_month"]), 760.0,
                               places=2)
        self.assertAlmostEqual(sum(s[1] for s in w["sources"]), 760.0,
                               places=2)
        f = reporting.flow_breakdown(self.conn, TODAY, "1y")
        self.assertAlmostEqual(f["in"]["total"], 760.0, places=2)
        self.assertAlmostEqual(f["in"]["interest"], 60.0, places=2)
        costs = reporting.compute_cash_costs(self.conn, today=TODAY)
        self.assertAlmostEqual(costs["earned_ytd"], 60.0, places=2)

    def test_income_is_the_unclaimed_part(self):
        self._check()

    def test_a_transfer_stamp_does_not_drop_the_unclaimed_part(self):
        self._stamp_transfer()
        self._check()


if __name__ == "__main__":
    unittest.main()
