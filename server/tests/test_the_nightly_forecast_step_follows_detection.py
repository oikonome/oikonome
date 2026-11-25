"""The forecast's nightly step keeps its record after the night's bill
detection, never costs a household its nightly, and finds the due
households on the same clock it then keeps.

- A household whose early morning falls just before the instance's nightly
  run (one on the instance's own clock, the usual self-host case) waits
  for that night's detection, so the night it keeps is graded against the
  schedule the night found, not the one before.
- The step has its own lock: a nightly reaching a household mid-build runs
  its detection, freeze and sweeps as ever, rather than skipping the
  household's whole night; the step is the one that yields to a nightly.
- The due check reads each household's day in the zone the step keeps it
  in, so an instance whose zone is only a process abbreviation (unset or
  mistyped OIKONOME_TZ) neither fails every tick nor reads a flipped clock.

Figures and names are invented.
"""

import datetime as dt
import os
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from oikonome import localtime
from oikonome.db import tenancy
from oikonome.engine import alerts, forecast
from oikonome.jobs import worker

from .test_the_forecast_keeps_what_it_said import _household
from .util import TEST_DB, TODAY, make_db

INSTANCE = "America/Chicago"


def _dsn():
    return os.environ.get(
        "OIKONOME_TEST_DSN",
        f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")


def _at(hour, minute):
    return dt.datetime(TODAY.year, TODAY.month, TODAY.day, hour, minute,
                       tzinfo=ZoneInfo(INSTANCE))


def _detected_at(conn, when):
    alerts.heartbeat(conn, "nightly-detect", "+0 proposals")
    conn.execute("UPDATE job_runs SET ran_at = %s WHERE job = 'nightly-detect'",
                 (when,))


def _kept(conn):
    return conn.execute("SELECT count(*) AS n FROM forecast_nights"
                        ).fetchone()["n"]


class _Clocked(unittest.TestCase):
    """A household on the instance's clock, the clock set by the test."""

    def setUp(self):
        self.conn = _household()
        self.addCleanup(self.conn.close)
        self.tid = forecast._tenant(self.conn)
        self.clock = {"at": _at(4, 3)}
        real = tenancy.tenant_connect
        for p in (mock.patch.dict(os.environ, {"OIKONOME_TZ": INSTANCE}),
                  mock.patch.object(worker.tenancy, "tenant_connect",
                                    side_effect=lambda t: real(t, _dsn())),
                  mock.patch.object(localtime, "now_local",
                                    lambda cfg: self.clock["at"].astimezone(
                                        localtime.tenant_tz(cfg)))):
            p.start()
            self.addCleanup(p.stop)


class TheRecordFollowsTheNightsDetection(_Clocked):
    def test_a_morning_just_before_the_nightly_waits_for_its_detection(self):
        _detected_at(self.conn, _at(4, 20) - dt.timedelta(days=1))
        out = worker.forecast_tenant(self.tid)
        self.assertEqual(out.get("_status"), "detection-pending", out)
        self.assertEqual(_kept(self.conn), 0)
        # the nightly reaches the household; the next tick keeps the night
        _detected_at(self.conn, _at(4, 19))
        self.clock["at"] = _at(4, 23)
        out = worker.forecast_tenant(self.tid)
        self.assertTrue(out.get("recorded"), out)

    def test_a_detect_sweep_that_outlived_its_run_is_still_waited_for(self):
        # the nightly's coroutine has ended (cancelled, say) while the
        # detect sweep goes on in the background: no counter of this
        # process says so, the household's own heartbeat does — inside
        # the give-up
        _detected_at(self.conn, _at(4, 20) - dt.timedelta(days=1))
        self.clock["at"] = _at(5, 33)
        with mock.patch.object(worker, "_NIGHTLY_RUNS", 0), \
             mock.patch.object(worker, "_step_threads_running", lambda: 0):
            out = worker.forecast_tenant(self.tid)
        self.assertEqual(out.get("_status"), "detection-pending", out)
        self.assertEqual(_kept(self.conn), 0)

    def test_a_nightly_still_queued_is_waited_for(self):
        # past the start, the nightly not begun anywhere yet
        _detected_at(self.conn, _at(4, 20) - dt.timedelta(days=1))
        self.clock["at"] = _at(4, 23)
        with mock.patch.object(worker, "_NIGHTLY_RUNS", 0):
            out = worker.forecast_tenant(self.tid)
        self.assertEqual(out.get("_status"), "detection-pending", out)

    def test_a_morning_far_from_the_nightly_keeps_its_night_at_once(self):
        # an hour no nightly is near: last night's detection is tonight's
        _detected_at(self.conn, _at(4, 20) - dt.timedelta(days=1))
        self.clock["at"] = _at(14, 3)
        out = worker.forecast_tenant(self.tid)
        self.assertTrue(out.get("recorded"), out)

    def test_a_household_whose_detection_failed_is_kept_after_the_give_up(
            self):
        # no detection heartbeat tonight: waited for until the give-up
        _detected_at(self.conn, _at(4, 20) - dt.timedelta(days=1))
        self.clock["at"] = (_at(4, 17) + worker.DETECTION_GIVE_UP
                            - dt.timedelta(minutes=5))
        self.assertEqual(worker.forecast_tenant(self.tid).get("_status"),
                         "detection-pending")
        self.clock["at"] = (_at(4, 17) + worker.DETECTION_GIVE_UP
                            + dt.timedelta(minutes=5))
        self.assertTrue(worker.forecast_tenant(self.tid).get("recorded"))


class TheStepNeverCostsANightly(_Clocked):
    def test_a_nightly_reaching_the_household_mid_build_runs(self):
        _detected_at(self.conn, _at(4, 19))
        self.clock["at"] = _at(4, 23)
        seen = []
        real = forecast.build

        def build(*a, **k):
            if not seen:           # the step's own build, not one the nightly makes
                seen.append(None)
                seen[0] = worker.nightly_tenant(self.tid)
            return real(*a, **k)
        with mock.patch.object(forecast, "build", build):
            out = worker.forecast_tenant(self.tid)
        self.assertTrue(out.get("recorded"), out)
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0].get("_status"), "already-running", seen)

    def test_the_step_yields_to_a_nightly_in_flight(self):
        _detected_at(self.conn, _at(4, 19))
        self.clock["at"] = _at(4, 23)
        holder = tenancy.tenant_connect(self.tid)
        try:
            holder.execute("SELECT pg_advisory_lock(hashtext(%s))",
                           (f"oikonome:nightly:{self.tid}",))
            out = worker.forecast_tenant(self.tid)
            self.assertNotIn("recorded", out)
            self.assertEqual(_kept(self.conn), 0)
        finally:
            tenancy.release_lock(holder, f"oikonome:nightly:{self.tid}")
            holder.close()
        self.assertTrue(worker.forecast_tenant(self.tid).get("recorded"))


