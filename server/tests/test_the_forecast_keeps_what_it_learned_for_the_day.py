"""What the forecast learns from history (the heavier-month factor, each
card's timing, the payday habit, the bills' paying accounts and past
payments, the usual pace, each closed month's variable rows) moves only
with a write, so it is kept per household for the day under a stamp of
everything it reads (forecast.learned). A second build the same day reads
almost nothing; a write, or a new day, misses; a closed month is not
bucketed again on a new day; the Today page, the Bills calendar and the
Cash Flow tail share one build. Figures are invented."""

import datetime as dt
import time
import unittest
from unittest import mock

from oikonome.engine import budget, forecast

from . import large_household
from .util import TODAY, add_txn


class _Counted:
    """The connection, counting the statements run on it."""

    def __init__(self, conn):
        self._c = conn
        self.n = 0

    def execute(self, *a, **k):
        self.n += 1
        return self._c.execute(*a, **k)

    def __getattr__(self, k):
        return getattr(self._c, k)


class KeptForTheDay(unittest.TestCase):
    # generous, as the repo's other time budgets are: a loaded shard is
    # not a slow function, and the counts below are the exact check
    WARM_BUDGET_S = 1.0
    COLD_BUDGET_S = 6.0

    @classmethod
    def setUpClass(cls):
        cls.conn, cls.rows = large_household.build(years=1)

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def setUp(self):
        forecast.forget_learned()
        self.conn._memo = None

    def _build(self, today=TODAY, days=forecast.HORIZON_DAYS):
        self.conn._memo = None
        calls = []
        real = budget.month_status

        def spy(*a, **k):
            calls.append(a[1] if len(a) > 1 else k.get("today"))
            return real(*a, **k)
        counted = _Counted(self.conn)
        with mock.patch.object(budget, "month_status", spy):
            t0 = time.perf_counter()
            f = forecast.build(counted, today=today, days=days, memo={})
            took = time.perf_counter() - t0
        return f, calls, counted.n, took

    def test_a_second_build_the_same_day_reads_almost_nothing(self):
        self.assertGreater(self.rows, 10_000)
        _f, calls, _n, took = self._build()
        # each month once: today's status, the running month as of
        # yesterday, and the closed months of the heavier month's year
        # (kept across days after this)
        self.assertEqual(len(calls), len(set(calls)), calls)
        self.assertLessEqual(len(calls), 18, calls)
        self.assertLess(took, self.COLD_BUDGET_S)
        f2, calls2, n2, took2 = self._build()
        self.assertEqual(calls2, [])
        self.assertLessEqual(n2, 12, n2)
        self.assertLess(took2, self.WARM_BUDGET_S)
        self.assertIs(f2, _f)

    def test_a_write_misses(self):
        f1, _c, _n, _t = self._build()
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 25.0, "SHOP 9",
                txn_id="kept-write")
        try:
            f2, calls, _n2, _t2 = self._build()
            self.assertIsNot(f2, f1)
            self.assertTrue(calls)
        finally:
            self.conn.execute("DELETE FROM transactions WHERE id='kept-write'")

    def test_a_charge_today_reuses_what_the_history_taught(self):
        # a sync today changes no fact read from the days before it: the
        # heavier month, the usual pace and the bills' payments are reused
        self._build()
        add_txn(self.conn, TODAY, 18.0, "SHOP 4", txn_id="kept-today")
        try:
            real = forecast.heavy_month_factor
            seen = []

            def spy(*a, **k):
                seen.append(1)
                return real(*a, **k)
            with mock.patch.object(forecast, "heavy_month_factor", spy), \
                 mock.patch.object(forecast, "bill_payments",
                                   side_effect=AssertionError("re-read")):
                _f, calls, _n, took = self._build()
            self.assertEqual(seen, [])
            # the running month alone is bucketed again
            self.assertLessEqual(len(calls), 1, calls)
            self.assertLess(took, self.WARM_BUDGET_S)
        finally:
            self.conn.execute("DELETE FROM transactions WHERE id='kept-today'")

    def test_a_new_day_does_not_bucket_its_closed_months_again(self):
        self._build()
        _f, calls, _n, took = self._build(today=TODAY + dt.timedelta(days=1))
        # the running month, and the closed months whose bill matching
        # still reaches into it (read as of yesterday, so a new day reads
        # them again); never an older closed month
        reach = dt.date(TODAY.year, TODAY.month, 1) - dt.timedelta(days=70)
        self.assertTrue(all(d >= reach for d in calls), calls)
        self.assertLess(took, self.COLD_BUDGET_S)

    def test_the_calendar_and_the_cash_flow_tail_share_todays_build(self):
        from oikonome.engine import reporting
        from oikonome.web import api_today
        f1, _c, _n, _t = self._build()
        built = []
        real = forecast._build

        def spy(*a, **k):
            built.append(1)
            return real(*a, **k)

        class Kept:
            def __init__(self, c):
                self._c = c

            def __getattr__(self, k):
                return getattr(self._c, k)

            def close(self):
                pass
        self.conn._memo = None
        with mock.patch.object(forecast, "_build", spy), \
             mock.patch.object(api_today, "_conn",
                               return_value=Kept(self.conn)), \
             mock.patch.object(api_today, "_today", return_value=TODAY):
            api_today.calendar_api({"tenant_id": "x"}, days=35)
            reporting.compute_cashflow(self.conn, today=TODAY)
        self.assertEqual(built, [])
        self.assertIs(forecast.build(self.conn, today=TODAY), f1)

