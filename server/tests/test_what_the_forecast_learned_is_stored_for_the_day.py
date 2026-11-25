"""What the forecast learned from history is stored per household per day
(forecast_learned; specs/today-figures.md, "What the forecast learned is
stored for the day"). The facts are a pure function of the ledger through
yesterday and the day, so every worker and every restart serves the same
forecast from one row: a process that only read the row builds exactly
what the process that learned it built. A write dated before today, or a
row learned by other rules, is learned again. Figures and names are
invented."""

import datetime as dt
import decimal
import json
import os
import subprocess
import sys
import time
import unittest
import uuid
from unittest import mock

from oikonome.engine import budget, forecast

from . import large_household
from .util import TEST_DB, TODAY, add_bill, add_txn, make_db, write_config

D = dt.date


def _as_json(f) -> str:
    return json.dumps(f, sort_keys=True, default=str)


def _household():
    conn = make_db()
    write_config(conn, food_monthly=600, other_monthly=600)
    d = TODAY - dt.timedelta(days=200)
    n = 0
    while d < TODAY:
        n += 1
        add_txn(conn, d, 20.0 + (n * 7) % 31, f"SHOP {n % 9}",
                primary="FOOD_AND_DRINK")
        if d.day == 1:
            add_txn(conn, d, 1200.0, "MAPLE RENTALS", account="chk")
        if d.day in (1, 15):
            add_txn(conn, d, -2100.0, "OAKFIELD PAYROLL", account="chk",
                    primary="INCOME", detailed="INCOME_WAGES")
        d += dt.timedelta(days=1)
    add_bill(conn, "MAPLE RENTALS", 1200.0, next_due=D(2026, 8, 1),
             last_seen=D(2026, 7, 1))
    return conn


def _tenant(conn) -> str:
    return forecast._tenant(conn)


def _row(conn, day=TODAY):
    return conn.execute(
        "SELECT rule_version, stamp, written_by, facts::text AS facts "
        "FROM forecast_learned WHERE as_of = %s", (day,)).fetchone()


