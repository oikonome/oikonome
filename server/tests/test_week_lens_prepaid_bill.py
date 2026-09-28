"""The week lens judges a bill's occurrence the way the month verdict does.

A bill due on the 1st that autopaid in the last days of the previous
month is settled on Today and in the month verdict (the candidate rows
reach back before the month and next month's occurrences join the
match). The week lens re-derives the same matching for the week's
months, so it must use the same window — or the week that straddles the
month boundary shows the paid rent as still awaiting.
"""

import datetime as dt
import unittest

from oikonome.web import lenses

from .util import add_bill, add_txn, make_db, write_config


class WeekLensPrepaidBillTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=3000, other_monthly=3100)

    def tearDown(self):
        self.conn.close()

    def test_a_bill_autopaid_before_its_month_is_not_awaiting(self):
        # Rent due the 1st, autopaid three days early; the week Mon Jul 27 –
        # Sun Aug 2 2026 holds the due date, viewed from Aug 3.
        add_bill(self.conn, "Northwind Rent", 2000.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 8, 1), last_seen=dt.date(2026, 7, 1))
        add_txn(self.conn, dt.date(2026, 7, 29), 2000.0, "NORTHWIND RENT",
                account="chk", primary="RENT_AND_UTILITIES")
        w = lenses.week_summary(self.conn, dt.date(2026, 7, 27),
                                today=dt.date(2026, 8, 3))
        fixed = w["buckets"]["fixed"]
        planned, awaiting = fixed["week_budget"], fixed["awaiting"]
        self.assertGreaterEqual(planned, 2000.0,
                                "the occurrence due this week is planned")
        self.assertEqual(awaiting, 0.0,
                         "paid three days early is settled, not awaiting")