class AnErasureWaitsForTheStep(unittest.TestCase):
    def test_the_steps_own_lock_is_a_writer_lock(self):
        """The step writes the household's learned forecast, its night and
        its marker: an erasure waits it out like every other writer."""
        import inspect
        from oikonome import erasure
        self.assertIn('f"oikonome:forecast:{tenant_id}"',
                      inspect.getsource(worker.forecast_tenant))
        self.assertIn("oikonome:forecast:{}", erasure.WRITER_LOCKS)


class TheDueCheckKeepsTheStepsClock(unittest.TestCase):
    def test_an_instance_zone_known_only_by_its_abbreviation(self):
        conn = make_db()
        self.addCleanup(conn.close)
        tid = forecast._tenant(conn)
        conn.commit()
        for tz in ("Asia/Jakarta", "America/Sao_Paulo", "Asia/Kolkata"):
            with self.subTest(tz=tz), mock.patch.dict(
                    os.environ, {"OIKONOME_TZ": "America/NewYork", "TZ": tz}):
                time.tzset()
                self.addCleanup(time.tzset)
                now = localtime.now_local({})
                want = [tid] if now.hour >= worker.FORECAST_LOCAL_HOUR else []
                admin = tenancy.admin_connect()
                try:
                    got = worker._due_forecast_tenants(admin, [tid])
                finally:
                    admin.close()
                self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
