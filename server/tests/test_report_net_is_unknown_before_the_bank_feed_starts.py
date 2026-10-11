"""A month's income is known only once a bank account feeds the ledger.

A household links a checking account mid-year next to a card whose
history reaches back to January. The months before the checking feed
starts have unknown income, not $0: counting them as $0 would show each
one's whole spend as a loss, and the report emails would add those losses
into a year-to-date net and a yearly headline ("you lost money") that are
not true. The Month lens hero, the Year lens cells, the monthly email's
year-to-date tile and the yearly email's headline and year strip must all
read the same rule.
"""

import datetime as dt
import unittest

from oikonome.web import lens_email, lenses

from .util import add_txn, make_db, write_config


def _seed(conn, first_bank_month: int) -> None:
    write_config(conn, food_monthly=800, other_monthly=1000)
    for m in range(1, 7):
        add_txn(conn, dt.date(2026, m, 10), 1000.0, "HARBOR GROCER",
                primary="FOOD_AND_DRINK")
    # the checking feed starts with an outflow and no deposit that month,
    # so that month is covered with $0 income
    add_txn(conn, dt.date(2026, first_bank_month, 12), 50.0, "CITY WATER",
            primary="RENT_AND_UTILITIES", account="chk")
    for m in range(first_bank_month + 1, 7):
        add_txn(conn, dt.date(2026, m, 1), -3000.0, "PAYROLL",
                primary="INCOME", account="chk")


class NetBeforeTheBankFeedTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_months_before_the_bank_feed_have_no_net(self):
        _seed(self.conn, first_bank_month=5)
        y = lenses.year_summary(self.conn, 2026, today=dt.date(2026, 7, 1))
        by_m = {c["m"]: c for c in y["cells"]}
        for m in range(1, 5):
            self.assertIsNone(by_m[m]["income"], f"month {m} income")
            self.assertIsNone(by_m[m]["net"], f"month {m} net")
        self.assertEqual(by_m[5]["income"], 0)
        self.assertIsNotNone(by_m[5]["net"])
        self.assertEqual(by_m[6]["income"], 3000)

    def test_the_month_hero_reads_the_same_rule_as_its_cell(self):
        _seed(self.conn, first_bank_month=5)
        today = dt.date(2026, 7, 1)
        april = lenses.month_summary(self.conn, 2026, 4, today=today)
        self.assertIsNone(april["income"]["actual"],
                          "before the feed starts income is unknown")
        may = lenses.month_summary(self.conn, 2026, 5, today=today)
        self.assertEqual(may["income"]["actual"], 0,
                         "a covered month with no deposits earned $0")

    def test_the_monthly_email_year_to_date_is_unknown(self):
        _seed(self.conn, first_bank_month=5)
        _subject, plain, _html = lens_email.build_monthly(
            self.conn, dt.date(2026, 7, 1))
        self.assertIn("Year to date net: —", plain)

    def test_the_yearly_email_does_not_say_the_year_lost_money(self):
        _seed(self.conn, first_bank_month=5)
        subject, plain, _html = lens_email.build_yearly(
            self.conn, dt.date(2027, 1, 1))
        self.assertNotIn("lost money", plain)
        self.assertNotIn(": net", subject)
        d = lenses.year_summary(self.conn, 2026, today=dt.date(2027, 1, 1),
                                with_detail=True)
        strip = {yr["y"]: yr for yr in d["detail"]["years"]}
        self.assertIsNone(strip[2026]["net"])

    def test_the_year_totals_do_not_count_uncovered_months_as_a_loss(self):
        """The Year page's totals read the cells' rule: a year whose bank
        feed starts mid-year has no known income, saved or savings rate,
        or `saved` would be the covered months' income against all twelve
        months' spend — a loss the cells and the yearly email deny."""
        _seed(self.conn, first_bank_month=5)
        y = lenses.year_summary(self.conn, 2026, today=dt.date(2027, 1, 1))
        t = y["totals"]
        self.assertIsNone(t["income"])
        self.assertIsNone(t["saved"])
        self.assertIsNone(t["rate"])
        # what went out is still known: six grocery months and the bill
        self.assertEqual(t["spend"], 6 * 1000 + 50)

    def test_a_year_the_bank_feeds_from_january_keeps_its_net(self):
        _seed(self.conn, first_bank_month=1)
        y = lenses.year_summary(self.conn, 2026, today=dt.date(2027, 1, 1),
                                with_detail=True)
        for c in y["cells"][:6]:
            self.assertIsNotNone(c["net"], f"month {c['m']}")
        ctx = lens_email.year_mail_context(y, 2026)
        self.assertIsNotNone(ctx["net"])
        strip = {yr["y"]: yr for yr in y["detail"]["years"]}
        self.assertIsNotNone(strip[2026]["net"])
        t = y["totals"]
        self.assertEqual(t["income"], 15000)
        self.assertEqual(t["saved"], 15000 - t["spend"])
        self.assertIsNotNone(t["rate"])


if __name__ == "__main__":
    unittest.main()
