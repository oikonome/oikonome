"""The Year page says what an ordinary month leaves, never a year total.

The year's money map printed "Also this month: $X excess stays in
checking" where X was the standing surplus times the budgeted months — a
figure no month's own page showed, under the words "this month", and
printed under a year that was over budget. The server now writes one
sentence per month ("An ordinary month leaves about $X") and none at all
under an over-budget year to date or a year with no data.
"""
import datetime as dt
import unittest

from oikonome.web import lenses

from .util import add_txn, freeze_month, make_db, write_config

TODAY = dt.date(2026, 7, 15)


class OrdinaryMonthLeavesTests(unittest.TestCase):
    def _year(self, june_spend):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn, food_monthly=800, other_monthly=1000,
                     budgeted_income_monthly=3000)
        for m in (5, 6):
            add_txn(conn, dt.date(2026, m, 1), -3000.0, "PAYROLL",
                    primary="INCOME", account="chk")
        add_txn(conn, dt.date(2026, 5, 10), 1500.0, "HARBOR GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(conn, dt.date(2026, 6, 10), june_spend, "HARBOR GROCER",
                primary="FOOD_AND_DRINK")
        freeze_month(conn, 2026, 5)
        freeze_month(conn, 2026, 6)
        return lenses.year_summary(conn, 2026, today=TODAY), conn

    def test_the_map_carries_no_year_excess_row(self):
        d, _ = self._year(1500.0)
        self.assertNotIn("Excess cash",
                         [r["label"] for r in d["plan_rows"] or []])

    def test_an_ordinary_month_is_one_months_surplus(self):
        d, _ = self._year(1500.0)
        self.assertIsNotNone(d["plan_surplus"])
        self.assertGreater(d["plan_surplus"], 0)
        self.assertEqual(
            d["excess_text"],
            f"An ordinary month leaves about ${round(d['plan_surplus']):,}")

    def test_an_over_budget_year_to_date_has_no_line(self):
        d, _ = self._year(3500.0)      # June over by more than May was under
        self.assertIsNone(d["excess_text"])

    def test_a_year_with_no_data_has_no_line(self):
        _, conn = self._year(1500.0)
        d = lenses.year_summary(conn, 2019, today=TODAY)
        self.assertIsNone(d["excess_text"])


if __name__ == "__main__":
    unittest.main()
