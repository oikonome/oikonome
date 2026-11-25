"""A card statement the household already paid is not paid again in the
autopay walk: a payment on or after the statement's issue date that covers
it settles it, so nothing leaves at the coming due date and the whole
balance waits for the next statement. A partial payment leaves the rest of
the statement at the due date; a payment before the issue date paid an
earlier statement. Figures and names are invented."""

import datetime as dt
import unittest

from oikonome.engine import business_days, forecast
from oikonome.engine.compat import jsonb

from .util import TODAY, make_db, write_config

DUE = TODAY + dt.timedelta(days=8)
# a payment due on a weekend or a bank holiday leaves checking the next
# business day, as every card payment the walk dates does
NEXT_DUE = business_days.next_business_day(DUE.replace(month=DUE.month + 1))
ISSUED = TODAY - dt.timedelta(days=17)


class PaidStatementTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0)
        self.conn.execute("UPDATE accounts SET balance_available=3000, "
                          "balance_current=3000 WHERE id='chk'")
        # 420 on the statement, paid since; 160 charged after it
        self.conn.execute("UPDATE accounts SET balance_current=160 "
                          "WHERE id='card'")

    def tearDown(self):
        self.conn.close()

    def _liab(self, **raw):
        base = {"last_statement_balance": 420.0,
                "last_statement_issue_date": ISSUED.isoformat(),
                "next_payment_due_date": DUE.isoformat()}
        base.update(raw)
        self.conn.execute(
            "INSERT INTO liabilities (account_id, raw) VALUES ('card', %s)",
            (jsonb(base),))

    def _stmt(self):
        f = forecast.build(self.conn, today=TODAY, days=60)
        out: dict[str, float] = {}
        for d, a, _ in f["card_events_stmt"]:
            out[d] = round(out.get(d, 0.0) + a, 2)
        return f, out

    def test_a_statement_paid_in_full_leaves_nothing_at_its_due_date(self):
        self._liab(last_payment_amount=420.0,
                   last_payment_date=(TODAY - dt.timedelta(days=4)).isoformat())
        f, stmt = self._stmt()
        self.assertEqual(stmt, {NEXT_DUE.isoformat(): -160.0})
        self.assertEqual([d for d, _a, _n in f["card_autopay"]],
                         [NEXT_DUE.isoformat()])

    def test_a_partial_payment_leaves_the_rest_at_the_due_date(self):
        self.conn.execute("UPDATE accounts SET balance_current=320 "
                          "WHERE id='card'")
        self._liab(last_payment_amount=260.0,
                   last_payment_date=(TODAY - dt.timedelta(days=4)).isoformat())
        _f, stmt = self._stmt()
        self.assertEqual(stmt, {DUE.isoformat(): -160.0,
                                NEXT_DUE.isoformat(): -160.0})

    def test_a_payment_before_the_statement_paid_an_earlier_one(self):
        self.conn.execute("UPDATE accounts SET balance_current=580 "
                          "WHERE id='card'")
        self._liab(last_payment_amount=420.0,
                   last_payment_date=(ISSUED - dt.timedelta(days=3)).isoformat())
        _f, stmt = self._stmt()
        self.assertEqual(stmt, {DUE.isoformat(): -420.0,
                                NEXT_DUE.isoformat(): -160.0})

    def test_an_unreadable_payment_is_no_payment(self):
        self.conn.execute("UPDATE accounts SET balance_current=580 "
                          "WHERE id='card'")
        self._liab(last_payment_amount="N/A", last_payment_date="soon")
        _f, stmt = self._stmt()
        self.assertEqual(stmt, {DUE.isoformat(): -420.0,
                                NEXT_DUE.isoformat(): -160.0})

    def test_the_rule_itself(self):
        owed = forecast.statement_owed
        self.assertEqual(owed(420.0, ISSUED, ISSUED, 420.0), 0.0)
        self.assertEqual(owed(420.0, ISSUED, ISSUED, 500.0), 0.0)
        self.assertEqual(owed(420.0, ISSUED, ISSUED, 100.0), 320.0)
        self.assertEqual(owed(420.0, ISSUED,
                              ISSUED - dt.timedelta(days=1), 420.0), 420.0)
        self.assertEqual(owed(420.0, None, ISSUED, 420.0), 420.0)
        self.assertEqual(owed(420.0, ISSUED, None, None), 420.0)


if __name__ == "__main__":
    unittest.main()
