"""The budget wizard's seeds count only the offers the Bills page lists.

With no bill approved yet, the planner's bill load and income fall back to
the pending offers. They summed every pending row — twins of one merchant
at several amounts, offers whose date lapsed a cycle ago — where the page
lists one offer per series and none that lapsed (bills.pending_proposals),
so the planner counted bills nobody could see."""

import datetime as dt
import json
import unittest

from oikonome.engine import budget

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 17)


def _offer(conn, pid, payee, amount, due, income=False):
    conn.execute(
        """INSERT INTO bill_proposals (id, kind, payee, amount, frequency,
                                       "interval", next_due, evidence, status)
           VALUES (%s,'add',%s,%s,'MONTHLY',1,%s,%s,'pending')""",
        (pid, payee, amount, due, json.dumps({"income": income, "hits": 3})))


class OnlyListedOffersSeed(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_txn(self.conn, dt.date(2026, 10, 3), 80.0, "GROCER")
        # one water series at two amounts: the page lists the later one
        _offer(self.conn, "prop:w1", "FABRIKAM WATER", -60.0,
               dt.date(2026, 11, 25))
        _offer(self.conn, "prop:w2", "FABRIKAM WATER", -70.0,
               dt.date(2026, 10, 25))
        # a gym offer a full cycle past its date: lapsed, not listed
        _offer(self.conn, "prop:g", "CONTOSO GYM", -40.0, dt.date(2026, 8, 1))
        # pay, listed, and a lapsed second payer
        _offer(self.conn, "prop:p1", "NORTHWIND PAYROLL", 3000.0,
               dt.date(2026, 12, 1), income=True)
        _offer(self.conn, "prop:p2", "OLD EMPLOYER PAYROLL", 2000.0,
               dt.date(2026, 6, 1), income=True)

    def tearDown(self):
        self.conn.close()

    def test_the_bill_load_is_the_listed_offers(self):
        s = budget.suggest_budgets(self.conn, TODAY)
        self.assertEqual(s["avg_bills_monthly"], 60.0)
        self.assertEqual(s["bills_count"], 1)

    def test_the_income_seed_is_the_listed_paycheck(self):
        s = budget.suggest_budgets(self.conn, TODAY)
        self.assertEqual(s["suggestions"]["income_monthly"], 3000.0)


if __name__ == "__main__":
    unittest.main()
