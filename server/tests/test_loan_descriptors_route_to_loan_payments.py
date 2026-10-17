"""Loan-servicing statement lines land in LOAN_PAYMENTS before any
categorizer sees them.

A student-loan or auto-loan payment is money out of the household, so it
stays in spend — but in the loan-payments bucket. Left to a categorizer
that reads only the servicer's name, a decade of loan payments is filed
under "services" or "utilities". Two failure modes are pinned here:

 * the raw line carries the words ("DES:STD LN PMT") and the merchant
   does not — an importer cleaned it to the bare servicer name, so the
   display-name screen alone never sees them;
 * a card-payment line spelled "CC PAYMENT".

The raw-line screen is loan-only: a raw "Withdrawal from …" is how some
banks spell any payment, and reading a transfer from it would hide spend.
"""

import unittest

from oikonome.engine import llm_categorize, merchant_seed

from .test_llm_categorize import add_raw_txn, cache
from .util import make_db


class FlowMatchTests(unittest.TestCase):
    def test_student_loan_line(self):
        self.assertEqual(
            merchant_seed.flow_match("NORTHWIND DES:STD LN PMT ID:CX1234"),
            ("LOAN_PAYMENTS", "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT"))
        self.assertEqual(
            merchant_seed.flow_match("Withdrawal from NORTHWIND STUDENT LOAN"),
            ("LOAN_PAYMENTS", "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT"))

    def test_other_loan_line(self):
        self.assertEqual(
            merchant_seed.flow_match("BILL PAY ACME FINANCIAL LOAN PMT 0042"),
            ("LOAN_PAYMENTS", "LOAN_PAYMENTS_OTHER_PAYMENT"))

    def test_cc_payment_is_a_card_payment(self):
        self.assertEqual(
            merchant_seed.flow_match("AB PL CC PAYMENT PAYMENT"),
            ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"))

    def test_bare_names_are_not_loans(self):
        for s in ("Student", "Northwind", "Loan Star Grill", "Student Union Cafe"):
            self.assertIsNone(merchant_seed.flow_match(s), s)
            self.assertIsNone(merchant_seed.loan_match(s), s)

    def test_raw_screen_never_reads_a_transfer(self):
        self.assertIsNone(merchant_seed.loan_match("Withdrawal from NORTHWIND"))


class FlowClassifyRawLineTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _row(self, tid):
        return self.conn.execute(
            "SELECT category_primary, category_detailed, category_source "
            "FROM transactions WHERE id=%s", (tid,)).fetchone()

    def test_loan_words_on_the_raw_line_route_the_row(self):
        add_raw_txn(self.conn, "l1", "2025-07-01", 250, "Northwind",
                    primary="GENERAL_SERVICES", raw={})
        self.conn.execute(
            "UPDATE transactions SET name=%s WHERE id='l1'",
            ("NORTHWIND DES:STD LN PMT ID:CX1234",))
        # the servicer's name alone had been filed as services by a model
        cache(self.conn, "Northwind", "GENERAL_SERVICES")
        stats = {}
        n = llm_categorize.flow_classify(self.conn, stats)
        self.assertEqual(n, 1)
        self.assertEqual(dict(self._row("l1")), {
            "category_primary": "LOAN_PAYMENTS",
            "category_detailed": "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT",
            "category_source": "flow"})
        self.assertIn("Northwind", stats["touched"])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories WHERE merchant='Northwind'"
        ).fetchone())

    def test_same_merchant_other_lines_keep_their_category(self):
        """Keyed on the raw line, not the merchant: a servicer's unrelated
        charge (a fee line without the loan words) is not swept along."""
        add_raw_txn(self.conn, "l1", "2025-07-01", 250, "Northwind",
                    primary="GENERAL_SERVICES", raw={})
        add_raw_txn(self.conn, "l2", "2025-07-02", 5, "Northwind",
                    primary="GENERAL_SERVICES", raw={})
        self.conn.execute(
            "UPDATE transactions SET name=%s WHERE id='l1'",
            ("NORTHWIND STD LN PMT",))
        llm_categorize.flow_classify(self.conn)
        self.assertEqual(self._row("l1")["category_primary"], "LOAN_PAYMENTS")
        self.assertEqual(self._row("l2")["category_primary"], "GENERAL_SERVICES")

    def test_a_raw_withdrawal_line_is_not_a_transfer(self):
        add_raw_txn(self.conn, "w1", "2025-07-01", 60, "Northwind",
                    primary="GENERAL_SERVICES", raw={})
        self.conn.execute(
            "UPDATE transactions SET name=%s WHERE id='w1'",
            ("Withdrawal from NORTHWIND",))
        self.assertEqual(llm_categorize.flow_classify(self.conn), 0)
        self.assertEqual(self._row("w1")["category_primary"], "GENERAL_SERVICES")


if __name__ == "__main__":
    unittest.main()
