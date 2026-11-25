"""The rows a merchant's history lists for a timeframe are the rows the
Spending tab's figure for that merchant was read over: both bound a range
key by one definition, `reporting.ledger_ranges` (specs/ledger-figures.md).
The history counted months back from today while the figure covered
complete months, so on a mid-month day the list held this month's charges
and missed the window's first weeks, and did not add up to the figure
clicked.
"""
import datetime as dt
import unittest

from oikonome.engine import bills, reporting

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 6, 15)
NAME = "Harbor Hardware"


class HistoryWindowIsTheFiguresWindow(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # one charge a month for fourteen months, then two this month
        day = dt.date(2025, 4, 3)
        amount = 20.0
        while day < TODAY:
            add_txn(self.conn, day, amount, "HARBOR HARDWARE", merchant=NAME,
                    primary="HOME_IMPROVEMENT")
            amount += 1.0
            day = (day.replace(day=1) + dt.timedelta(days=32)).replace(day=3)
        add_txn(self.conn, dt.date(2026, 6, 12), 500.0, "HARBOR HARDWARE",
                merchant=NAME, primary="HOME_IMPROVEMENT")
        # a second merchant, so the figure is not simply the whole window
        add_txn(self.conn, dt.date(2026, 3, 9), 70.0, "Grocer",
                primary="FOOD_AND_DRINK")

    def tearDown(self):
        self.conn.close()

    def _figure(self, key):
        w = reporting.spending_window(self.conn, TODAY, key)
        return next((r[1] for r in w["merchants"] if r[0] == NAME), 0.0)

    def _listed(self, key):
        h = bills.merchant_history(self.conn, NAME, today=TODAY, window=key)
        return round(sum(t["amount"] for t in h["txns"]), 2)

    def test_the_rows_listed_add_up_to_the_figure_clicked(self):
        for key in ("cur", "1m", "3m", "6m", "1y"):
            self.assertEqual(self._listed(key), self._figure(key), key)
        # and the two differ by window, so the equality is not vacuous
        self.assertNotEqual(self._figure("3m"), self._figure("1y"))

    def test_a_range_key_is_bounded_by_the_ledgers_own_ranges(self):
        r = reporting.ledger_ranges(self.conn, TODAY)["3m"]
        h = bills.merchant_history(self.conn, NAME, today=TODAY, window="3m")
        days = sorted(str(t["date"])[:10] for t in h["txns"])
        self.assertTrue(days)
        self.assertGreaterEqual(days[0], r["start"])
        self.assertLessEqual(days[-1], r["end"])
        # complete months: this month's charges are not in "3m"
        self.assertLess(days[-1], "2026-06-01")

    def test_all_is_the_whole_ledger_and_months_back_is_its_own_mode(self):
        every = bills.merchant_history(self.conn, NAME, today=TODAY,
                                       window="all")["txns"]
        self.assertEqual(len(every), 16)
        # a caller that asks for months back from today still gets them
        back = bills.merchant_history(self.conn, NAME, today=TODAY,
                                      months=3)["txns"]
        self.assertIn("2026-06-12", {str(t["date"])[:10] for t in back})


if __name__ == "__main__":
    unittest.main()
