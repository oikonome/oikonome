"""The Yearly budget card says which months it covers.

The card's figures are the elapsed budgeted months only, but its title,
its bars and its links all said the year, so a reader took a few months'
spending for the whole year's. The server writes the caption.
"""
import datetime as dt
import unittest

from oikonome.web import lenses

from .util import add_txn, freeze_month, make_db, write_config

TODAY = dt.date(2026, 7, 15)


class YearlyBudgetCaptionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, food_monthly=800, other_monthly=1000)
        for m in (2, 3, 5, 6):
            add_txn(self.conn, dt.date(2026, m, 10), 400.0, "HARBOR GROCER",
                    primary="FOOD_AND_DRINK")

    def _caption(self, y=2026):
        return lenses.year_summary(self.conn, y, today=TODAY)["plan_caption"]

    def test_a_run_of_months_is_named_first_to_last(self):
        freeze_month(self.conn, 2026, 5)
        freeze_month(self.conn, 2026, 6)
        # May and June frozen, July the month in progress with a budget
        self.assertEqual(self._caption(),
                         "May–Jul, the months with a budget · Jul to date "
                         "(its cell above is projected to month end)")

    def test_months_with_gaps_are_listed(self):
        for m in (2, 5, 6):
            freeze_month(self.conn, 2026, m)
        self.assertEqual(self._caption(),
                         "Feb, May, Jun, Jul, the months with a budget · "
                         "Jul to date (its cell above is projected to "
                         "month end)")

    def test_a_closed_year_names_no_month_to_date(self):
        for m in (5, 6):
            freeze_month(self.conn, 2026, m)
        cap = lenses.year_summary(self.conn, 2026,
                                  today=dt.date(2027, 2, 1))["plan_caption"]
        self.assertNotIn("to date", cap)

    def test_a_year_with_no_map_has_no_caption(self):
        self.assertIsNone(self._caption(2019))


if __name__ == "__main__":
    unittest.main()
