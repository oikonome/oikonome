"""Cash-flow windows and income coverage follow the rows they total.

Spend is summed over every account, cards included, so a spend window is
anchored on the first row of any account — not on the first checking row.
Income is known only from the bank feed's first month on, the rule the
Year and Month lenses follow; every other surface that states a net (the
Cash Flow by-year table, the cumulative line, the flow picture, the
ledger's month header) must call the months before it unknown too, or the
same year reads as a loss on one page and unknown on the next. And a
household whose ledger began this month has no complete month yet: its
ranges show the month so far, and say so, instead of nothing."""

import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 10, 9)


def _months(first: dt.date, last: dt.date):
    y, m = first.year, first.month
    while (y, m) <= (last.year, last.month):
        yield dt.date(y, m, 10)
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


class _Base(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def card(self, day, amount):
        add_txn(self.conn, day, amount, "GROCER", account="card",
                primary="FOOD_AND_DRINK")

    def pay(self, day, amount):
        add_txn(self.conn, day, -amount, "NORTHWIND PAYROLL", account="chk",
                primary="INCOME", detailed="INCOME_WAGES")


class SpendWindowAnchor(_Base):
    def test_card_history_older_than_checking_counts_in_total_and_pace(self):
        """Card rows from 2023-10, checking from 2024-08: the 3y window
        holds 36 months of card spend and must divide by 36, and 'All'
        must start at the first card row, not the first checking row."""
        for d in _months(dt.date(2023, 10, 1), dt.date(2026, 9, 1)):
            self.card(d, 3000.0)
        for d in _months(dt.date(2024, 8, 1), dt.date(2026, 9, 1)):
            self.pay(d, 5000.0)
        w = reporting.spending_window(self.conn, TODAY, "3y")
        self.assertEqual(w["total"], 108000.0)
        self.assertEqual(w["months"], 36)
        a = reporting.spending_window(self.conn, TODAY, "all")
        self.assertEqual(a["from"], "2023-10-01")
        self.assertEqual(a["total"], 108000.0)
        self.assertEqual(a["months"], 36)
        # income still anchors on the bank rows it sums
        i = reporting.income_window(self.conn, TODAY, "all")
        self.assertEqual(i["from"], "2024-08-01")

    def test_a_card_only_household_sees_all_its_history_on_all(self):
        for d in _months(dt.date(2025, 1, 1), dt.date(2026, 9, 1)):
            self.card(d, 100.0)
        a = reporting.spending_window(self.conn, TODAY, "all")
        self.assertEqual(a["from"], "2025-01-01")
        self.assertEqual(a["total"], 2100.0)


class FirstMonthHousehold(_Base):
    def test_a_ledger_that_began_this_month_shows_the_month_so_far(self):
        self.card(dt.date(2026, 10, 3), 120.0)
        self.pay(dt.date(2026, 10, 5), 2000.0)
        for key in ("1m", "1y", "all"):
            w = reporting.spending_window(self.conn, TODAY, key)
            self.assertTrue(w["live_month"], key)
            self.assertEqual(w["total"], 120.0, key)
            self.assertEqual(w["from"], "2026-10-01", key)
            self.assertEqual(w["to"], TODAY.isoformat(), key)
            self.assertEqual(w["label"], "Oct 2026", key)
            self.assertIsNone(w["prev_total"], key)
            self.assertEqual(w["months"], 1, key)
        i = reporting.income_window(self.conn, TODAY, "1y")
        self.assertTrue(i["live_month"])
        self.assertEqual(i["total"], 2000.0)
        f = reporting.flow_breakdown(self.conn, TODAY, "1y")
        self.assertTrue(f["live_month"])
        self.assertEqual(f["out"]["total"], 120.0)
        self.assertEqual(f["in"]["total"], 2000.0)

    def test_a_ledger_with_a_closed_month_keeps_complete_months(self):
        self.card(dt.date(2026, 9, 20), 50.0)
        self.card(dt.date(2026, 10, 3), 120.0)
        w = reporting.spending_window(self.conn, TODAY, "1y")
        self.assertFalse(w["live_month"])
        self.assertEqual(w["total"], 50.0)

    def test_an_empty_ledger_is_not_called_a_live_month(self):
        w = reporting.spending_window(self.conn, TODAY, "1y")
        self.assertFalse(w["live_month"])
        self.assertEqual(w["total"], 0)


class IncomeCoverageAgrees(_Base):
    """Card spend all of 2025, checking feed from 2025-06."""

    def setUp(self):
        super().setUp()
        for d in _months(dt.date(2025, 1, 1), dt.date(2026, 9, 1)):
            self.card(d, 1000.0)
        for d in _months(dt.date(2025, 6, 1), dt.date(2026, 9, 1)):
            self.pay(d, 3000.0)

    def test_the_year_the_feed_starts_is_unknown_in_the_by_year_table(self):
        rows = {r[0]: r for r in
                reporting.compute_cashflow(self.conn, TODAY)["savings_by_year"]}
        self.assertEqual(rows["2025"], ["2025", None, 12000.0, None, None])
        self.assertEqual(rows["2026"][1], 27000.0)
        self.assertEqual(rows["2026"][3], 18000.0)

    def test_the_cumulative_line_holds_flat_through_the_partial_year(self):
        line = dict(reporting._cumulative_savings(self.conn))
        self.assertEqual(line["2025"], 0.0)
        self.assertEqual(line["2026"], 18000.0)

    def test_the_flow_picture_starts_where_income_is_known(self):
        f = reporting.flow_breakdown(self.conn, TODAY, "3y")
        self.assertTrue(f["income_clipped"])
        self.assertEqual(f["from"], "2025-06-01")
        self.assertEqual(f["label"], "Jun 2025–Sep 2026")
        self.assertEqual(f["in"]["total"], 16 * 3000.0)
        self.assertEqual(f["out"]["total"], 16 * 1000.0)
        self.assertEqual(f["saved"], 32000.0)
        inside = reporting.flow_breakdown(self.conn, TODAY, "1y")
        self.assertFalse(inside["income_clipped"])

    def test_the_ledger_month_header_calls_a_pre_feed_month_unknown(self):
        before = reporting.month_flows(self.conn, 2025, 5)
        self.assertEqual(before, {"out": 1000.0, "in": None, "net": None})
        after = reporting.month_flows(self.conn, 2025, 7)
        self.assertEqual(after, {"out": 1000.0, "in": 3000.0, "net": 2000.0})


class FlowPictureWithoutKnownIncome(_Base):
    """A window with no month of known income must report income, saved
    and rate as unknown, never "$0 came in" and the whole spend lost."""

    def test_a_card_only_household_has_unknown_income(self):
        for d in _months(dt.date(2025, 1, 1), dt.date(2026, 9, 1)):
            self.card(d, 1000.0)
        f = reporting.flow_breakdown(self.conn, TODAY, "1y")
        self.assertTrue(f["income_unknown"])
        self.assertIsNone(f["in"]["total"])
        self.assertIsNone(f["saved"])
        self.assertIsNone(f["rate"])
        self.assertEqual(f["out"]["total"], 12000.0)

    def test_a_feed_newer_than_the_window_leaves_income_unknown(self):
        for d in _months(dt.date(2025, 1, 1), dt.date(2026, 9, 1)):
            self.card(d, 1000.0)
        self.pay(dt.date(2026, 10, 2), 3000.0)
        f = reporting.flow_breakdown(self.conn, TODAY, "1y")
        self.assertTrue(f["income_unknown"])
        self.assertIsNone(f["saved"])

    def test_a_window_with_known_income_is_not_flagged(self):
        for d in _months(dt.date(2025, 1, 1), dt.date(2026, 9, 1)):
            self.card(d, 1000.0)
            self.pay(d, 3000.0)
        f = reporting.flow_breakdown(self.conn, TODAY, "1y")
        self.assertFalse(f["income_unknown"])
        self.assertEqual(f["saved"], 24000.0)


if __name__ == "__main__":
    unittest.main()
