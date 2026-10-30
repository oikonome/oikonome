"""A row with no merchant name is screened for the categorizer the way the
flow pass reads it: by the bank's whole statement line.

Some banks spell an ordinary purchase "WITHDRAWAL POS #123 <store>" and a
utility's online bill "<biller> E-PAYMENT". The flow pass leaves those in
spend, so they must reach the categorizer; screened with the looser
merchant rules they were dropped as bank mechanics there too, and the
purchase was never categorized by anything. A line with explicit
card-payment wording is still kept away from it.
"""

import unittest

from oikonome.engine import llm_categorize

from .util import add_txn, make_db


def _bare(conn, amount, line):
    """An uncategorized row whose source sent no merchant name."""
    tid = add_txn(conn, "2025-07-01", amount, line, primary=None)
    conn.execute("UPDATE transactions SET merchant_name = NULL WHERE id = %s",
                 (tid,))
    return tid


def _pending_lines(conn):
    return {m["raw_name"] for m in llm_categorize.pending_merchants(conn)}


class BareLineCandidateTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_a_bare_withdrawal_purchase_is_a_candidate(self):
        _bare(self.conn, 80.0, "WITHDRAWAL POS #123 NORTHWIND GROCERY")
        llm_categorize.flow_classify(self.conn)
        self.assertIn("WITHDRAWAL POS #123 NORTHWIND GROCERY",
                      _pending_lines(self.conn))

    def test_a_billers_e_payment_is_a_candidate(self):
        _bare(self.conn, 60.0, "FABRIKAM WATER E-PAYMENT")
        llm_categorize.flow_classify(self.conn)
        self.assertIn("FABRIKAM WATER E-PAYMENT", _pending_lines(self.conn))

    def test_an_explicit_card_payment_line_is_not(self):
        _bare(self.conn, 900.0, "CONTOSO CREDIT CARD PAYMENT")
        self.assertNotIn("CONTOSO CREDIT CARD PAYMENT",
                         _pending_lines(self.conn))

    def test_a_named_row_keeps_the_wider_screen(self):
        # the merchant name is the source's own, not a bank line: a bare
        # WITHDRAWAL there still names a movement of money
        add_txn(self.conn, "2025-07-01", 50.0, "CASH WITHDRAWAL",
                primary=None, merchant="Teller Withdrawal")
        self.assertEqual(llm_categorize.pending_merchants(self.conn), [])


if __name__ == "__main__":
    unittest.main()
