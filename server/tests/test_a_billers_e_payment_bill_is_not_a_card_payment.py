"""A recurring bill whose payee is a biller's bare "<biller> E-PAYMENT" line
stays in upcoming bills while card debt exists.

Hand-tracked card-payment bills leave the plan when live card debt exists,
because the card machinery already pays that debt. The payee of a bill is
often the bank's bare statement line, and read with the looser merchant
rules any E-PAYMENT looked like a card payment, so a utility bill vanished
from the plan. The line reading names a card payment only on explicit
wording or an issuer's own E-PAYMENT.
"""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import TODAY, add_bill, make_db, write_config


class EPaymentBillTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)       # the fixture card carries live debt

    def tearDown(self):
        self.conn.close()

    def _plan_with(self, payee):
        add_bill(self.conn, payee, 70.0,
                 next_due=TODAY + dt.timedelta(days=3),
                 last_seen=TODAY - dt.timedelta(days=27))
        return budget.month_status(self.conn, TODAY)["avg_bills"]

    def test_a_billers_e_payment_is_not_a_card_payment(self):
        self.assertFalse(budget._card_pay_bill(
            {"payee": "FABRIKAM WATER E-PAYMENT"}))

    def test_a_billers_e_payment_bill_stays_in_the_plan(self):
        self.assertAlmostEqual(self._plan_with("FABRIKAM WATER E-PAYMENT"),
                               70.0, places=2)

    def test_an_issuers_e_payment_is_still_a_card_payment(self):
        self.assertTrue(budget._card_pay_bill(
            {"payee": "SYNCHRONY BANK E-PAYMENT"}))
        self.assertAlmostEqual(self._plan_with("SYNCHRONY BANK E-PAYMENT"),
                               0.0, places=2)

    def test_explicit_card_wording_and_autopay_are_card_payments(self):
        self.assertTrue(budget._card_pay_bill(
            {"payee": "CONTOSO CREDIT CARD PAYMENT"}))
        # the importers' detector still catches what the line reading
        # leaves to it
        self.assertTrue(budget._card_pay_bill(
            {"payee": "SYNCHRONY CARD AUTOPAY"}))


if __name__ == "__main__":
    unittest.main()
