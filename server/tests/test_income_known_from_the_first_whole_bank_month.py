"""A month's income is known only when the bank feed covers ALL of it.

Spend is summed from the 1st of every month, card rows imported years
earlier included. A checking feed whose first row lands on the 20th has
no record of the paychecks that came in on the 1st and the 15th, so
calling that month's income known sets nothing in against all of its
spend and reports a loss nobody made: the Month lens shows income $0 and
net -$4,200, the Year lens cell the same, and the Cash Flow picture opens
on the 1st of that month with its spend whole and its income missing.
Coverage is decided on the first imported day: the feed's first month
counts only when the feed opens on the 1st, and a year only when it opens
on Jan 1 (or earlier). Nothing posts on a weekend or a bank holiday, so a
feed that opened on the 1st can show its first row a few days later: a
first row on or before the month's second weekday counts as the 1st."""

import datetime as dt
import unittest

from oikonome.engine import reporting
from oikonome.web import lenses

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 10, 31)


def _seed(conn, first_bank_day: dt.date) -> None:
    write_config(conn, food_monthly=800, other_monthly=1000)
    # the card reaches back well before checking was linked
    d = dt.date(2025, 6, 5)
    while d < TODAY.replace(day=1):
        add_txn(conn, d, 4200.0, "HARBOR GROCER", primary="FOOD_AND_DRINK")
        d = (d + dt.timedelta(days=32)).replace(day=5)
    # checking opens mid-month: the paychecks of the 1st and the 15th of
    # that month were never imported
    add_txn(conn, first_bank_day, 60.0, "CITY WATER",
            primary="RENT_AND_UTILITIES", account="chk")
    m = (first_bank_day + dt.timedelta(days=32)).replace(day=1)
    while m < TODAY.replace(day=1):
        for day in (1, 15):
            add_txn(conn, m.replace(day=day), -2500.0, "NORTHWIND PAYROLL",
                    primary="INCOME", account="chk", detailed="INCOME_WAGES")
        m = (m + dt.timedelta(days=32)).replace(day=1)


class IncomeKnownFromTheFirstWholeBankMonthTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_a_feed_opening_mid_month_leaves_that_month_unknown(self):
        _seed(self.conn, dt.date(2026, 3, 20))
        march = lenses.month_summary(self.conn, 2026, 3, today=TODAY)
        self.assertIsNone(march["income"]["actual"],
                          "Mar 1-19 were never imported")
        april = lenses.month_summary(self.conn, 2026, 4, today=TODAY)
        self.assertEqual(april["income"]["actual"], 5000)
        y = lenses.year_summary(self.conn, 2026, today=TODAY)
        cells = {c["m"]: c for c in y["cells"]}
        self.assertIsNone(cells[3]["net"])
        self.assertIsNotNone(cells[4]["net"])

    def test_the_flow_picture_opens_on_the_first_whole_month(self):
        _seed(self.conn, dt.date(2026, 3, 20))
        f = reporting.flow_breakdown(self.conn, TODAY, "1y")
        self.assertEqual(f["from"], "2026-04-01")
        self.assertTrue(f["income_clipped"])
        # six whole months of pay against six months of spend
        self.assertEqual(round(f["in"]["total"]), 6 * 5000)
        self.assertEqual(round(f["out"]["total"]), 6 * 4200)

    def test_saved_by_month_has_no_loss_for_the_partial_month(self):
        _seed(self.conn, dt.date(2026, 3, 20))
        cov = reporting._income_coverage(self.conn)
        months = reporting._known_months(cov, "2026-10")
        self.assertNotIn("2026-03", months)
        self.assertEqual(months[0], "2026-04")

    def test_a_year_whose_feed_opens_after_jan_1_is_unknown(self):
        _seed(self.conn, dt.date(2026, 1, 20))
        cov = reporting._income_coverage(self.conn)
        self.assertFalse(reporting._income_known(cov, "2026-01"))
        self.assertTrue(reporting._income_known(cov, "2026-02"))

    def test_a_feed_opening_on_the_first_covers_its_month(self):
        _seed(self.conn, dt.date(2026, 3, 1))
        cov = reporting._income_coverage(self.conn)
        self.assertTrue(reporting._income_known(cov, "2026-03"))
        march = lenses.month_summary(self.conn, 2026, 3, today=TODAY)
        self.assertEqual(march["income"]["actual"], 0,
                         "a whole month covered with no deposit earned $0")

    def test_a_first_row_after_the_new_year_holiday_covers_the_year(self):
        # Jan 1 never posts: a feed from Jan 1 shows its first row on Jan 2
        _seed(self.conn, dt.date(2026, 1, 2))
        cov = reporting._income_coverage(self.conn)
        self.assertTrue(reporting._income_known(cov, "2026-01"))
        y = lenses.year_summary(self.conn, 2026, today=TODAY)
        self.assertIsNotNone({c["m"]: c for c in y["cells"]}[1]["net"])

    def test_a_first_row_after_a_leading_weekend_covers_its_month(self):
        # Mar 1 2026 is a Sunday: a feed from Mar 1 posts first on Mon/Tue
        _seed(self.conn, dt.date(2026, 3, 3))
        cov = reporting._income_coverage(self.conn)
        self.assertTrue(reporting._income_known(cov, "2026-03"))

    def test_a_first_row_past_the_second_weekday_leaves_the_month_unknown(self):
        _seed(self.conn, dt.date(2026, 3, 4))
        cov = reporting._income_coverage(self.conn)
        self.assertFalse(reporting._income_known(cov, "2026-03"))
        self.assertTrue(reporting._income_known(cov, "2026-04"))

    def test_a_feed_opening_on_the_16th_after_older_card_data_is_unknown(self):
        _seed(self.conn, dt.date(2025, 10, 16))
        cov = reporting._income_coverage(self.conn)
        self.assertEqual(cov[1], "2025-11")
        self.assertFalse(reporting._income_known(cov, "2025-10"))

    def test_a_card_opening_earlier_in_the_same_month_leaves_it_unknown(self):
        # Two institutions linked weeks apart: the card's history opens on
        # the 2nd, checking's on the 20th. Spend is summed from the 2nd but
        # the paychecks of the 1st and the 15th were never imported.
        write_config(self.conn, food_monthly=800, other_monthly=1000)
        add_txn(self.conn, dt.date(2024, 10, 2), 4200.0, "HARBOR GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2024, 10, 20), 60.0, "CITY WATER",
                primary="RENT_AND_UTILITIES", account="chk")
        add_txn(self.conn, dt.date(2024, 11, 1), -2500.0, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk", detailed="INCOME_WAGES")
        cov = reporting._income_coverage(self.conn)
        self.assertEqual(cov[1], "2024-11")
        self.assertFalse(reporting._income_known(cov, "2024-10"))
        october = lenses.month_summary(self.conn, 2024, 10, today=TODAY)
        self.assertIsNone(october["income"]["actual"])

    def test_a_whole_ledger_opening_mid_month_keeps_its_first_month(self):
        # No spend row predates the feed's first day: both sides are
        # measured from the 20th, so October stays known.
        write_config(self.conn, food_monthly=800, other_monthly=1000)
        add_txn(self.conn, dt.date(2024, 10, 20), 60.0, "CITY WATER",
                primary="RENT_AND_UTILITIES", account="chk")
        add_txn(self.conn, dt.date(2024, 10, 22), 90.0, "HARBOR GROCER",
                primary="FOOD_AND_DRINK")
        cov = reporting._income_coverage(self.conn)
        self.assertEqual(cov[1], "2024-10")


if __name__ == "__main__":
    unittest.main()
