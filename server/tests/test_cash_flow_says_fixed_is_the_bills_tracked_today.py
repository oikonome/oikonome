"""On a long Cash Flow window, Fixed is what the bills tracked TODAY
matched, and the page says so.

A window that reaches back before most of today's bills began has the
old rent and the loans since paid off in Variable, so Fixed reads as a
small share of what went out — with no word on any range but "all". The
split rule stays (the first bill's start is the one boundary); the
server says what Fixed means on such a window, in one sentence, and both
clients print it as sent."""

import datetime as dt
import pathlib
import unittest

from oikonome.engine import budget, reporting

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 17)
_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _months(first: dt.date, last: dt.date):
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        yield dt.date(y, m, 1)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


class TwoBillsBeganInMay(unittest.TestCase):
    """A gym membership paid since Nov 2023; rent and power, both bills,
    paid only from May 2025. Groceries and pay every month."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for d in _months(dt.date(2023, 11, 1), dt.date(2026, 11, 1)):
            add_txn(self.conn, d.replace(day=12), 400.0, "GROCER")
            add_txn(self.conn, d.replace(day=2), -5000.0, "NORTHWIND PAYROLL",
                    account="chk", primary="INCOME", detailed="INCOME_WAGES")
            add_txn(self.conn, d.replace(day=5), 30.0, "ACME GYM",
                    account="chk")
            if d >= dt.date(2025, 5, 1):
                add_txn(self.conn, d.replace(day=1), 1700.0, "FABRIKAM",
                        account="chk")
                add_txn(self.conn, d.replace(day=8), 90.0, "CITY POWER",
                        account="chk")
        add_bill(self.conn, "ACME GYM", 30.0, next_due=dt.date(2026, 12, 5))
        add_bill(self.conn, "FABRIKAM", 1700.0,
                 next_due=dt.date(2026, 12, 1))
        add_bill(self.conn, "CITY POWER", 90.0,
                 next_due=dt.date(2026, 12, 8))

    def tearDown(self):
        self.conn.close()

    def test_a_long_window_says_fixed_is_todays_bills(self):
        out = reporting.flow_breakdown(self.conn, TODAY, "3y")["out"]
        # the split itself is unchanged: nothing before the gym's start
        self.assertEqual(out["unsplit"], 0.0)
        self.assertEqual(
            out["fixed_basis_text"],
            "Fixed counts the bills you track today; 18 of these 36 months "
            "are from before most of them began.")

    def test_a_window_inside_the_bills_says_nothing(self):
        out = reporting.flow_breakdown(self.conn, TODAY, "1y")["out"]
        self.assertIsNone(out["fixed_basis_text"])

    def test_a_month_is_early_while_fewer_than_half_had_started(self):
        bills, envelopes = budget.live_schedule(self.conn)
        self.assertTrue(budget.month_mostly_unstarted(
            bills, envelopes, dt.date(2025, 4, 30)))
        self.assertFalse(budget.month_mostly_unstarted(
            bills, envelopes, dt.date(2025, 5, 31)))
        # an envelope never used is in no month's count
        self.assertFalse(budget.month_mostly_unstarted(
            [], [{"since": None}], dt.date(2025, 11, 30)))


class TheAllRangeCountsOnlyTheMonthsItSplits(TwoBillsBeganInMay):
    """The same household with two years of groceries before the gym:
    those months are the unsplit band, which says its own months."""

    def setUp(self):
        super().setUp()
        for d in _months(dt.date(2021, 11, 1), dt.date(2023, 10, 1)):
            add_txn(self.conn, d.replace(day=12), 400.0, "GROCER")

    def test_the_all_range_counts_the_other_months(self):
        f = reporting.flow_breakdown(self.conn, TODAY, "all")
        out = f["out"]
        self.assertEqual(out["unsplit"], 24 * 400.0)
        self.assertEqual(
            out["fixed_basis_text"],
            "Fixed counts the bills you track today; 18 of the other 36 "
            "months are from before most of them began.")


class BothClientsPrintTheSentenceAsSent(unittest.TestCase):
    def test_the_web_prints_it(self):
        web = (_ROOT / "webapp/src/pages/CashFlow.tsx").read_text()
        self.assertIn("{flow.out.fixed_basis_text}", web)

    def test_the_phone_prints_it_and_nothing_for_an_older_server(self):
        phone = (_ROOT / "mobile/src/app/cashflow.tsx").read_text()
        self.assertIn("{flow.out.fixed_basis_text}", phone)
        self.assertIn("older-server path: stands in for `fixed_basis_text`",
                      phone)


if __name__ == "__main__":
    unittest.main()
