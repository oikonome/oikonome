"""A card payment made by hand shows on the card days before the debit
leaves checking. In between, the card's balance has dropped by it and
checking has not, so the cash timeline walks the payment out of checking
on the day it should land — three business days after the credit, never
before tomorrow — until the checking leg appears. The leg is found by the
Cash Flow month's own pairing (same amount, within five days, a marked
card payment or a plain transfer out). Figures and names are invented."""

import datetime as dt
import unittest

from oikonome.engine import forecast
from oikonome.engine.compat import jsonb
from oikonome.web import report, todayview

from .util import TODAY, add_txn, make_db, write_config

LABEL = "Card payment on its way — Test Card"
CREDIT_DAY = TODAY - dt.timedelta(days=1)          # a Tuesday
LANDS = "2026-07-17"                                # three business days on


class PaymentOnItsWayTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0)
        self.conn.execute("UPDATE accounts SET balance_available=3000, "
                          "balance_current=3000 WHERE id='chk'")
        # the card's balance already dropped by the 420 payment
        self.conn.execute("UPDATE accounts SET balance_current=60 "
                          "WHERE id='card'")

    def tearDown(self):
        self.conn.close()

    def _credit(self, day=CREDIT_DAY, amount=420.0, **kw):
        kw.setdefault("primary", "LOAN_PAYMENTS")
        return add_txn(self.conn, day, -amount, "PAYMENT THANK YOU",
                       detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", **kw)

    def _on_way(self):
        f = forecast.build(self.conn, today=TODAY, days=60)
        return f, [(d, a) for d, a, p in f["events"] if p == LABEL]

    def test_a_credit_with_no_checking_leg_leaves_checking_when_it_lands(self):
        self._credit()
        f, ev = self._on_way()
        self.assertEqual(ev, [(LANDS, -420.0)])
        walk = dict(f["pace_stmt"]["series"])
        self.assertAlmostEqual(walk["2026-07-16"] - walk[LANDS], 420.0,
                               places=2)

    def test_the_checking_leg_ends_it(self):
        self._credit()
        add_txn(self.conn, TODAY, 420.0, "ONLINE PMT CARD", account="chk",
                primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        self.assertEqual(self._on_way()[1], [])

    def test_a_plain_transfer_out_is_the_leg_too(self):
        self._credit()
        add_txn(self.conn, TODAY, 420.0, "TRANSFER TO CARD", account="chk",
                primary="TRANSFER_OUT")
        self.assertEqual(self._on_way()[1], [])

    def test_it_never_lands_before_tomorrow(self):
        self._credit(day=TODAY - dt.timedelta(days=5))    # a Friday
        self.assertEqual(self._on_way()[1],
                         [((TODAY + dt.timedelta(days=1)).isoformat(),
                           -420.0)])

    def test_an_older_credit_is_no_longer_on_its_way(self):
        self._credit(day=TODAY - dt.timedelta(days=6))
        self.assertEqual(self._on_way()[1], [])

    def test_a_pending_credit_or_a_refund_is_no_payment(self):
        self._credit(pending=1)
        add_txn(self.conn, CREDIT_DAY, -420.0, "BLUEWATER SOFAS RETURN",
                primary="GENERAL_MERCHANDISE")
        self.assertEqual(self._on_way()[1], [])

    def test_a_pending_leg_counts_once_it_is_in_the_start_balance(self):
        self._credit()
        add_txn(self.conn, TODAY, 420.0, "ONLINE PMT CARD", account="chk",
                primary="LOAN_PAYMENTS", pending=1,
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        # the bank reports an available balance: the hold is in it
        self.assertEqual(self._on_way()[1], [])
        # only a current balance: the hold is not in the start yet
        self.conn.execute("UPDATE accounts SET balance_available=NULL "
                          "WHERE id='chk'")
        self.assertEqual(self._on_way()[1], [(LANDS, -420.0)])

    def test_with_a_paid_statement_nothing_is_counted_twice(self):
        due = TODAY + dt.timedelta(days=9)
        self.conn.execute(
            "INSERT INTO liabilities (account_id, raw) VALUES ('card', %s)",
            (jsonb({"last_statement_balance": 420.0,
                    "last_statement_issue_date":
                        (TODAY - dt.timedelta(days=15)).isoformat(),
                    "last_payment_amount": 420.0,
                    "last_payment_date": CREDIT_DAY.isoformat(),
                    "next_payment_due_date": due.isoformat()}),))
        self._credit()
        f, ev = self._on_way()
        self.assertEqual(ev, [(LANDS, -420.0)])
        # the statement is settled: only the 60 charged since is owed,
        # with the next statement
        self.assertEqual(sum(a for _d, a, _p in f["card_events_stmt"]),
                         -60.0)
        self.assertNotIn(due.isoformat(),
                         [d for d, _a, _p in f["card_events_stmt"]])
        self.assertAlmostEqual(f["pace_stmt"]["end"], 3000.0 - 420.0 - 60.0,
                               places=2)

    def test_every_surface_prints_the_row(self):
        self._credit()
        st = report.gather(self.conn, TODAY)
        ctx = todayview.build_context(st)
        self.assertIn((LANDS, -420.0, LABEL),
                      [(r[0], r[1], r[2]) for r in ctx["fc_rows"]])
        # the email's timeline table prints the row as sent (the plain
        # text lists no timeline rows); the page and the phone print
        # forecast_rows, built from these same rows
        _s, _plain, html = report.build(st)
        self.assertIn(LABEL, html)


if __name__ == "__main__":
    unittest.main()
