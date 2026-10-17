"""The raw-line loan screen files a servicer's PAYMENT lines under loan
payments, and does no more than that.

* Money coming back on a line with the same words — a reversed payment, a
  student loan's disbursement — is not a payment: filed under loan
  payments it would net the proceeds against what was paid and hide the
  payments from spend.
* The servicer's name is a real merchant with lines of its own (a late
  fee). While such a line remains, the category the model gave the
  merchant is still in use; dropping it on every new payment line would
  re-ask the model, a paid call, after each one.
"""
import unittest

from oikonome.engine import llm_categorize

from .test_llm_categorize import add_raw_txn, cache
from .util import make_db


class LoanLineTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _line(self, tid, amount, name):
        add_raw_txn(self.conn, tid, "2025-07-01", amount, "Northwind",
                    primary="GENERAL_SERVICES", raw={})
        self.conn.execute("UPDATE transactions SET name=%s WHERE id=%s",
                          (name, tid))

    def _primary(self, tid):
        return self.conn.execute(
            "SELECT category_primary FROM transactions WHERE id=%s",
            (tid,)).fetchone()["category_primary"]

    def _cached(self):
        return self.conn.execute(
            "SELECT category_primary FROM merchant_categories "
            "WHERE merchant='Northwind'").fetchone()

    def test_money_back_on_a_loan_line_is_not_a_payment(self):
        self._line("rev", -250, "NORTHWIND LOAN PMT REVERSAL")
        self._line("dis", -4000, "NORTHWIND STUDENT LOAN DISBURSEMENT")
        self.assertEqual(llm_categorize.flow_classify(self.conn), 0)
        self.assertEqual(self._primary("rev"), "GENERAL_SERVICES")
        self.assertEqual(self._primary("dis"), "GENERAL_SERVICES")

    def test_a_mixed_servicer_keeps_its_cached_category(self):
        self._line("pmt", 250, "NORTHWIND STD LN PMT")
        self._line("fee", 15, "NORTHWIND LATE FEE")
        cache(self.conn, "Northwind", "GENERAL_SERVICES")
        stats = {}
        self.assertEqual(llm_categorize.flow_classify(self.conn, stats), 1)
        self.assertEqual(self._primary("pmt"), "LOAN_PAYMENTS")
        self.assertEqual(self._primary("fee"), "GENERAL_SERVICES")
        self.assertIsNotNone(self._cached())
        self.assertNotIn("Northwind", stats.get("touched", set()))

    def test_a_payments_only_servicer_still_drops_the_stale_cache(self):
        self._line("p1", 250, "NORTHWIND STD LN PMT 07")
        self._line("p2", 250, "NORTHWIND STD LN PMT 08")
        cache(self.conn, "Northwind", "GENERAL_SERVICES")
        stats = {}
        self.assertEqual(llm_categorize.flow_classify(self.conn, stats), 2)
        self.assertIsNone(self._cached())
        self.assertIn("Northwind", stats["touched"])

    def test_money_back_under_a_loan_named_merchant_is_not_a_payment(self):
        """The same words in the display merchant itself, not only on the
        raw line: a disbursement is still money in."""
        add_raw_txn(self.conn, "dis", "2025-07-01", -2500.0,
                    "NORTHWIND STUDENT LOAN", primary="GENERAL_SERVICES", raw={})
        add_raw_txn(self.conn, "pmt", "2025-07-15", 250.0,
                    "NORTHWIND STUDENT LOAN", primary="GENERAL_SERVICES", raw={})
        self.assertEqual(llm_categorize.flow_classify(self.conn), 1)
        self.assertEqual(self._primary("dis"), "GENERAL_SERVICES")
        self.assertEqual(self._primary("pmt"), "LOAN_PAYMENTS")

    def test_a_card_payment_or_transfer_under_its_merchant_keeps_both_directions(self):
        """Only the loan rule is one-way: a card payment's refund leg and an
        incoming transfer are still the bank mechanics those rules name."""
        add_raw_txn(self.conn, "cc", "2025-07-01", -50.0,
                    "CHASE CREDIT CARD PAYMENT", primary="GENERAL_SERVICES",
                    raw={})
        add_raw_txn(self.conn, "tin", "2025-07-01", -50.0,
                    "TRANSFER FROM SAVINGS", primary="GENERAL_SERVICES", raw={})
        llm_categorize.flow_classify(self.conn)
        self.assertEqual(self._primary("cc"), "LOAN_PAYMENTS")
        self.assertEqual(self._primary("tin"), "TRANSFER_IN")


if __name__ == "__main__":
    unittest.main()
