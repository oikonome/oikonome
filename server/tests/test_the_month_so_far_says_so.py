"""The month so far says so on every Cash Flow tab.

"This month" is the one window that runs to today. Its heading is the
server's "Nov 1–17 so far", it prints no per-month pace (one partial month
divided by one is only the total again), its comparison names the days it
is set against ("vs Oct 1–17", the same days of last month) and the Income
tab and the Overview name the paychecks still due this month when the
budget tracks one. A range of whole months keeps its label and says "vs
the year before"."""

import datetime as dt
import unittest

from oikonome.engine import reporting

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 17)


class TheMonthSoFar(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for back in range(1, 28):
            m = (TODAY.replace(day=1) - dt.timedelta(days=28 * back)
                 ).replace(day=5)
            add_txn(self.conn, m, 900.0, "GROCER")
            add_txn(self.conn, m, -3000.0, "NORTHWIND PAYROLL",
                    account="chk", primary="INCOME",
                    detailed="INCOME_WAGES")
        add_txn(self.conn, TODAY.replace(day=5), 400.0, "GROCER")
        add_txn(self.conn, TODAY.replace(day=5), -3000.0, "NORTHWIND PAYROLL",
                account="chk", primary="INCOME", detailed="INCOME_WAGES")

    def tearDown(self):
        self.conn.close()

    def test_this_month_is_headed_so_far_with_its_days(self):
        for fn in (reporting.spending_window, reporting.income_window,
                   reporting.flow_breakdown):
            w = fn(self.conn, TODAY, "cur")
            self.assertTrue(w["so_far"], fn.__name__)
            self.assertEqual(w["heading"], "Nov 1–17 so far", fn.__name__)

    def test_this_month_compares_with_the_same_days_of_last_month(self):
        for fn in (reporting.spending_window, reporting.income_window):
            w = fn(self.conn, TODAY, "cur")
            self.assertEqual(w["prev_text"], "vs Oct 1–17", fn.__name__)

    def test_a_range_of_months_keeps_its_label(self):
        w = reporting.spending_window(self.conn, TODAY, "1y")
        self.assertFalse(w["so_far"])
        self.assertEqual(w["heading"], w["label"])
        self.assertEqual(w["prev_text"], "vs the year before")
        self.assertIsNone(
            reporting.income_window(self.conn, TODAY, "1y")["income_due"])

    def test_the_paychecks_still_due_are_named(self):
        add_bill(self.conn, "NORTHWIND PAYROLL", 3000.0, income=True,
                 frequency="MONTHLY", interval=1,
                 next_due=dt.date(2026, 11, 25))
        for fn in (reporting.income_window, reporting.flow_breakdown):
            due = fn(self.conn, TODAY, "cur")["income_due"]
            self.assertEqual(due, {
                "amount": 3000.0,
                "text": "$3,000 in paychecks still due this month"},
                fn.__name__)


if __name__ == "__main__":
    unittest.main()
