"""A month surface reads no month but its own for its pools
(specs/today-figures.md, "Cost").

A named pool line on Today, the Bills page and a Month lens prints only
what the month's own status holds, so yearly pools add no month-state
read to those pages: the counts are the pages' own (Today and the Bills
page read none, the running Month lens its one), with yearly pools as
without. A wall-time budget on the large household (tests/
large_household.py) with two yearly pools added, generous as the repo's
other budgets are: the counts are the exact check.
"""

from __future__ import annotations

import datetime as dt
import time
import unittest
from unittest import mock

from oikonome.web import api_bills, lenses, report, todayview

from . import large_household
from .util import TODAY, add_bill, add_txn


class _Kept:
    """The page's connection, left open when the door closes it."""

    def __init__(self, conn):
        self._c = conn

    def close(self):
        pass

    def __getattr__(self, k):
        return getattr(self._c, k)


class AMonthSurfaceReadsNoOtherMonth(unittest.TestCase):
    WARM_BUDGET_S = 4.0

    @classmethod
    def setUpClass(cls):
        cls.conn, _rows = large_household.build(years=1)
        for name, pool in (("Gift Nook", 1200.0), ("Trail Outfitters",
                                                   900.0)):
            add_bill(cls.conn, name, pool, bill_type="envelope",
                     frequency=None, interval=12, merchant=name.lower(),
                     show_today=True)
            for m in range(1, TODAY.month + 1):
                add_txn(cls.conn, dt.date(TODAY.year, m, 9),
                        pool / 5, name.upper())
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _pages(self):
        conn = self.conn
        st = report.gather(conn, TODAY)
        todayview.build_context(st)
        with mock.patch.object(api_bills, "_conn",
                               lambda _u: _Kept(conn)):
            api_bills.bills_list(user={"role": "viewer"}, month=None)
        lenses.month_summary(conn, TODAY.year, TODAY.month, today=TODAY)

    def _count(self, fn):
        calls = []
        real = lenses.month_state

        def spy(*a, **k):
            calls.append(a[1:3])
            return real(*a, **k)
        with mock.patch.object(lenses, "month_state", spy):
            t0 = time.perf_counter()
            fn()
            took = time.perf_counter() - t0
        return calls, took

    def test_yearly_pools_add_no_month_state_read(self):
        conn = self.conn
        self._pages()                         # warm
        today, t_today = self._count(
            lambda: todayview.build_context(report.gather(conn, TODAY)))

        def bills():
            with mock.patch.object(api_bills, "_conn",
                                   lambda _u: _Kept(conn)):
                api_bills.bills_list(user={"role": "viewer"}, month=None)
        bills_calls, t_bills = self._count(bills)
        month, t_month = self._count(lambda: lenses.month_summary(
            conn, TODAY.year, TODAY.month, today=TODAY))
        self.assertEqual(today, [])
        self.assertEqual(bills_calls, [])
        self.assertEqual(month, [(TODAY.year, TODAY.month)])
        for took in (t_today, t_bills, t_month):
            self.assertLess(took, self.WARM_BUDGET_S)
        print(f"\n[timing] warm, two yearly pools: today {t_today:.2f}s · "
              f"bills {t_bills:.2f}s · month {t_month:.2f}s", flush=True)


if __name__ == "__main__":
    unittest.main()