if __name__ == "__main__":
    unittest.main()


class AClosedMonthMissesOnEveryRowItReads(unittest.TestCase):
    """A closed month's variable rows read what reporting's own reach says
    the month reads — an annual envelope's whole calendar year — so a
    write anywhere in it misses."""

    def test_removing_januarys_charge_reaches_mays_rows(self):
        from .util import add_bill, make_db, write_config
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=600)
            add_bill(conn, "Quillfeather Hosting", 120.0,
                     bill_type="envelope", frequency=None, interval=12)
            add_txn(conn, dt.date(2026, 1, 10), 120.0, "QUILLFEATHER HOSTING",
                    txn_id="jan-hosting")
            add_txn(conn, dt.date(2026, 5, 20), 90.0, "QUILLFEATHER HOSTING",
                    txn_id="may-hosting")
            forecast.forget_learned()
            before = forecast._closed_month_rows(conn, 2026, 5, None)
            self.assertEqual([r["amount"] for r in before], [90.0])
            conn.execute("DELETE FROM transactions WHERE id='jan-hosting'")
            conn._memo = None
            after = forecast._closed_month_rows(conn, 2026, 5, None)
            fresh = budget.month_status(conn, dt.date(2026, 5, 31),
                                        closed=True)
            want = [r["amount"] for k in ("food", "other")
                    for r in fresh["buckets"][k]["rows"]]
            self.assertEqual([r["amount"] for r in after], want)
            self.assertEqual(want, [])
        finally:
            conn.close()


class WhatIsKeptIsBounded(unittest.TestCase):
    def test_households_idle_entries_and_closed_months_are_bounded(self):
        kept = forecast._Kept(3)
        for i in range(5):
            kept.put(f"t{i}", i)
        self.assertEqual(len(kept), 3)
        self.assertIsNone(kept.get("t0"))          # least recently used
        self.assertEqual(kept.get("t2"), 2)
        kept.put("t5", 5)                          # t3 is now the oldest
        self.assertIsNone(kept.get("t3"))
        with mock.patch.object(forecast, "KEPT_IDLE_S", -1.0):
            self.assertIsNone(kept.get("t2"))      # unused too long
        self.assertLessEqual(forecast.KEPT_HOUSEHOLDS, 1000)

    def test_only_the_months_a_build_reads_are_kept(self):
        from .util import make_db, write_config
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=600)
            forecast.forget_learned()
            y, m = 2024, 1
            for _ in range(forecast.CLOSED_KEPT_MONTHS + 6):
                forecast._closed_month_rows(conn, y, m, None)
                y, m = (y, m + 1) if m < 12 else (y + 1, 1)
            months = forecast._CLOSED_ROWS.get(forecast._tenant(conn))
        finally:
            conn.close()
        self.assertEqual(len(months), forecast.CLOSED_KEPT_MONTHS)
        self.assertNotIn((2024, 1), months)
