"""The first-run budget seed counts as spend every row the live verdict
counts as spend.

The seed leaves bank mechanics out by their TEXT only while a row is still
uncategorized and the text names a card payment or a transfer — the rows
the categorize pass is about to route out of spend. A row the feed already
categorized (a debit purchase spelled "POS WITHDRAWAL <store>") and any
loan payment other than a card payment are spend in month_status, so the
seed must count them too; otherwise the budget is seeded below what the
verdict measures and a new household reads OVER BUDGET from day one.
"""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import TODAY, add_txn, make_db


def _last_month(day: int) -> dt.date:
    return (TODAY.replace(day=1) - dt.timedelta(days=1)).replace(day=day)


class SeedCountsCategorizedMechanicsTextTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        # one complete closed month of history
        add_txn(self.conn, _last_month(1), 100.0, "HARDWARE BARN")

    def tearDown(self):
        self.conn.close()

    def test_categorized_loan_payment_and_debit_purchase_are_seeded(self):
        add_txn(self.conn, _last_month(5), 350.0,
                "DEPT OF LEARNING DES:STUDENT LN",
                primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT")
        add_txn(self.conn, _last_month(9), 80.0,
                "POS WITHDRAWAL EXAMPLE GROCER")
        seed = budget._seed_config(self.conn, today=TODAY)
        self.assertEqual(seed["other_monthly"], 530.0)

    def test_the_verdict_counts_the_same_rows_the_seed_does(self):
        when = TODAY.replace(day=3)
        add_txn(self.conn, when, 350.0, "DEPT OF LEARNING DES:STUDENT LN",
                primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT")
        add_txn(self.conn, when, 80.0, "POS WITHDRAWAL EXAMPLE GROCER")
        st = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(st["buckets"]["other"]["actual"], 430.0, 2)

    def test_uncategorized_loan_payment_text_stays_in_the_seed(self):
        add_txn(self.conn, _last_month(5), 350.0,
                "DEPT OF LEARNING DES:STUDENT LN", primary=None)
        seed = budget._seed_config(self.conn, today=TODAY)
        self.assertEqual(seed["other_monthly"], 450.0)

    def test_uncategorized_card_payment_and_transfer_stay_out(self):
        add_txn(self.conn, _last_month(6), 900.0,
                "ACME BANK CREDIT CARD AUTOPAY PAYMENT", primary=None)
        add_txn(self.conn, _last_month(7), 500.0,
                "ONLINE TRANSFER TO SAVINGS", primary=None)
        seed = budget._seed_config(self.conn, today=TODAY)
        self.assertEqual(seed["other_monthly"], 100.0)


class SeedScreensTheMerchantNotTheRawLineTests(unittest.TestCase):
    """The seed judges an uncategorized row by its merchant, as the flow
    pass does — never by the raw statement line, whose "WITHDRAWAL" or
    "PAYMENT" is how some banks spell an ordinary debit."""

    def setUp(self):
        self.conn = make_db()
        add_txn(self.conn, _last_month(1), 100.0, "HARDWARE BARN")

    def tearDown(self):
        self.conn.close()

    def test_uncategorized_debit_purchase_spelled_withdrawal_is_seeded(self):
        add_txn(self.conn, _last_month(9), 80.0,
                "POS WITHDRAWAL EXAMPLE GROCER",
                merchant="Example Grocer", primary=None)
        seed = budget._seed_config(self.conn, today=TODAY)
        self.assertEqual(seed["other_monthly"], 180.0)

    def test_uncategorized_utility_e_payment_is_seeded(self):
        add_txn(self.conn, _last_month(12), 60.0, "CITY WATER E-PAYMENT",
                merchant="City Water", primary=None)
        seed = budget._seed_config(self.conn, today=TODAY)
        self.assertEqual(seed["other_monthly"], 160.0)

    def test_the_verdict_counts_those_rows_too(self):
        when = TODAY.replace(day=2)
        add_txn(self.conn, when, 80.0, "POS WITHDRAWAL EXAMPLE GROCER",
                merchant="Example Grocer", primary=None)
        add_txn(self.conn, when, 60.0, "CITY WATER E-PAYMENT",
                merchant="City Water", primary=None)
        st = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(st["buckets"]["other"]["actual"], 140.0, 2)

    def test_merchant_naming_a_transfer_stays_out(self):
        add_txn(self.conn, _last_month(7), 500.0,
                "WEB XFER 0042", merchant="Online Transfer to Savings",
                primary=None)
        seed = budget._seed_config(self.conn, today=TODAY)
        self.assertEqual(seed["other_monthly"], 100.0)


if __name__ == "__main__":
    unittest.main()
