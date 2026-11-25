"""A card in credit (a refund or an overpayment left its balance below
$0) owes its pending charges only beyond that credit (specs/today-
figures.md, "Card debt"): its debt is the signed balance plus the pending
charges, never below $0, and the pending part the walk pays is what is
left of the charges once the credit has absorbed them. Figures are
invented."""

import unittest

from oikonome.engine import forecast

from .util import TODAY, add_txn, make_db, write_config


class TheCreditAbsorbsTheCharges(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0)

    def tearDown(self):
        self.conn.close()

    def _card(self, balance, pending):
        self.conn.execute("UPDATE accounts SET balance_current=%s "
                          "WHERE id='card'", (balance,))
        if pending:
            add_txn(self.conn, TODAY, pending, "CORNER MARKET", pending=1)
        [c] = [c for c in forecast.card_balances(self.conn, [],
                                                with_empty=True)
               if c["id"] == "card"]
        return c

    def test_a_credit_larger_than_the_charges_owes_nothing(self):
        c = self._card(-430.0, 70.0)
        self.assertEqual((c["posted"], c["pending"], c["debt"]),
                         (0.0, 0.0, 0.0))
        self.assertEqual(forecast.card_debt_total(self.conn, []), 0.0)

    def test_a_credit_smaller_than_the_charges_owes_the_rest(self):
        c = self._card(-30.0, 110.0)
        self.assertEqual((c["posted"], c["pending"], c["debt"]),
                         (0.0, 80.0, 80.0))

    def test_a_card_owing_keeps_posted_plus_pending(self):
        c = self._card(240.0, 60.0)
        self.assertEqual((c["posted"], c["pending"], c["debt"]),
                         (240.0, 60.0, 300.0))


if __name__ == "__main__":
    unittest.main()