def _dsn() -> str:
    return os.environ.get(
        "OIKONOME_TEST_DSN",
        f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")


# run from server/ with -c, so this checkout's package is the one imported
_CHILD = "\n".join((
    "import datetime as dt, json, sys",
    "from unittest import mock",
    "from oikonome.db import tenancy",
    "from oikonome.engine import forecast",
    "tid, dsn, day, only_read = sys.argv[1:5]",
    "conn = tenancy.tenant_connect(tid, dsn)",
    "if only_read == '1':",
    "    # a process that only reads the row: learning anything fails",
    "    for name in ('forecast_pace', 'heavy_month_factor', 'card_timing',",
    "                 'card_payday_habit', 'bill_payments',",
    "                 'bill_paying_accounts'):",
    "        mock.patch.object(forecast, name,",
    "                          side_effect=AssertionError(name)).start()",
    "f = forecast.build(conn, today=dt.date.fromisoformat(day))",
    "sys.stdout.write(json.dumps(f, sort_keys=True, default=str))",
))


def _in_a_fresh_process(tid: str, day: dt.date, only_read: bool) -> str:
    server = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run(
        [sys.executable, "-W", "ignore", "-c", _CHILD, tid, _dsn(),
         day.isoformat(), "1" if only_read else "0"],
        cwd=server, capture_output=True, text=True, timeout=300)
    if out.returncode:
        raise AssertionError(out.stderr[-3000:])
    return out.stdout


class EveryProcessServesTheSameForecast(unittest.TestCase):
    def test_a_process_that_only_read_the_row_builds_the_same_bytes(self):
        conn = _household()
        try:
            tid = _tenant(conn)
            conn.execute("DELETE FROM forecast_learned")
            learned = _in_a_fresh_process(tid, TODAY, only_read=False)
            row = _row(conn)
            self.assertIsNotNone(row, "the build that learned stored nothing")
            self.assertEqual(row["rule_version"],
                             forecast.LEARNED_RULE_VERSION)
            read = _in_a_fresh_process(tid, TODAY, only_read=True)
            # and this process, learning again from the ledger
            forecast.forget_learned()
            conn._memo = None
            here = _as_json(forecast.build(conn, today=TODAY))
        finally:
            conn.close()
        self.assertEqual(read, learned)
        self.assertEqual(here, learned)


def _cold_worker():
    """This worker holds nothing (the stored rows stay readable)."""
    with forecast._LEARNED_LOCK:
        forecast._HISTORY.clear()
        forecast._BUILT.clear()
        forecast._CLOSED_ROWS.clear()


class WhatIsStoredIsReadAgain(unittest.TestCase):
    def setUp(self):
        self.conn = _household()
        forecast.forget_learned()

    def tearDown(self):
        self.conn.close()

    def _spy_build(self, day=TODAY):
        """A build in a cold worker, and the facts it had to learn."""
        _cold_worker()
        self.conn._memo = None
        seen = []
        real = forecast.heavy_month_factor

        def spy(*a, **k):
            seen.append("heavy")
            return real(*a, **k)
        with mock.patch.object(forecast, "heavy_month_factor", spy):
            f = forecast.build(self.conn, today=day)
        return f, seen

    def _as_another_writer(self):
        # the rows as another process wrote them (this process's
        # forget_learned sets its own earlier rows aside)
        self.conn.execute("UPDATE forecast_learned SET written_by = ''")

    def test_a_cold_worker_reads_the_row_and_learns_nothing(self):
        first, seen = self._spy_build()
        self.assertEqual(seen, ["heavy"])
        self._as_another_writer()
        again, seen = self._spy_build()
        self.assertEqual(seen, [])
        self.assertEqual(_as_json(again), _as_json(first))

    def test_a_write_dated_before_today_learns_again(self):
        self._spy_build()
        self._as_another_writer()
        before = _row(self.conn)["stamp"]
        add_txn(self.conn, TODAY - dt.timedelta(days=3), 64.0, "SHOP 4",
                primary="FOOD_AND_DRINK", txn_id="back-dated")
        f, seen = self._spy_build()
        self.assertEqual(seen, ["heavy"])
        self.assertNotEqual(_row(self.conn)["stamp"], before)
        forecast.forget_learned()
        self.conn._memo = None
        self.assertEqual(_as_json(f),
                         _as_json(forecast.build(self.conn, today=TODAY)))

    def test_a_write_dated_today_keeps_the_row(self):
        self._spy_build()
        self._as_another_writer()
        before = _row(self.conn)["stamp"]
        add_txn(self.conn, TODAY, 18.0, "SHOP 2", primary="FOOD_AND_DRINK",
                txn_id="today-charge")
        _f, seen = self._spy_build()
        self.assertEqual(seen, [])
        self.assertEqual(_row(self.conn)["stamp"], before)

    def test_a_send_stamp_in_the_settings_keeps_the_row(self):
        self._spy_build()
        self._as_another_writer()
        before = _row(self.conn)["stamp"]
        # what a delivery, a mute or a milestone mail writes
        with budget.config_txn(self.conn) as cfg:
            cfg["savings_goal_state"] = {"Rainy day": {"milestone": 50}}
            cfg["email_muted"] = ["someone@example.com"]
            cfg["email_send_hour_utc"] = 14
        kept, seen = self._spy_build()
        self.assertEqual(seen, [])
        self.assertEqual(_row(self.conn)["stamp"], before)
        # and what it kept is what learning again gives
        forecast.forget_learned()
        self.conn._memo = None
        self.assertEqual(_as_json(kept),
                         _as_json(forecast.build(self.conn, today=TODAY)))
        # a budget the pace reads is an input: learned again
        with budget.config_txn(self.conn) as cfg:
            cfg["food_monthly"] = 650
        _f, seen = self._spy_build()
        self.assertEqual(seen, ["heavy"])

    def test_a_row_learned_by_other_code_is_not_read(self):
        """Any deploy learns once again: a row carries the code that
        learned it (forecast.code_identity)."""
        self._spy_build()
        self._as_another_writer()
        with mock.patch.object(forecast, "_CODE_IDENTITY", "v9.9.9-other"):
            _f, seen = self._spy_build()
            self.assertEqual(seen, ["heavy"])
            self.assertTrue(_row(self.conn)["stamp"].endswith(
                "|v9.9.9-other"))
        # back on this code: that row is another code's, learned again
        self._as_another_writer()
        _f, seen = self._spy_build()
        self.assertEqual(seen, ["heavy"])

    def test_the_code_identity_is_the_release_or_the_source(self):
        with mock.patch.object(forecast, "_CODE_IDENTITY", None), \
             mock.patch.dict(os.environ, {"OIKONOME_VERSION": "v1.2.3-4-gabc"}):
            self.assertEqual(forecast.code_identity(), "v1.2.3-4-gabc")
        for word in ("dev", "unknown", "v1.2.3-dirty", ""):
            with mock.patch.object(forecast, "_CODE_IDENTITY", None), \
                 mock.patch.dict(os.environ, {"OIKONOME_VERSION": word}):
                got = forecast.code_identity()
            self.assertRegex(got, r"\+src:[0-9a-f]{16}$", word)
            self.assertTrue(got.startswith((word or "dev") + "+"), got)

    def test_a_table_not_migrated_yet_still_forecasts(self):
        """A new app running before its migration: every read and write of
        the stored row fails, and the forecast is built all the same."""
        import psycopg

        class NoTable:
            def __init__(self, c):
                self._c = c

            def execute(self, q, *a, **k):
                if "forecast_learned" in str(q):
                    raise psycopg.errors.UndefinedTable(
                        'relation "forecast_learned" does not exist')
                return self._c.execute(q, *a, **k)

            def __getattr__(self, k):
                return getattr(self._c, k)
        _cold_worker()
        self.conn._memo = None
        f = forecast.build(NoTable(self.conn), today=TODAY)
        self.assertIn("pace_usual", f)
        forecast.forget_learned()
        self.conn._memo = None
        self.assertEqual(_as_json(f),
                         _as_json(forecast.build(self.conn, today=TODAY)))

    def test_a_write_landing_mid_build_is_not_stored_under_the_old_stamp(
            self):
        """A bill edited from another connection while the facts are being
        learned: the stamp read before is the old history's, so the facts
        (perhaps the new history's) are not stored under it."""
        from oikonome.db import tenancy
        from .util import TEST_DB
        tid = _tenant(self.conn)
        other = tenancy.tenant_connect(tid, os.environ.get(
            "OIKONOME_TEST_DSN",
            f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}"))
        real = forecast.heavy_month_factor

        def edit_meanwhile(*a, **k):
            other.execute("UPDATE bills SET amount = amount - 1 "
                          "WHERE payee = 'MAPLE RENTALS'")
            return real(*a, **k)
        try:
            _cold_worker()
            self.conn._memo = None
            with mock.patch.object(forecast, "heavy_month_factor",
                                   edit_meanwhile):
                forecast.build(self.conn, today=TODAY)
        finally:
            other.close()
        self.assertIsNone(_row(self.conn))
        # the next build learns under the new history and stores it
        _f, seen = self._spy_build()
        self.assertEqual(seen, ["heavy"])
        self.assertIsNotNone(_row(self.conn))

    def test_a_row_learned_by_other_rules_is_not_read(self):
        self._spy_build()
        self._as_another_writer()
        self.conn.execute("UPDATE forecast_learned SET rule_version = %s",
                          (forecast.LEARNED_RULE_VERSION - 1,))
        _f, seen = self._spy_build()
        self.assertEqual(seen, ["heavy"])
        self.assertEqual(_row(self.conn)["rule_version"],
                         forecast.LEARNED_RULE_VERSION)

    def test_an_unreadable_row_is_learned_again(self):
        first, _seen = self._spy_build()
        self._as_another_writer()
        self.conn.execute(
            """UPDATE forecast_learned SET facts = '{"pace": {"q": 1}}'""")
        again, seen = self._spy_build()
        self.assertEqual(seen, ["heavy"])
        self.assertEqual(_as_json(again), _as_json(first))

    def test_a_new_day_with_its_row_learns_nothing(self):
        tomorrow = TODAY + dt.timedelta(days=1)
        self._spy_build(tomorrow)           # the nightly step's build
        self._as_another_writer()
        _f, seen = self._spy_build(tomorrow)
        self.assertEqual(seen, [])

    def test_only_the_newest_days_are_kept(self):
        for k in range(6):
            self._spy_build(TODAY + dt.timedelta(days=k))
        days = [r["as_of"] for r in self.conn.execute(
            "SELECT as_of FROM forecast_learned ORDER BY as_of").fetchall()]
        newest = TODAY + dt.timedelta(days=5)
        self.assertEqual(days[-1], newest)
        self.assertTrue(all(
            (newest - d).days <= forecast.LEARNED_KEEP_DAYS for d in days),
            days)


class TheNightlyStepStoresTheDay(unittest.TestCase):
    def test_the_step_builds_for_the_households_day(self):
        from oikonome import localtime
        from oikonome.db import tenancy
        from oikonome.jobs import worker
        conn = _household()
        try:
            tid = _tenant(conn)
            # the household's clock past the hour the night is kept at
            five = (lambda cfg: dt.datetime.now(localtime.tenant_tz(cfg))
                    .replace(hour=5, minute=0))
            day = five(budget.load_config(conn)).date()
            # tonight's detection has run (the step waits for it)
            from oikonome.engine import alerts as _alerts
            _alerts.heartbeat(conn, "nightly-detect", "")
            conn.execute("UPDATE job_runs SET ran_at = now() + interval "
                         "'1 day' WHERE job = 'nightly-detect'")
            real = tenancy.tenant_connect
            with mock.patch.object(worker.tenancy, "tenant_connect",
                                   side_effect=lambda t: real(t, _dsn())), \
                 mock.patch.object(localtime, "now_local", five):
                out = worker.forecast_tenant(tid)
            self.assertEqual(out.get("day"), day.isoformat(), out)
            self.assertIsNotNone(_row(conn, day))
            hb = conn.execute("SELECT note FROM job_runs WHERE job = "
                              "'forecast'").fetchone()
            self.assertEqual(hb["note"], day.isoformat())
        finally:
            conn.close()


class TheEncodingKeepsEveryType(unittest.TestCase):
    def test_round_trip(self):
        values = [
            None, True, False, 0, -3, 2 ** 70, 0.1, 1e-07, 1e16, -0.0,
            float("inf"), float("-inf"), "", "plain", "@at the start", "@@",
            D(2026, 7, 15), dt.datetime(2026, 7, 15, 4, 17, 0, 12),
            decimal.Decimal("12.30"), uuid.UUID(int=7),
            [1, (2, 3)], (D(2026, 1, 2), 4.5), {1, 2}, frozenset({"a"}),
            {"b": 1, "a": 2}, {D(2026, 1, 1): [("x", 1.5)], (1, 2): None},
            {"nested": {"k": [{"t": 1}, {"m": [1]}]}},
        ]
        for v in values:
            back = forecast.decode_learned(json.loads(json.dumps(
                forecast.encode_learned(v), allow_nan=False)))
            self.assertEqual(back, v, v)
            self.assertIs(type(back), type(v), v)
            if isinstance(v, float):
                self.assertEqual(repr(back), repr(v))
            if isinstance(v, dict):
                self.assertEqual(list(back), list(v))
        nan = forecast.decode_learned(forecast.encode_learned(float("nan")))
        self.assertNotEqual(nan, nan)

    def test_every_fact_a_large_household_learns_comes_back_equal(self):
        conn, _n = large_household.build(years=1)
        try:
            forecast.forget_learned()
            forecast.build(conn, today=TODAY)
            facts = forecast._HISTORY.get(_tenant(conn)).facts
            self.assertGreaterEqual(len(facts), 5)
            for name, v in facts.items():
                back = forecast.decode_learned(json.loads(json.dumps(
                    forecast.encode_learned(v), allow_nan=False)))
                self.assertEqual(back, v, name)
                self.assertEqual(_as_json(back), _as_json(v), name)
        finally:
            conn.close()


# a fresh process that may only read: it counts the month buckets and the
# learned readers it runs, and the build's wall time
_COLD_READER = "\n".join((
    "import datetime as dt, json, sys, time",
    "from oikonome.db import tenancy",
    "from oikonome.engine import budget, forecast",
    "tid, dsn, day = sys.argv[1:4]",
    "conn = tenancy.tenant_connect(tid, dsn)",
    "n = {'month_status': 0, 'learned': 0}",
    "real = budget.month_status",
    "def ms(*a, **k):",
    "    n['month_status'] += 1",
    "    return real(*a, **k)",
    "budget.month_status = ms",
    "for name in ('forecast_pace', 'heavy_month_factor', 'card_timing',",
    "             'card_payday_habit', 'bill_payments',",
    "             'bill_paying_accounts'):",
    "    f = getattr(forecast, name)",
    "    def w(*a, _f=f, **k):",
    "        n['learned'] += 1",
    "        return _f(*a, **k)",
    "    setattr(forecast, name, w)",
    "conn.execute('SELECT 1')",
    "t0 = time.perf_counter()",
    "forecast.build(conn, today=dt.date.fromisoformat(day))",
    "n['seconds'] = time.perf_counter() - t0",
    "sys.stdout.write(json.dumps(n))",
))


class ARestartedWorkerReadsTheRow(unittest.TestCase):
    """A process restart with a valid stored row learns nothing: no
    learned reader runs and only today's month is bucketed, inside
    COLD_BUDGET_S on the large household."""
    COLD_BUDGET_S = 0.4

    def test_a_fresh_process_reads_the_large_households_row(self):
        conn, _n = large_household.build()
        try:
            tid = _tenant(conn)
            conn.execute("DELETE FROM forecast_learned")
            _in_a_fresh_process(tid, TODAY, only_read=False)
            self.assertIsNotNone(_row(conn))
            server = os.path.dirname(os.path.dirname(os.path.abspath(
                __file__)))
            best = None
            for _ in range(2):
                out = subprocess.run(
                    [sys.executable, "-W", "ignore", "-c", _COLD_READER,
                     tid, _dsn(), TODAY.isoformat()],
                    cwd=server, capture_output=True, text=True, timeout=300)
                self.assertEqual(out.returncode, 0, out.stderr[-2000:])
                got = json.loads(out.stdout)
                self.assertEqual(got["learned"], 0, got)
                self.assertLessEqual(got["month_status"], 1, got)
                best = min(best or got["seconds"], got["seconds"])
            self.assertLess(best, self.COLD_BUDGET_S)
        finally:
            conn.close()


class ALargeHouseholdsDayIsOneRow(unittest.TestCase):
    """On the generated large household, a cold worker's build of a day
    whose row the nightly step wrote learns nothing and buckets only
    today's month; the time is bounded generously (the counts are the
    check)."""
    BUDGET_S = 1.5

    def test_a_new_day_and_a_cold_worker_read_the_row(self):
        conn, _n = large_household.build()
        try:
            tomorrow = TODAY + dt.timedelta(days=1)
            forecast.forget_learned()
            forecast.build(conn, today=TODAY)
            # the nightly step's build of tomorrow, in another worker
            conn._memo = None
            forecast.build(conn, today=tomorrow)
            conn.execute("UPDATE forecast_learned SET written_by = ''")
            for _ in range(2):
                _cold_worker()
                conn._memo = None
                calls = []
                real = budget.month_status

                def spy(*a, **k):
                    calls.append(a[1] if len(a) > 1 else k.get("today"))
                    return real(*a, **k)
                with mock.patch.object(budget, "month_status", spy), \
                     mock.patch.object(forecast, "card_timing",
                                       side_effect=AssertionError("learned")):
                    t0 = time.perf_counter()
                    forecast.build(conn, today=tomorrow, memo={})
                    took = time.perf_counter() - t0
                self.assertEqual(calls, [tomorrow], calls)
                self.assertLess(took, self.BUDGET_S)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
