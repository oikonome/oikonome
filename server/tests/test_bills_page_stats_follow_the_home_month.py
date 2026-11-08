"""The Bills page's month stats give every occurrence the Month lens's home.

A bill occurrence belongs to ONE month (budget.occurrence_home): the month
its payment is dated in when it was paid, its due month when it was not.
The Month lens, Today and the email judge each month that way, and the
Bills page's Paid x/y, Paid $ and next-unpaid columns describe the same
month — for the live month and for a past one stepped back to with
`?month=`. So, month by month, the page's per-bill paid dollars add up to
the verdict's fixed actual, and a pinned bill's occurrence count and paid
count are the same on both. Read any other way, a rent paid two days late
is 'missed' in the month it was due and paid again in the next, and an
annual premium prepaid five weeks early is in no month's Paid $ at all.
"""
import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.web import lenses
from oikonome.web.api import _month_bill_stats

from .util import add_bill, add_txn, make_db, write_config


class _Case(unittest.TestCase):
    TODAY = dt.date(2026, 11, 20)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=600)

    def tearDown(self):
        self.conn.close()

    def stats(self, m):
        """The Bills page's stats for 2026-m, read the way the door reads
        them: the live month as of today, a past one as of its last day."""
        st = lenses.month_state(self.conn, 2026, m, self.TODAY)
        closed = (2026, m) < (self.TODAY.year, self.TODAY.month)
        cfg = budget.load_config(self.conn)
        return st, _month_bill_stats(self.conn, cfg, st["as_of"],
                                     closed=closed)

    def assert_pages_agree(self, m, payee):
        st, page = self.stats(m)
        total = round(sum(e["paid_amount"] for e in page.values()), 2)
        self.assertAlmostEqual(total, st["buckets"]["fixed"]["actual"],
                               places=2, msg=f"month {m}")
        pin = next((p for p in st["pinned_bills"] if p["payee"] == payee),
                   None)
        row = page.get(payee) or {}
        self.assertEqual(
            (row.get("occurrences", 0), row.get("paid", 0),
             row.get("next_unpaid")),
            ((pin["occ_count"], pin["paid_count"], pin["next_due"])
             if pin else (0, 0, None)), f"month {m}")
        return st, page


class RentPaidTwoDaysLate(_Case):
    """Due the 30th; October's rent posts on November 2."""

    def setUp(self):
        super().setUp()
        add_bill(self.conn, "Harbor Lofts", 2000.0,
                 next_due=dt.date(2026, 10, 30),
                 last_seen=dt.date(2026, 9, 30), show_today=True)
        for d in (dt.date(2026, 9, 30), dt.date(2026, 11, 2)):
            add_txn(self.conn, d, 2000.0, "HARBOR LOFTS", account="chk",
                    primary="RENT_AND_UTILITIES")
        self.conn.commit()

    def test_october_does_not_call_the_late_rent_missed(self):
        _, page = self.assert_pages_agree(10, "Harbor Lofts")
        self.assertIsNone(page.get("Harbor Lofts", {}).get("next_unpaid"))

    def test_november_counts_it_once_beside_its_own_rent(self):
        _, page = self.assert_pages_agree(11, "Harbor Lofts")
        row = page["Harbor Lofts"]
        self.assertEqual((row["occurrences"], row["paid"]), (2, 1))
        self.assertAlmostEqual(row["paid_amount"], 2000.0, places=2)


class AnnualPremiumPrepaidFiveWeeksEarly(_Case):
    """Due Aug 3, paid Jun 29: June's cash, June's occurrence."""

    def setUp(self):
        super().setUp()
        add_bill(self.conn, "Fernhill Mutual", 1200.0, frequency="YEARLY",
                 next_due=dt.date(2026, 8, 3),
                 last_seen=dt.date(2025, 8, 3), show_today=True)
        add_txn(self.conn, dt.date(2026, 6, 29), 1200.0, "FERNHILL MUTUAL",
                account="chk", primary="LOAN_PAYMENTS")
        self.conn.commit()

    def test_june_pays_it_and_no_later_month_shows_it(self):
        _, june = self.assert_pages_agree(6, "Fernhill Mutual")
        self.assertAlmostEqual(june["Fernhill Mutual"]["paid_amount"],
                               1200.0, places=2)
        for m in (7, 8):
            self.assert_pages_agree(m, "Fernhill Mutual")


class FeePostedTheDayBeforeTheCharge(_Case):
    """A $2.95 fee posts Oct 31, the $1,500 charge it rides with Nov 1:
    the fee is October's cash and the charge November's."""

    def setUp(self):
        super().setUp()
        add_bill(self.conn, "Harbor Lofts", 1500.0,
                 next_due=dt.date(2026, 11, 1),
                 last_seen=dt.date(2026, 10, 1), show_today=True,
                 companions=[{"tokens": "servicing surcharge",
                              "amount": 2.95, "window_days": 3}])
        add_txn(self.conn, dt.date(2026, 10, 1), 1500.0, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        add_txn(self.conn, dt.date(2026, 10, 31), 2.95,
                "SERVICING SURCHARGE", account="chk",
                primary="GENERAL_SERVICES")
        add_txn(self.conn, dt.date(2026, 11, 1), 1500.0, "HARBOR LOFTS",
                account="chk", primary="RENT_AND_UTILITIES")
        self.conn.commit()

    def test_november_paid_dollars_leave_out_octobers_fee(self):
        _, page = self.assert_pages_agree(11, "Harbor Lofts")
        self.assertAlmostEqual(page["Harbor Lofts"]["paid_amount"], 1500.0,
                               places=2)

    def test_october_counts_the_fee_where_it_was_dated(self):
        self.assert_pages_agree(10, "Harbor Lofts")


if __name__ == "__main__":
    unittest.main()
