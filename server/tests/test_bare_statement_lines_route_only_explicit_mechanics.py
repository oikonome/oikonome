"""A row with no merchant name carries the bank's whole statement line as
its payee. The flow screen reads such a line by explicit wording only:
"WITHDRAWAL POS #123 NORTHWIND GROCERY" and "FABRIKAM WATER E-PAYMENT" are
how some banks spell an ordinary purchase or bill, and filing them as a
transfer or a card payment takes real spending out of the budget for good
(the flow guard keeps a flow row a flow row). The opposite failure is as
costly: a card issuer's electronic payment carries its card signal only
through the issuer's name and E-PAYMENT together, and a card settlement
left in spend counts every purchase on that card twice.
"""
import unittest

from oikonome.engine import llm_categorize, merchant_seed
from oikonome.sync import flowmap

from .test_llm_categorize import add_raw_txn
from .util import make_db

CARD_PAYMENT = ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")

MUST_ROUTE_AS_CARD_PAYMENT = (
    "BARCLAYCARD EPAYMENT 9021",
    "SYNCHRONY BANK E-PAYMENT",
    "SYNCB EPAYMENT 0042",
    "BARCLAYCARD US E-PAYMENT",
    "BARCLAY CARD E-PAYMENT",
    "SYNCHRONY BANK EPAY",
    "CONTOSO CREDIT CRD AUTOPAY",
    "FABRIKAM CARD WEB PAYMENT",
)
MUST_STAY_SPEND = (
    "WITHDRAWAL POS #123 NORTHWIND GROCERY",
    "FABRIKAM WATER E-PAYMENT",
    "WITHDRAWAL DEBIT CARD PURCHASE NORTHWIND OUTFITTERS",
    "POS DEBIT CARD PAYMENT NORTHWIND GROCERY",
    "VISA DEBIT CARD PAYMENT FABRIKAM HARDWARE",
)


class StatementLineReaderTests(unittest.TestCase):
    def test_issuer_payments_route_as_card_payments(self):
        for line in MUST_ROUTE_AS_CARD_PAYMENT:
            with self.subTest(line=line):
                self.assertEqual(merchant_seed.flow_match_line(line),
                                 CARD_PAYMENT)

    def test_loose_words_on_a_purchase_line_route_nothing(self):
        for line in MUST_STAY_SPEND:
            with self.subTest(line=line):
                self.assertIsNone(merchant_seed.flow_match_line(line))

    def test_explicit_transfer_and_cash_wording_routes_as_transfer(self):
        for line in ("ATM WITHDRAWAL", "CASH WITHDRAWAL 0042",
                     "WITHDRAWAL TRANSFER TO SAVINGS",
                     "ONLINE XFER TO CHECKING"):
            with self.subTest(line=line):
                self.assertEqual(merchant_seed.flow_match_line(line),
                                 ("TRANSFER_OUT", None))

    def test_a_loan_payment_line_still_reads_as_one(self):
        self.assertEqual(
            merchant_seed.flow_match_line("CONTOSO DES:STD LN PMT"),
            ("LOAN_PAYMENTS", "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT"))

    def test_a_debit_card_purchase_is_never_a_card_payment(self):
        # the importers (OFX) and the bills screen share this detector
        for line in ("POS DEBIT CARD PAYMENT NORTHWIND GROCERY",
                     "VISA DEBIT CARD PAYMENT FABRIKAM HARDWARE",
                     "CHECK CARD PAYMENT NORTHWIND"):
            with self.subTest(line=line):
                self.assertFalse(flowmap.looks_like_card_payment(line))
        self.assertTrue(flowmap.looks_like_card_payment(
            "CONTOSO CREDIT CRD AUTOPAY"))


class BareStatementLineFlowTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _bare(self, tid, amount, line, primary=None, source=None):
        add_raw_txn(self.conn, tid, "2025-07-01", amount, line,
                    primary=primary, raw={}, account="chk")
        self.conn.execute(
            "UPDATE transactions SET merchant_name=NULL, category_source=%s "
            "WHERE id=%s", (source, tid))

    def _row(self, tid):
        return self.conn.execute(
            "SELECT category_primary, category_detailed FROM transactions "
            "WHERE id=%s", (tid,)).fetchone()

    def _primary(self, tid):
        return self._row(tid)["category_primary"]

    def test_issuer_payments_on_a_bare_line_leave_spend(self):
        for i, line in enumerate(MUST_ROUTE_AS_CARD_PAYMENT):
            self._bare(f"cc{i}", 250.00, line)
        self.assertEqual(llm_categorize.flow_classify(self.conn),
                         len(MUST_ROUTE_AS_CARD_PAYMENT))
        for i, line in enumerate(MUST_ROUTE_AS_CARD_PAYMENT):
            with self.subTest(line=line):
                r = self._row(f"cc{i}")
                self.assertEqual((r["category_primary"],
                                  r["category_detailed"]), CARD_PAYMENT)

    def test_purchases_spelled_with_loose_words_stay_spend(self):
        for i, line in enumerate(MUST_STAY_SPEND):
            self._bare(f"sp{i}", 84.12, line)
        self.assertEqual(llm_categorize.flow_classify(self.conn), 0)
        for i, line in enumerate(MUST_STAY_SPEND):
            with self.subTest(line=line):
                self.assertIsNone(self._primary(f"sp{i}"))

    def test_explicit_transfers_follow_the_money(self):
        self._bare("atm", 60.00, "ATM WITHDRAWAL")
        self._bare("out", 200.00, "WITHDRAWAL TRANSFER TO SAVINGS")
        self._bare("in", -200.00, "DEPOSIT TRANSFER FROM SAVINGS")
        self.assertEqual(llm_categorize.flow_classify(self.conn), 3)
        self.assertEqual(self._primary("atm"), "TRANSFER_OUT")
        self.assertEqual(self._primary("out"), "TRANSFER_OUT")
        self.assertEqual(self._primary("in"), "TRANSFER_IN")

    def test_an_import_category_is_not_overruled_by_loose_words(self):
        add_raw_txn(self.conn, "imp", "2025-07-01", 60.00,
                    "WITHDRAWAL NORTHWIND INSURANCE",
                    primary="GENERAL_SERVICES", raw={}, account="chk")
        self.conn.execute("UPDATE transactions SET category_source='import' "
                          "WHERE id='imp'")
        self.assertEqual(llm_categorize.flow_classify(self.conn), 0)
        self.assertEqual(self._primary("imp"), "GENERAL_SERVICES")

    def test_a_named_merchant_still_reads_the_wider_rules(self):
        # the same strings as an aggregator's merchant name route as before
        add_raw_txn(self.conn, "nm", "2025-07-01", 40.00,
                    "WITHDRAWAL POS #123 NORTHWIND GROCERY", raw={},
                    account="chk")
        add_raw_txn(self.conn, "ep", "2025-07-01", 45.00,
                    "FABRIKAM WATER E-PAYMENT", raw={}, account="chk")
        self.assertEqual(llm_categorize.flow_classify(self.conn), 2)
        self.assertEqual(self._primary("nm"), "TRANSFER_OUT")
        self.assertEqual(self._primary("ep"), "LOAN_PAYMENTS")

    def test_a_named_and_a_bare_row_of_one_string_are_read_apart(self):
        line = "WITHDRAWAL POS #123 NORTHWIND GROCERY"
        add_raw_txn(self.conn, "nm", "2025-07-01", 40.00, line, raw={},
                    account="chk")
        self._bare("bare", 40.00, line)
        self.assertEqual(llm_categorize.flow_classify(self.conn), 1)
        self.assertEqual(self._primary("nm"), "TRANSFER_OUT")
        self.assertIsNone(self._primary("bare"))


if __name__ == "__main__":
    unittest.main()
