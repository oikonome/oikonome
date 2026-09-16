"""A money-in row on a credit card that says "payment" IS a card payment,
whatever the aggregator called it.

Plaid can deliver a card settlement ("Payment Thank You - Web") as
LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT while the sibling card payments that
post the same day arrive correctly. Only
LOAN_PAYMENTS_CREDIT_CARD_PAYMENT is in the engine's spend-exclusion list,
so the mislabelled row reads as real money in — and the ledger draws it
green next to identical grey ones.

The account supplies the card context, which is what lets a bare payment
word be trusted here (the same reasoning as Discover's mixed
"Payments and Credits" category, test_flowmap_discover). A genuine merchant
refund on the card carries no payment word and stays a refund.
"""

import datetime as dt
import unittest

from oikonome.sync import base, flowmap

from .util import make_db

TODAY = dt.date(2026, 7, 31)


def _txn(tid, account_id, amount, name, primary, detailed):
    return base.Transaction(
        id=tid, account_id=account_id, date=TODAY, amount=amount, name=name,
        category_primary=primary, category_detailed=detailed)


class NameDetectorTests(unittest.TestCase):
    def test_payment_shaped_names(self):
        for name in ("Payment Thank You - Web", "INTERNET PAYMENT - THANK YOU",
                     "ONLINE PAYMENT, THANK YOU", "AUTOPAY 1234",
                     "DIRECTPAY FULL BALANCE", "MOBILE PMT"):
            self.assertTrue(
                flowmap.looks_like_card_payment_on_card(name), name)

    def test_refunds_are_not_payments(self):
        for name in ("AMAZON MKTPL REFUND", "STATEMENT CREDIT",
                     "CASHBACK REDEMPTION", "SAFEWAY STORE 123"):
            self.assertFalse(
                flowmap.looks_like_card_payment_on_card(name), name)


class UpsertNormalizeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _cat(self, tid):
        r = self.conn.execute(
            "SELECT category_primary, category_detailed FROM transactions "
            "WHERE id = %s", (tid,)).fetchone()
        return r["category_primary"], r["category_detailed"]

    def test_mislabelled_card_settlement_is_normalized(self):
        base.upsert_transactions(self.conn, [
            _txn("t1", "card", -2400.00, "Payment Thank You - Web",
                 "LOAN_DISBURSEMENTS", "LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT")])
        self.assertEqual(self._cat("t1"),
                         ("LOAN_PAYMENTS", flowmap.CC_PAYMENT_DETAILED))

    def test_correctly_labelled_sibling_is_left_alone(self):
        base.upsert_transactions(self.conn, [
            _txn("t2", "card", -1400.00, "INTERNET PAYMENT - THANK YOU",
                 "LOAN_PAYMENTS", flowmap.CC_PAYMENT_DETAILED)])
        self.assertEqual(self._cat("t2"),
                         ("LOAN_PAYMENTS", flowmap.CC_PAYMENT_DETAILED))

    def test_refund_on_the_card_stays_a_refund(self):
        base.upsert_transactions(self.conn, [
            _txn("t3", "card", -25.00, "AMAZON MKTPL REFUND",
                 "GENERAL_MERCHANDISE", None)])
        self.assertEqual(self._cat("t3"), ("GENERAL_MERCHANDISE", None))

    def test_purchase_on_the_card_is_untouched(self):
        """Money OUT is never a settlement, whatever it is called — a
        merchant genuinely named "... PAYMENT" must stay spend."""
        base.upsert_transactions(self.conn, [
            _txn("t4", "card", 60.00, "CITY ELECTRIC PAYMENT",
                 "RENT_AND_UTILITIES", None)])
        self.assertEqual(self._cat("t4"), ("RENT_AND_UTILITIES", None))

    def test_checking_row_is_untouched(self):
        """No card context, no bare-word trust: on checking this could be
        any merchant, which is what the narrower detector is for."""
        base.upsert_transactions(self.conn, [
            _txn("t5", "chk", -500.00, "PAYMENT THANK YOU",
                 "INCOME", None)])
        self.assertEqual(self._cat("t5"), ("INCOME", None))

    def test_user_override_is_never_clobbered(self):
        base.upsert_transactions(self.conn, [
            _txn("t6", "card", -2400.00, "Payment Thank You - Web",
                 "LOAN_DISBURSEMENTS", "LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT")])
        self.conn.execute("UPDATE transactions SET category_override = %s "
                          "WHERE id = 't6'", ("Reimbursed",))
        base.upsert_transactions(self.conn, [
            _txn("t6", "card", -2400.00, "Payment Thank You - Web",
                 "LOAN_DISBURSEMENTS", "LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT")])
        row = self.conn.execute(
            "SELECT category_override FROM transactions WHERE id='t6'"
        ).fetchone()
        self.assertEqual(row["category_override"], "Reimbursed")




class ThankYouIsNotAPaymentWordTests(unittest.TestCase):
    """looks_like_card_payment_on_card promises, in its own docstring, that a
    genuine merchant refund rides through as the credit it is. "thank you"
    cannot be a payment word: card statements print it on refunds as
    readily as on settlements, so a refund would be swallowed as an
    internal transfer, the original purchase would go un-offset, and spend
    would read high — the household told to spend less than it can."""

    def test_real_payments_still_match(self):
        for name in ("Payment Thank You - Web",
                     "INTERNET PAYMENT - THANK YOU",
                     "DIRECTPAY FULL BALANCE",
                     "PHONE PAYMENT",
                     "AUTOPAY 1234"):
            self.assertTrue(flowmap.looks_like_card_payment_on_card(name),
                            f"{name!r} is a card payment")

    def test_a_refund_that_merely_says_thank_you_is_not_a_payment(self):
        for name in ("RETURN - THANK YOU FOR SHOPPING",
                     "TARGET REFUND THANK YOU",
                     "THANK YOU"):
            self.assertFalse(flowmap.looks_like_card_payment_on_card(name),
                             f"{name!r} is a credit, not a settlement")


class CardPaymentNameTests(unittest.TestCase):
    """Card-payment NAME detection: autopay/epayment wordings are
    transfers; bill-pay to a merchant stays spend."""

    def test_autopay_names_are_payments_merchant_bills_are_not(self):
        for nm in ("CHASE CREDIT CRD AUTOPAY", "CARDMEMBER SERV WEB PYMT",
                   "AMEX EPAYMENT", "Capital One Card Payment"):
            self.assertTrue(flowmap.looks_like_card_payment(nm), nm)
        for nm in ("CITY ELECTRIC PAYMENT", "PAYMENT THANK YOU",
                   "SAFEWAY 123", "VENMO CASHOUT", "ACME PLUMBING"):
            self.assertFalse(flowmap.looks_like_card_payment(nm), nm)


if __name__ == "__main__":
    unittest.main()
