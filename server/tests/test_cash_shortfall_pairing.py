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
        s = self.d["forecast"]["pace_usual"]
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

    def test_a_dip_from_bills_gives_no_spending_advice(self):
        # zero burn and still short: no daily rate clears a dip that comes
        # from what is scheduled (forecast.slow_to_rate) — the notice says
        # so instead of advising $0/day
        self._scenario()
        cash = todayview.build_context(self.d)["cash"]
        self.assertIsNone(cash["slow_to"])
        self.assertTrue(cash["scheduled"])

    def test_the_sent_notice_carries_the_same_pair(self):
        # the clients draw this notice; they never judge the thresholds
        self._scenario()
        note = todayview.build_context(self.d)["cash_notice"]
        self.assertEqual(note["state"], "critical")
        self.assertEqual(note["kind"], "date")
        self.assertEqual(note["until"], self.dip_date)
        self.assertEqual(note["amount"], 50.0)
        self.assertIsNone(note["slow_to_per_day"])
        self.assertTrue(note["scheduled"])
        self.assertIn("bills and card payments due before 07/20/26 exceed "
                      "what is coming in", note["text"])
        self.assertNotIn("$0/day", note["text"])

    def test_plain_text_quotes_the_same_pair(self):
        self._scenario()
        plain = report.build(self.d)[1]
        line = next(l for l in plain.splitlines() if "!! CASH:" in l)
        self.assertIn("Short $50 by", line)
        self.assertNotIn("$5,", line)
        self.assertNotIn("$4,", line)
        self.assertNotIn("$3,", line)


if __name__ == "__main__":
    unittest.main()


class SlowToIsDecidedAgainstTheWalk(unittest.TestCase):
    def test_the_largest_whole_dollar_rate_that_stays_above_zero(self):
        from oikonome.engine import forecast
        self.assertEqual(forecast.slow_to_rate(lambda r: 500 - 30 * r, 100),
                         {"rate": 16.0, "scheduled": False})
        # even $0 a day dips: no advice
        self.assertEqual(forecast.slow_to_rate(lambda r: -10 - r, 100),
                         {"rate": None, "scheduled": True})
        # under $1 a day is no advice either, never "$0/day"
        self.assertEqual(forecast.slow_to_rate(lambda r: 0.5 - r, 100),
                         {"rate": None, "scheduled": True})
