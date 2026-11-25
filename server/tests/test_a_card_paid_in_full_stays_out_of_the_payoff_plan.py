"""A card whose statement is cleared every month is not revolving debt.

What breaks if these fail: the Debt page quotes a debt-free date years
away and interest to pay on a card that never charges any, because a
paid statement's $0 minimum read as "not reported" and the plan walked an
estimated minimum against the whole balance (specs/debt-plan.md).
"""

import datetime as dt
import unittest

from oikonome.engine import debt
from oikonome.engine.compat import jsonb

from .util import TODAY, add_txn, make_db, write_config


class PaidInFullTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute("UPDATE accounts SET balance_current = 840"
                          " WHERE id = 'card'")

    def tearDown(self):
        self.conn.close()

    def _statement(self, stmt, paid, minimum=0.0, issued=20, paid_back=3,
                   due=None):
        self.conn.execute(
            "INSERT INTO liabilities (account_id, raw) VALUES ('card', %s)",
            (jsonb({"aprs": [{"apr_type": "purchase_apr",
                              "apr_percentage": 23.0}],
                    "minimum_payment_amount": minimum,
                    "last_statement_balance": stmt,
                    "last_statement_issue_date":
                        (TODAY - dt.timedelta(days=issued)).isoformat(),
                    "next_payment_due_date": None if due is None else
                        (TODAY + dt.timedelta(days=due)).isoformat(),
                    "last_payment_amount": paid,
                    "last_payment_date":
                        (TODAY - dt.timedelta(days=paid_back)).isoformat()}),))

    def test_a_paid_statement_takes_the_card_out_of_the_plan(self):
        self._statement(612.40, 612.40)
        r = debt.build(self.conn, TODAY)
        self.assertEqual([d["id"] for d in r["debts"]], [])
        self.assertEqual(r["plan"]["total_interest"], 0.0)
        [card] = r["paid_in_full"]
        self.assertEqual((card["id"], card["basis"], card["balance"],
                          card["statement_balance"], card["last_payment"]),
                         ("card", "issuer", 840.0, 612.4, 612.4))

    def test_a_statement_not_due_yet_leaves_the_card_out(self):
        # the payment that cleared the previous statement predates this
        # one, which is due in two weeks: the card is still paid in full
        self._statement(731.15, 588.20, minimum=25.0, issued=10,
                        paid_back=20, due=14)
        r = debt.build(self.conn, TODAY)
        self.assertEqual([d["id"] for d in r["debts"]], [])
        self.assertEqual(r["paid_in_full"][0]["basis"], "issuer")

    def test_a_statement_past_due_with_no_payment_since_revolves(self):
        self._statement(731.15, 588.20, minimum=25.0, issued=40,
                        paid_back=50, due=-15)
        r = debt.build(self.conn, TODAY)
        self.assertEqual([d["id"] for d in r["debts"]], ["card"])
        self.assertEqual(r["paid_in_full"], [])

    def test_a_part_paid_statement_keeps_the_card_in_the_plan(self):
        self._statement(612.40, 50.00, minimum=35.0)
        r = debt.build(self.conn, TODAY)
        self.assertEqual([d["id"] for d in r["debts"]], ["card"])
        self.assertEqual(r["paid_in_full"], [])

    def test_interest_charged_lately_means_it_revolves(self):
        self._statement(612.40, 612.40)
        add_txn(self.conn, TODAY - dt.timedelta(days=40), 11.73,
                "INTEREST CHARGE ON PURCHASES", primary="BANK_FEES",
                detailed="BANK_FEES_INTEREST_CHARGE")
        r = debt.build(self.conn, TODAY)
        self.assertEqual([d["id"] for d in r["debts"]], ["card"])
        self.assertEqual(r["paid_in_full"], [])

    def test_the_ledger_alone_shows_a_card_cleared_each_month(self):
        # no statement from the issuer: three months of payments that
        # cover the balance, and no interest
        add_txn(self.conn, TODAY - dt.timedelta(days=150), 20.0, "Grocer")
        for back, amt in ((5, -790.0), (35, -655.0), (65, -702.0)):
            add_txn(self.conn, TODAY - dt.timedelta(days=back), amt,
                    "AUTOPAY PAYMENT - THANK YOU", primary="LOAN_PAYMENTS",
                    detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        r = debt.build(self.conn, TODAY)
        self.assertEqual(r["debts"], [])
        self.assertEqual(r["paid_in_full"][0]["basis"], "ledger")

    def test_a_short_ledger_is_no_evidence(self):
        for back, amt in ((5, -790.0), (35, -655.0)):
            add_txn(self.conn, TODAY - dt.timedelta(days=back), amt,
                    "AUTOPAY PAYMENT", primary="LOAN_PAYMENTS",
                    detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        r = debt.build(self.conn, TODAY)
        self.assertEqual([d["id"] for d in r["debts"]], ["card"])
        self.assertEqual(r["paid_in_full"], [])

    def test_small_payments_on_a_big_balance_are_not_paid_in_full(self):
        self.conn.execute("UPDATE accounts SET balance_current = 5200"
                          " WHERE id = 'card'")
        add_txn(self.conn, TODAY - dt.timedelta(days=150), 20.0, "Grocer")
        for back in (5, 35, 65):
            add_txn(self.conn, TODAY - dt.timedelta(days=back), -100.0,
                    "PAYMENT", primary="LOAN_PAYMENTS",
                    detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        r = debt.build(self.conn, TODAY)
        self.assertEqual([d["id"] for d in r["debts"]], ["card"])
        self.assertEqual(r["paid_in_full"], [])


if __name__ == "__main__":
    unittest.main()
