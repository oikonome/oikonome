"""An envelope's bar is one fraction, decided on the server.

The Bills page on the web and on the phone, and the Today envelope card,
each draw how full an envelope's pool is. When each client divided by its
own figure (one by the bill's amount with a fallback to this month's
swipes, the other by the pool) the same envelope drew two bars. The
fraction is `budget.envelope_frac`, sent as the bill row's `period_frac`
and the card's `frac`, and both clients print it.
"""

import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web.api import _month_bill_stats

from .util import add_bill, add_txn, make_db, write_config


class TheFractionItself(unittest.TestCase):
    def test_is_what_the_period_drew_over_the_pool(self):
        self.assertEqual(budget.envelope_frac(300.0, 1200.0), 0.25)

    def test_is_held_to_zero_and_one(self):
        self.assertEqual(budget.envelope_frac(1500.0, 1200.0), 1.0)
        # a refund larger than the period's charges draws nothing, not less
        self.assertEqual(budget.envelope_frac(-40.0, 1200.0), 0.0)

    def test_a_pool_of_nothing_reads_full(self):
        self.assertEqual(budget.envelope_frac(0.0, 0.0), 1.0)


class TheBillsPageAndTodayDrawOneBar(unittest.TestCase):
    TODAY = dt.date(2026, 7, 15)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # an annual pool: earlier months of the year draw on it too, so this
        # month's swipes alone are not how full it is
        add_bill(self.conn, "Pet Care", 1200.0, bill_type="envelope",
                 frequency=None, interval=12, category="PETS",
                 match_category=True, show_today=True)
        add_txn(self.conn, dt.date(2026, 3, 10), 200.0, "NORTHWIND VET",
                override="PETS")
        add_txn(self.conn, self.TODAY, 100.0, "NORTHWIND VET",
                override="PETS")

    def tearDown(self):
        self.conn.close()

    def test_the_bill_row_carries_the_period_fraction(self):
        cfg = budget.load_config(self.conn)
        row = _month_bill_stats(self.conn, cfg, self.TODAY)["Pet Care"]
        self.assertAlmostEqual(row["period_used"], 300.0, places=2)
        self.assertEqual(row["period_frac"], 0.25)

    def test_a_bill_that_is_not_an_envelope_has_no_fraction(self):
        add_bill(self.conn, "Rent", 2000.0, next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        cfg = budget.load_config(self.conn)
        stats = _month_bill_stats(self.conn, cfg, self.TODAY)
        self.assertIsNone(stats.get("Rent", {"period_frac": None})
                          ["period_frac"])

    def test_the_today_card_reads_the_same_fraction(self):
        from oikonome.web.todayview import _bill_cards
        cfg = budget.load_config(self.conn)
        row = _month_bill_stats(self.conn, cfg, self.TODAY)["Pet Care"]
        st = budget.month_status(self.conn, self.TODAY)
        st["today"] = self.TODAY
        (card,) = [c for c in _bill_cards(st) if c["kind"] == "envelope"]
        self.assertEqual(card["frac"], row["period_frac"])


if __name__ == "__main__":
    unittest.main()
