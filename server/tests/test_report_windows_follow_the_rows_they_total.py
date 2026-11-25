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
        # the Income tab reads the same window — the months before the
        # bank feed are in it, marked incomplete — and its pace still
        # divides by the months income is measured in
        i = reporting.income_window(self.conn, TODAY, "all")
        self.assertEqual(i["from"], "2023-10-01")
        self.assertEqual(i["incomplete_months"][0], "2023-10")
        # the feed's first row is Aug 10, so August itself is not whole
        self.assertEqual(i["incomplete_months"][-1], "2024-08")
        self.assertEqual(i["months"], 26)
        self.assertEqual(i["per_month"], 5000.0)
        # the heading names 36 months, so the /mo says what it is over;
        # the 3y range the income fills too reads the same
        self.assertEqual(i["heading"], "Oct 2023–Sep 2026")
        self.assertEqual(i["per_month_basis"],
                         "over the 26 months since the bank feed began")
        y = reporting.income_window(self.conn, TODAY, "3y")
        self.assertEqual((y["months"], y["total"], y["per_month"]),
                         (26, 26 * 5000.0, 5000.0))
        self.assertEqual(y["per_month_basis"], i["per_month_basis"])
        one = reporting.income_window(self.conn, TODAY, "1y")
        self.assertEqual((one["months"], one["per_month"]), (12, 5000.0))
        self.assertIsNone(one["per_month_basis"])

    def test_a_card_only_household_sees_all_its_history_on_all(self):
        for d in _months(dt.date(2025, 1, 1), dt.date(2026, 9, 1)):
            self.card(d, 100.0)
        a = reporting.spending_window(self.conn, TODAY, "all")
        self.assertEqual(a["from"], "2025-01-01")
        self.assertEqual(a["total"], 2100.0)


class SpendingAllStartsAtTheFirstCountedCharge(_Base):
    """Paychecks from 2024-02, the first card charge in 2024-07. Spending
    on 'All' opens where its per-month average divides from, so the
    heading, the total and the /mo describe one span; the Overview and
    Income tabs keep the window both ledgers open together."""

    def setUp(self):
        super().setUp()
        for d in _months(dt.date(2024, 2, 1), dt.date(2026, 9, 1)):
            self.pay(d, 4000.0)
        for d in _months(dt.date(2024, 7, 1), dt.date(2026, 9, 1)):
            self.card(d, 250.0)

    def test_heading_total_and_pace_read_one_span(self):
        a = reporting.spending_window(self.conn, TODAY, "all")
        self.assertEqual(a["from"], "2024-07-01")
        self.assertEqual(a["to"], "2026-09-30")
        self.assertEqual(a["heading"], "Jul 2024–Sep 2026")
        self.assertEqual(a["label"], "Jul 2024–Sep 2026")
        self.assertEqual(a["months"], 27)
        self.assertEqual(a["total"], 27 * 250.0)
        self.assertEqual(a["per_month"], 250.0)
        self.assertTrue(all(ym >= "2024-07" for ym in a["incomplete_months"]))

    def test_the_drill_reads_the_same_span(self):
        a = reporting.spending_window(self.conn, TODAY, "all")
        d = reporting.spending_window_ledger(self.conn, TODAY, "all")
        self.assertEqual((d["from"], d["to"], d["amount"]),
                         (a["from"], a["to"], a["total"]))
        self.assertEqual(d["label"], f"All spending · {a['heading']}")

    def test_the_other_tabs_and_ranges_keep_their_windows(self):
        self.assertEqual(
            reporting.income_window(self.conn, TODAY, "all")["from"],
            "2024-02-01")
        f = reporting.flow_breakdown(self.conn, TODAY, "all")
        self.assertEqual(f["from"], "2024-02-01")
        y = reporting.spending_window(self.conn, TODAY, "3y")
        self.assertEqual((y["from"], y["months"]), ("2023-10-01", 27))

    def test_a_range_longer_than_the_spending_says_what_mo_divides_by(self):
        """3y on a ledger whose charges began 27 months ago keeps its
        window and every figure, and says the /mo is over those months;
        a range the spending fills, and All, say nothing extra."""
        y = reporting.spending_window(self.conn, TODAY, "3y")
        self.assertEqual((y["heading"], y["months"], y["total"],
                          y["per_month"]),
                         ("Oct 2023–Sep 2026", 27, 27 * 250.0, 250.0))
        self.assertEqual(y["per_month_basis"],
                         "over the 27 months with spending")
        for key in ("1y", "all"):
            w = reporting.spending_window(self.conn, TODAY, key)
            self.assertIsNone(w["per_month_basis"], key)
        a = reporting.spending_window(self.conn, TODAY, "all")
        self.assertEqual((a["heading"], a["months"], a["per_month"]),
                         ("Jul 2024–Sep 2026", 27, 250.0))

    def test_one_month_with_spending_is_worded_singular(self):
        self.assertEqual(reporting.per_month_basis(1, 12),
                         "over the 1 month with spending")
        self.assertIsNone(reporting.per_month_basis(12, 12))


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
        # paid on the 1st: the feed covers June 2025 whole (opening later
        # in June would leave June itself unknown)
        for d in _months(dt.date(2025, 6, 1), dt.date(2026, 9, 1)):
            self.pay(d.replace(day=1), 3000.0)

    def test_the_year_the_feed_starts_saves_over_its_measured_months(self):
        rows = {r[0]: r for r in
                reporting.compute_cashflow(self.conn, TODAY)["savings_by_year"]}
        # out over all of 2025; the feed measures seven months, so saved
        # and the rate are those seven months' and the row is flagged
        # (specs/counted-figures.md, "one rule")
        self.assertEqual(rows["2025"],
                         ["2025", 21000.0, 12000.0, 14000.0, 66.7])
        self.assertEqual(
            reporting.compute_cashflow(self.conn, TODAY)
            ["savings_by_year_notes"]["2025"],
            "income incomplete — left over counts 7 of 12 months")
        self.assertEqual(reporting.compute_cashflow(self.conn, TODAY)
                         ["savings_by_year_months"]["2025"], [7, 12])
        self.assertEqual(rows["2026"][1], 27000.0)
        self.assertEqual(rows["2026"][3], 18000.0)

    def test_the_cumulative_line_counts_a_partial_years_measured_months(self):
        line = dict(reporting._cumulative_savings(self.conn))
        # June-December 2025, then 2026 on top
        self.assertEqual(line["2025"], 14000.0)
        self.assertEqual(line["2026"], 32000.0)

    def test_the_flow_picture_marks_the_months_before_income_is_known(self):
        """3y is Oct 2023–Sep 2026 on every tab; every month before June
        2025 has no bank feed, so it is counted and marked incomplete."""
        f = reporting.flow_breakdown(self.conn, TODAY, "3y")
        self.assertFalse(f["income_clipped"])
        self.assertEqual(f["from"], "2023-10-01")
        self.assertEqual(f["label"], "Oct 2023–Sep 2026")
        self.assertEqual(f["incomplete_months"][0], "2023-10")
        self.assertEqual(f["incomplete_months"][-1], "2025-05")
        self.assertEqual(len(f["incomplete_months"]), 20)
        self.assertEqual(f["in"]["total"], 16 * 3000.0)
        self.assertEqual(f["out"]["total"], 21 * 1000.0)
        # saved over the 16 months with bank data only
        self.assertEqual(f["saved"], 32000.0)
        self.assertEqual((f["saved_months_known"], f["saved_months_total"]),
                         (16, 36))
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
