"""The cash-critical message pairs a shortfall DOLLAR with the DATE it
happens. The forecast can dip a little early and trough deep much later;
"short $<deep trough> by <early date>" welds the wrong amount to the wrong
day and panics the reader. Both mirrors — the todayview context (SPA + email
HTML) and the plain text — must quote the balance AT the first-negative
date, and the slow-spending suggestion must be computed from that same pair.
"""

import datetime as dt
import unittest

from oikonome.web import report, todayview

from .util import TODAY, add_bill, make_db, write_config


class CashShortfallPairingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        # zero variable budgets → zero forecast burn; the walk is bills only
        write_config(self.conn, food_monthly=0, other_monthly=0)
        # checking $100, no card debt
        self.conn.execute("UPDATE accounts SET balance_available=100, "
                          "balance_current=100 WHERE id='chk'")
        self.conn.execute(
            "UPDATE accounts SET balance_current=0 WHERE id='card'")
        # paycheck lands day +3 (keeps headroom positive: no bills due
        # before it), a bill on day +5 dips the balance to −$50, and a big
        # bill on day +40 digs a far deeper trough
        add_bill(self.conn, "Paycheck Co", 1000.0, frequency="WEEKLY",
                 interval=2, next_due=TODAY + dt.timedelta(days=3),
                 income=True, last_seen=TODAY - dt.timedelta(days=11))
        add_bill(self.conn, "Water", 1150.0,
                 next_due=TODAY + dt.timedelta(days=5),
                 last_seen=TODAY - dt.timedelta(days=25))
        add_bill(self.conn, "Insurance", 5000.0,
                 next_due=TODAY + dt.timedelta(days=40),
                 last_seen=TODAY - dt.timedelta(days=20))
        self.d = report.gather(self.conn, TODAY)
        self.dip_date = (TODAY + dt.timedelta(days=5)).isoformat()

    def tearDown(self):
        self.conn.close()

    def _scenario(self):
        s = self.d["forecast"]["pace_stmt"]
        # fixture sanity: early small dip, deep later trough
        self.assertEqual(s["negative_date"], self.dip_date)
        self.assertEqual(s["first_neg_amount"], -50.0)
        self.assertLess(s["min"], -1000.0)
        self.assertNotEqual(s["min_date"], s["negative_date"])
        return s

    def test_context_pairs_the_dip_date_with_the_dip_amount(self):
        self._scenario()
        cash = todayview.build_context(self.d)["cash"]
        self.assertIsNotNone(cash)
        self.assertEqual(cash["state"], "critical")
        self.assertEqual(cash["kind"], "date")
        self.assertEqual(cash["by"], self.dip_date)
        self.assertEqual(cash["short"], 50.0)

    def test_slow_to_uses_the_amount_of_the_date_shown(self):
        s = self._scenario()
        cash = todayview.build_context(self.d)["cash"]
        # $50 short in 5 days at zero burn: no daily rate to slow below
        # −$650/day — the honest suggestion from the shown pair is $0/day
        # of headroom against the dip, i.e. rate + first_neg/days
        expected = max(0.0, (s["rate"] or 0) + s["first_neg_amount"] / 5)
        self.assertAlmostEqual(cash["slow_to"], expected, places=2)

    def test_plain_text_quotes_the_same_pair(self):
        self._scenario()
        plain = report.build(self.d)[1]
        line = next(l for l in plain.splitlines() if "!! CASH:" in l)
        self.assertIn("short $50 by", line)
        self.assertNotIn("$5,", line)
        self.assertNotIn("$4,", line)
        self.assertNotIn("$3,", line)


if __name__ == "__main__":
    unittest.main()
