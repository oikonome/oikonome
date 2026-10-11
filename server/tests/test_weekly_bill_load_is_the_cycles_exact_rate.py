"""A weekly or every-other-week bill costs its cycle's rate every month.

A two-week cycle does not divide a calendar year: a biweekly Friday
payday whose phase lands on January 1 falls 27 times that year, and a
weekly Thursday bill falls 53 times in a year that starts on a Thursday.
Counting occurrences over the one calendar year in progress reads those
years as 27/26 and 53/52 of the standing cost, so the Bills headline, the
month plan's bill load, the planned surplus and the month snapshots carry
a few percent of money that does not exist for the whole year, then drop
back the next January. The monthly load is the cycle's exact rate over a
mean year, the same in every year.
"""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import _ensure_db, add_bill, make_db, write_config

MEAN_YEAR = 365.2425


class WeeklyCycleLoadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_biweekly_payday_reads_the_same_load_in_a_27_payday_year(self):
        # Friday 2027-01-01 is a payday, so 2027 holds 27 of them
        add_bill(self.conn, "Payroll", 2000.0, income=True,
                 frequency="WEEKLY", interval=2,
                 next_due=dt.date(2027, 1, 1),
                 last_seen=dt.date(2026, 12, 18))
        want = 2000.0 * (MEAN_YEAR / 14) / 12
        for today in (dt.date(2026, 6, 1), dt.date(2027, 6, 1),
                      dt.date(2028, 6, 1)):
            got = budget.recurring_load(self.conn, today)["income_monthly"]
            self.assertAlmostEqual(got, want, delta=0.01, msg=str(today))

    def test_a_weekly_bill_in_a_53_week_year_is_not_overstated(self):
        # 2026 opens on a Thursday: 53 Thursdays
        add_bill(self.conn, "Lawn Service", 100.0,
                 frequency="WEEKLY", interval=1,
                 next_due=dt.date(2026, 10, 15),
                 last_seen=dt.date(2026, 10, 8))
        want = 100.0 * (MEAN_YEAR / 7) / 12
        for today in (dt.date(2026, 10, 9), dt.date(2027, 3, 1)):
            st = budget.recurring_load(self.conn, today)
            self.assertAlmostEqual(st["bills_monthly"], want, delta=0.01,
                                   msg=str(today))
            self.assertAlmostEqual(
                budget.month_status(self.conn, today)["avg_bills"], want,
                delta=0.01, msg=str(today))

    def test_a_weekly_bill_that_ends_this_year_still_counts_only_its_run(self):
        """An end date inside the year keeps the count over what is left of
        the series: the exact rate is for a cycle that runs all year."""
        add_bill(self.conn, "Pool Service", 100.0,
                 frequency="WEEKLY", interval=1,
                 next_due=dt.date(2026, 1, 1),
                 last_seen=dt.date(2025, 12, 25))
        bill = budget._recurring_bills(self.conn)[0]
        bill["recurrence"] = dict(bill["recurrence"], endOn="2026-02-28")
        load = budget.evened_monthly_load([bill], dt.date(2026, 1, 15))
        # Jan + Feb Thursdays only: 5 + 4
        self.assertAlmostEqual(load, 9 * 100.0 / 12, places=2)


if __name__ == "__main__":
    unittest.main()
