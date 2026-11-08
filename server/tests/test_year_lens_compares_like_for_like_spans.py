"""The Year lens sets the lens year against the SAME span of the year
before. While the year is in progress the prior year is cut at the same
day of the year (Feb 29 reads as Feb 28; a month's last day takes the
prior year's whole month), so a household whose spending did not change
reads a zero delta — never a green drop every category shows only because
eleven months are set against twelve. A closed year compares whole years.
The categories, the top-line totals and `prev_through` all follow the one
window."""

import datetime as dt
import unittest

from oikonome.web import lenses

from .util import add_txn, make_db, write_config


def steady_household(conn, years):
    """$4,200 paid in on the 1st and $730 of groceries on the 5th of every
    month of `years` (invented figures)."""
    for y in years:
        for m in range(1, 13):
            add_txn(conn, dt.date(y, m, 1), -4200.0, "PAYROLL",
                    account="chk", primary="INCOME")
            add_txn(conn, dt.date(y, m, 5), 730.0, "GROCER",
                    primary="FOOD_AND_DRINK")


class PriorYearWindowTests(unittest.TestCase):
    def test_year_in_progress_stops_at_the_same_day(self):
        self.assertEqual(lenses.prior_year_window(2026, dt.date(2026, 11, 7)),
                         (dt.date(2025, 1, 1), dt.date(2025, 11, 8),
                          dt.date(2025, 11, 7)))

    def test_closed_year_is_whole(self):
        self.assertEqual(lenses.prior_year_window(2025, dt.date(2026, 11, 7)),
                         (dt.date(2024, 1, 1), dt.date(2025, 1, 1), None))

    def test_year_end_day_is_whole(self):
        self.assertEqual(lenses.prior_year_window(2026, dt.date(2026, 12, 31)),
                         (dt.date(2025, 1, 1), dt.date(2026, 1, 1), None))

    def test_feb_29_clamps_to_feb_28(self):
        self.assertEqual(lenses.prior_year_window(2028, dt.date(2028, 2, 29)),
                         (dt.date(2027, 1, 1), dt.date(2027, 3, 1),
                          dt.date(2027, 2, 28)))

    def test_feb_28_month_end_takes_the_leap_years_whole_february(self):
        self.assertEqual(lenses.prior_year_window(2029, dt.date(2029, 2, 28)),
                         (dt.date(2028, 1, 1), dt.date(2028, 3, 1),
                          dt.date(2028, 2, 29)))


class YearLensSpanTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=900, other_monthly=600)

    def tearDown(self):
        self.conn.close()

    def test_steady_spend_mid_november_reads_no_change(self):
        steady_household(self.conn, (2025,))
        for m in range(1, 12):        # 2026 so far: Jan .. Nov 7
            add_txn(self.conn, dt.date(2026, m, 1), -4200.0, "PAYROLL",
                    account="chk", primary="INCOME")
            add_txn(self.conn, dt.date(2026, m, 5), 730.0, "GROCER",
                    primary="FOOD_AND_DRINK")
        y = lenses.year_summary(self.conn, 2026, today=dt.date(2026, 11, 7))
        (label, cur, prev, _key), = y["categories"]
        self.assertEqual((cur, prev), (730.0 * 11, 730.0 * 11))
        self.assertEqual(y["totals"]["spend"], y["prev_totals"]["spend"])
        self.assertEqual(y["prev_totals"]["spend"], 730.0 * 11)
        self.assertEqual(y["prev_totals"]["income"], 4200.0 * 11)
        self.assertEqual(y["prev_through"], "2025-11-07")

    def test_closed_year_compares_whole_years(self):
        steady_household(self.conn, (2024, 2025))
        y = lenses.year_summary(self.conn, 2025, today=dt.date(2026, 11, 7))
        (_l, cur, prev, _k), = y["categories"]
        self.assertEqual((cur, prev), (730.0 * 12, 730.0 * 12))
        self.assertEqual(y["prev_totals"]["spend"], 730.0 * 12)
        self.assertEqual(y["prev_totals"]["income"], 4200.0 * 12)
        self.assertIsNone(y["prev_through"])

    def test_leap_day_counts_the_prior_feb_28_and_nothing_after(self):
        add_txn(self.conn, dt.date(2027, 2, 28), 410.0, "GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2027, 3, 1), 999.0, "GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2028, 2, 10), 300.0, "GROCER",
                primary="FOOD_AND_DRINK")
        y = lenses.year_summary(self.conn, 2028, today=dt.date(2028, 2, 29))
        self.assertEqual(y["categories"][0][2], 410.0)
        self.assertEqual(y["prev_totals"]["spend"], 410.0)
        self.assertEqual(y["prev_through"], "2027-02-28")


if __name__ == "__main__":
    unittest.main()
