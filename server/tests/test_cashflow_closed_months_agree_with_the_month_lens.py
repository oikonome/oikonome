"""The Cash Flow picture judges a closed month the way the Month lens does.

A closed month's fixed spend is the charges its bills matched. The Month
lens reads that month under the budget and the bill schedule it closed
under (its snapshot) and as a month that is over, seeing the payments
dated after its end that home a late bill elsewhere. If the Cash Flow
picture read the same month against today's bills and as a live day, a
bill deleted since would turn the month's rent into variable spending on
one page while the other still calls it fixed.
"""

import datetime as dt
import unittest

from oikonome.engine import bills, budget, reporting
from oikonome.web import lenses

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 6, 15)


def _lens_fixed(conn, y, m):
    return float(lenses.month_state(conn, y, m, TODAY)
                 ["buckets"]["fixed"]["actual"] or 0)


class ClosedMonthAgreementTests(unittest.TestCase):
    def setUp(self):
        reporting._FIXED_MONTH_CACHE.clear()
        self.conn = make_db()
        write_config(self.conn)
        self.bank = self.conn.execute(
            "SELECT id FROM accounts WHERE type='depository' LIMIT 1"
        ).fetchone()["id"]
        # history before the window, so May is a whole month of ledger
        add_txn(self.conn, dt.date(2026, 3, 3), 41.0, "Corner Grocer",
                account=self.bank)

    def tearDown(self):
        self.conn.close()
        reporting._FIXED_MONTH_CACHE.clear()

    def test_a_bill_deleted_since_still_makes_its_month_fixed(self):
        add_txn(self.conn, dt.date(2026, 5, 3), 130.0, "Rent Co",
                account=self.bank, primary="RENT_AND_UTILITIES")
        add_bill(self.conn, "Rent Co", 130.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 6, 3))
        budget.snapshot_month(self.conn, dt.date(2026, 5, 1))
        bills.delete_bill(self.conn, "Rent Co")
        self.assertEqual(_lens_fixed(self.conn, 2026, 5), 130.0)
        flow = reporting.flow_breakdown(self.conn, TODAY, "1m")
        self.assertEqual(flow["out"]["fixed"], 130.0)

    def test_early_rents_home_where_the_lens_homes_them(self):
        """Rent due the 1st, sometimes paid a day or two early: each closed
        month's fixed figure is the lens's, so the window's total is the
        sum of the months the lens shows."""
        for d in (dt.date(2026, 2, 1), dt.date(2026, 3, 1),
                  dt.date(2026, 3, 30), dt.date(2026, 4, 29),
                  dt.date(2026, 5, 1), dt.date(2026, 6, 1)):
            add_txn(self.conn, d, 130.0, "Rent Co", account=self.bank,
                    primary="RENT_AND_UTILITIES")
        add_bill(self.conn, "Rent Co", 130.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 7, 1), last_seen=dt.date(2026, 3, 1))
        lens = sum(_lens_fixed(self.conn, 2026, m) for m in (3, 4, 5))
        flow = reporting.flow_breakdown(self.conn, TODAY, "3m")
        self.assertEqual(flow["out"]["fixed"], lens)

    def test_an_account_excluded_since_leaves_both_halves_of_the_month(self):
        """May closes with nothing excluded; in June the household excludes
        the card that paid a $40 recurring bill. Cash Flow's May total is
        read under today's exclusions, so its fixed half must be too: the
        card's bill leaves both, never just the total."""
        card = self.conn.execute(
            "SELECT id FROM accounts WHERE type='credit' LIMIT 1"
        ).fetchone()["id"]
        add_txn(self.conn, dt.date(2026, 5, 3), 130.0, "Rent Co",
                account=self.bank, primary="RENT_AND_UTILITIES")
        add_txn(self.conn, dt.date(2026, 5, 9), 40.0, "Quillsoft",
                account=card, primary="GENERAL_SERVICES")
        add_txn(self.conn, dt.date(2026, 5, 12), 500.0, "Corner Grocer",
                account=self.bank)
        add_bill(self.conn, "Rent Co", 130.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 6, 3))
        add_bill(self.conn, "Quillsoft", 40.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 6, 9))
        budget.snapshot_month(self.conn, dt.date(2026, 5, 1))
        cfg = budget.load_config(self.conn)
        cfg["excluded_accounts"] = [card]
        budget.save_config(self.conn, cfg)
        flow = reporting.flow_breakdown(self.conn, TODAY, "1m")
        self.assertEqual(flow["out"]["fixed"], 130.0)
        self.assertEqual(flow["out"]["variable"], 500.0)

    def test_a_closed_month_without_a_snapshot_still_follows_a_new_bill(self):
        add_txn(self.conn, dt.date(2026, 5, 3), 130.0, "Rent Co",
                account=self.bank, primary="RENT_AND_UTILITIES")
        first = reporting.flow_breakdown(self.conn, TODAY, "1m")
        self.assertEqual(first["out"]["fixed"], 0.0)
        add_bill(self.conn, "Rent Co", 130.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 6, 3))
        self.assertEqual(_lens_fixed(self.conn, 2026, 5), 130.0)
        second = reporting.flow_breakdown(self.conn, TODAY, "1m")
        self.assertEqual(second["out"]["fixed"], 130.0)

    def test_every_closed_month_matches_the_lens_with_late_payments(self):
        """A rent due the 30th and paid on the 2nd belongs to the month
        it is paid in; a closed month must see past its own end to know
        that, exactly as the lens does."""
        for d in (dt.date(2026, 3, 2), dt.date(2026, 4, 2),
                  dt.date(2026, 5, 2), dt.date(2026, 6, 2)):
            add_txn(self.conn, d, 130.0, "Rent Co", account=self.bank,
                    primary="RENT_AND_UTILITIES")
        add_txn(self.conn, dt.date(2026, 4, 29), 55.0, "Power Co",
                account=self.bank, primary="RENT_AND_UTILITIES")
        add_txn(self.conn, dt.date(2026, 6, 1), 55.0, "Power Co",
                account=self.bank, primary="RENT_AND_UTILITIES")
        add_bill(self.conn, "Rent Co", 130.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 6, 30))
        add_bill(self.conn, "Power Co", 55.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 6, 30))
        budget.snapshot_month(self.conn, dt.date(2026, 4, 1))
        lens = sum(_lens_fixed(self.conn, 2026, m) for m in (4, 5))
        reporting._FIXED_MONTH_CACHE.clear()
        # 3m = Mar..May; March's lens figure added for the whole window
        lens3 = lens + _lens_fixed(self.conn, 2026, 3)
        flow = reporting.flow_breakdown(self.conn, TODAY, "3m")
        self.assertEqual(flow["out"]["fixed"], lens3)


if __name__ == "__main__":
    unittest.main()
