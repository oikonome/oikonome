"""A bill's old prices count wherever a charge is weighed against it.

A bill whose price changed keeps recognising the charges it was paid with
at the price it had then (budget.bill_amount_bands). Two readers on the
Bills side still compared against the current amount alone:

- the health verdict's "charged, then withdrawn by the feed" evidence: a
  cycle charged at the old price and withdrawn read as a bill gone quiet
  (stale, with a review nag) instead of withdrawn;
- a category pool's history page, which leaves out the charges another
  bill takes: a charge that bill took at its old price was listed as the
  pool's own spending, while the budget spent it on the bill.
"""
import datetime as dt
import json
import unittest

from oikonome.engine import bills
from oikonome.sync import base

from .util import TODAY, add_bill, add_txn, make_db, write_config


def _old_price(conn, payee, amount, until):
    conn.execute(
        "UPDATE bills SET raw = raw || %s::jsonb WHERE payee = %s",
        (json.dumps({"amount_history": [{"amount": amount,
                                         "until": until.isoformat()}]}),
         payee))


class OldPriceTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_cycle_withdrawn_at_the_old_price_reads_withdrawn(self):
        for d in (dt.date(2026, 3, 20), dt.date(2026, 4, 20),
                  dt.date(2026, 5, 20)):
            add_txn(self.conn, d, 100.0, "CITY POWER")
        add_bill(self.conn, "City Power", 200.0,
                 next_due=dt.date(2026, 7, 20), last_seen=dt.date(2026, 5, 20))
        _old_price(self.conn, "City Power", 100.0, dt.date(2026, 6, 25))
        base.mark_removed(self.conn, [add_txn(
            self.conn, dt.date(2026, 6, 20), 100.0, "CITY POWER")])
        h = bills.analyze_bills(self.conn, TODAY)["City Power"]
        self.assertEqual(h["health"], "withdrawn")
        self.assertEqual(h["withdrawn_on"], dt.date(2026, 6, 20))

    def test_a_pool_page_leaves_out_a_charge_another_bill_took_at_its_old_price(self):
        today = dt.date(2026, 7, 25)
        add_bill(self.conn, "Coffee Pool", 400, bill_type="envelope",
                 category="EDUCATION", match_category=True)
        add_bill(self.conn, "Zenith Education", 200)
        _old_price(self.conn, "Zenith Education", 100.0, dt.date(2026, 7, 1))
        # the bill's own payment at the price it had then
        add_txn(self.conn, dt.date(2026, 6, 15), 100.0, "ZENITH EDUCATION",
                primary="EDUCATION", account="chk")
        add_txn(self.conn, dt.date(2026, 7, 12), 80.0, "ATM WITHDRAWAL",
                override="EDUCATION", account="chk")
        h = bills.merchant_history(self.conn, "Coffee Pool", today=today)
        self.assertEqual([t["payee"] for t in h["txns"]], ["ATM WITHDRAWAL"])
        self.assertEqual(h["lifetime"]["count"], 1)


if __name__ == "__main__":
    unittest.main()
