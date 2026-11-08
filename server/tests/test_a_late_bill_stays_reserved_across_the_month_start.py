"""A bill still unpaid when its month ends stays reserved until it posts.

Rent due Friday the 30th, autopay posting Monday the 2nd. On the 31st the
cash surfaces (forecast walk, runway, Today headroom, the "after cards &
bills" tile) hold the rent back for tomorrow. On the 1st nothing in the
ledger has changed, so they must hold it back still: dropping it because
its due date now sits in last month lets headroom jump by the whole rent
overnight, and the 2nd's balance drop is one the forecast never expected.
Once the payment posts it is in the balance and is reserved no more.

The reservation lasts as long as a payment could still be that bill's —
the occurrence's own match window. A bill skipped long ago is not owed
tomorrow.
"""
import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import report

from .util import add_bill, add_txn, make_db, write_config

RENT = 1613.0
DUE = dt.date(2026, 10, 30)


class LateRentAcrossTheMonthStart(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Harbor Lofts", RENT, next_due=DUE,
                 last_seen=dt.date(2026, 9, 30))
        add_txn(self.conn, dt.date(2026, 9, 30), RENT, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def reserved(self, today):
        """The rent the cash walk holds back over the next two weeks."""
        up = budget.upcoming_bill_occurrences(
            self.conn, budget.load_config(self.conn), today,
            today + dt.timedelta(days=14))
        return [(o["due"], o["planned"]) for o in up
                if o["payee"] == "Harbor Lofts"
                and o["occ"]["due"] == DUE]

    def runway_due(self, today):
        return report.gather(self.conn, today)["runway"]["due_total"]

    def test_the_rent_is_held_back_on_the_31st_and_on_the_1st(self):
        for today in (dt.date(2026, 10, 31), dt.date(2026, 11, 1)):
            self.assertEqual(self.reserved(today),
                             [(today + dt.timedelta(days=1), RENT)], today)

    def test_headroom_does_not_jump_on_the_1st(self):
        before = self.runway_due(dt.date(2026, 10, 31))
        after = self.runway_due(dt.date(2026, 11, 1))
        self.assertAlmostEqual(before, after)
        self.assertGreaterEqual(after, RENT)

    def test_the_tile_still_counts_it_on_the_1st(self):
        cfg = budget.load_config(self.conn)
        left = report._still_due_this_month(self.conn, cfg,
                                            dt.date(2026, 11, 1))
        self.assertIn(DUE, [o["occ"]["due"] for o in left])

    def test_once_it_posts_it_is_not_reserved_again(self):
        add_txn(self.conn, dt.date(2026, 11, 2), RENT, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        self.conn.commit()
        today = dt.date(2026, 11, 2)
        self.assertEqual(self.reserved(today), [])
        # nothing else of the rent is due inside two weeks
        self.assertEqual(self.runway_due(today), 0.0)

    def test_a_bill_past_its_match_window_is_not_owed_tomorrow(self):
        """Due the 30th, still unpaid on the 20th after: a payment now
        would not even count as that bill's, so it is not reserved."""
        today = dt.date(2026, 11, 20)
        self.assertEqual(self.reserved(today), [])


class AnnualPremiumPaidEarlyLastMonth(unittest.TestCase):
    """An annual premium due Oct 20 and paid 30 days early on Sep 20 is
    paid: on Nov 1 the forecast must still see that payment, though it
    is more than 35 days before the 1st, and not reserve it again."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Kestrel Mutual", 874.0, frequency="YEARLY",
                 next_due=dt.date(2026, 10, 20),
                 last_seen=dt.date(2025, 10, 20))
        add_txn(self.conn, dt.date(2026, 9, 20), 874.0, "KESTREL MUTUAL",
                account="chk", primary="LOAN_PAYMENTS")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_the_early_payment_still_settles_it_after_the_1st(self):
        for today in (dt.date(2026, 11, 1), dt.date(2026, 11, 10)):
            up = budget.upcoming_bill_occurrences(
                self.conn, budget.load_config(self.conn), today,
                today + dt.timedelta(days=14))
            self.assertEqual(
                [o for o in up if o["payee"] == "Kestrel Mutual"], [], today)


if __name__ == "__main__":
    unittest.main()
