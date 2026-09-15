"""Email scheduling: cadence due-logic (local-time, per-day
guards, legacy fallback) and the weekly/monthly lens-email builders."""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import budget
from oikonome.jobs import worker
from oikonome.web import lens_email

from .util import TODAY, add_txn, make_db, write_config


def _set_sched(conn, **cads):
    cfg = budget.load_config(conn)
    cfg["email_schedule"] = cads
    budget.save_config(conn, cfg)


def _utc(y, mo, d, h):
    return dt.datetime(y, mo, d, h, 5, tzinfo=dt.timezone.utc)


class DueTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _stamp(self, job, when):
        # simulate the worker sending + recording the run (what makes the
        # fresh() once-per-day guard hold between sweeps)
        self.conn.execute(
            "INSERT INTO job_runs (job, ran_at) VALUES (%s,%s) "
            "ON CONFLICT (tenant_id, job) DO UPDATE SET ran_at=EXCLUDED.ran_at",
            (job, when))

    def test_malformed_cadence_entries_never_crash_the_sweep(self):
        """A restore can plant non-dict cadence entries (the API validates
        writes; restore does not). The hourly sweep must skip them, not
        crash — a crash here silences EVERY cadence for the tenant."""
        _set_sched(self.conn, daily=True, weekly="x", monthly=7, yearly=[1])
        with mock.patch.dict("os.environ", {"OIKONOME_TZ": "UTC"}):
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 23)), [])

    def test_legacy_fallback_daily_utc(self):
        cfg = budget.load_config(self.conn)
        cfg["email_send_hour_utc"] = 14
        budget.save_config(self.conn, cfg)
        with mock.patch.dict("os.environ", {"OIKONOME_TZ": "UTC"}):
            # fires at the hour…
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 14)), ["daily"])
            self._stamp("daily-email", _utc(2026, 7, 15, 14))
            # …and NOT again later the same day (guard, not the ==-hour)
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 15)), [])

    def test_creation_day_holds_first_scheduled_email(self):
        """A tenant created 'today' gets NO scheduled email that day; the
        first fires the next local day. Without the hold, signing up
        mid-day fires the daily verdict within the hour."""
        _set_sched(self.conn, daily={"on": True, "hour": 7})
        tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.conn.execute(
            "INSERT INTO users (tenant_id, email, password_hash, created_at) "
            "VALUES (%s, %s, %s, %s)",
            (tid, f"owner-{tid}@example.test", "x", _utc(2026, 7, 15, 3)))
        with mock.patch.dict("os.environ", {"OIKONOME_TZ": "UTC"}):
            # same local day as creation → held (even past the 07:00 hour)
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 9)), [])
            # the next local day → the first daily fires
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 16, 7)), ["daily"])

    def test_cadences_fire_on_their_local_slots(self):
        _set_sched(self.conn,
                   daily={"on": True, "hour": 7},
                   weekly={"on": True, "hour": 8, "weekday": 2},  # Wednesday
                   monthly={"on": True, "hour": 9})
        with mock.patch.dict("os.environ", {"OIKONOME_TZ": "UTC"}):
            # chronological sweeps, stamping each cadence as it "sends"
            # (job_runs persists, so out-of-order dates would false-guard).
            # 2026-07-01 is a Wednesday AND the 1st → all three fire.
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 1, 7)), ["daily"])
            self._stamp("daily-email", _utc(2026, 7, 1, 7))
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 1, 8)), ["weekly"])
            self._stamp("weekly-email", _utc(2026, 7, 1, 8))
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 1, 9)), ["monthly"])
            self._stamp("monthly-email", _utc(2026, 7, 1, 9))
            self.assertEqual(   # all guarded the rest of the day
                worker.emails_due(self.conn, _utc(2026, 7, 1, 10)), [])
            # 2026-07-15 (Wed, not the 1st): daily + weekly, no monthly
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 7)), ["daily"])
            self._stamp("daily-email", _utc(2026, 7, 15, 7))
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 8)), ["weekly"])
            self._stamp("weekly-email", _utc(2026, 7, 15, 8))
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 20)), [])

    def test_off_cadence_never_fires_and_guard_dedupes(self):
        _set_sched(self.conn, daily={"on": False, "hour": 7},
                   weekly={"on": True, "hour": 7, "weekday": 2})
        with mock.patch.dict("os.environ", {"OIKONOME_TZ": "UTC"}):
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 7)), ["weekly"])
            # heartbeat stamped INSIDE the fake day → guarded
            self.conn.execute(
                "INSERT INTO job_runs (job, ran_at) VALUES "
                "('weekly-email', %s) ON CONFLICT (tenant_id, job) "
                "DO UPDATE SET ran_at=EXCLUDED.ran_at",
                (_utc(2026, 7, 15, 7),))
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 7)), [])

    def test_a_household_zone_beats_the_instance_zone(self):
        """A hosted instance runs on one clock and its households on many.
        With `timezone` set, the hour is kept in the household's zone: a
        9am Los Angeles verdict goes at 16:05 UTC, and the 09:05 UTC sweep
        — 2am on that coast — leaves it alone, whatever OIKONOME_TZ says."""
        cfg = budget.load_config(self.conn)
        cfg["email_schedule"] = {"daily": {"on": True, "hour": 9}}
        cfg["timezone"] = "America/Los_Angeles"
        budget.save_config(self.conn, cfg)
        with mock.patch.dict("os.environ", {"OIKONOME_TZ": "Etc/UTC"}):
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 9)), [])
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 16)), ["daily"])
            self._stamp("daily-email", _utc(2026, 7, 15, 16))
            # the day turns at the HOUSEHOLD's midnight (07:00 UTC), so the
            # next UTC morning is still "today" there
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 16, 5)), [])
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 16, 16)), ["daily"])

    def test_an_unloadable_household_zone_falls_back_to_the_instance(self):
        cfg = budget.load_config(self.conn)
        cfg["email_schedule"] = {"daily": {"on": True, "hour": 7}}
        cfg["timezone"] = "Mars/Olympus_Mons"
        budget.save_config(self.conn, cfg)
        with mock.patch.dict("os.environ",
                             {"OIKONOME_TZ": "America/Los_Angeles"}):
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 14)), ["daily"])

    def test_local_timezone_shift(self):
        _set_sched(self.conn, daily={"on": True, "hour": 7})
        # 14:05 UTC = 07:05 in Los Angeles (PDT, July)
        with mock.patch.dict("os.environ",
                             {"OIKONOME_TZ": "America/Los_Angeles"}):
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 14)), ["daily"])
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 7)), [])


class LensEmailTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_txn(self.conn, TODAY - dt.timedelta(days=7), 42.0, "LAST WEEK CAFE",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, (TODAY.replace(day=1) - dt.timedelta(days=3)),
                99.0, "LAST MONTH SHOP")

    def tearDown(self):
        self.conn.close()

    def test_weekly_builds(self):
        subject, plain, html = lens_email.build_weekly(self.conn, TODAY)
        self.assertIn("Week of", subject)
        self.assertIn("budget", subject.lower())
        self.assertIn("Day by day", html)
        self.assertIn("$", plain)

    def test_monthly_builds(self):
        subject, plain, html = lens_email.build_monthly(self.conn, TODAY)
        self.assertIn("report card", subject)
        self.assertIn("LAST MONTH SHOP", html)
        self.assertIn("$", plain)


class MissedHourCatchUpTests(unittest.TestCase):
    """A scheduled send whose hour was missed (downtime, DST jump)
    is still due at the next sweep — >= the hour, exactly once."""

    def test_catch_up_after_missed_hour(self):
        conn = make_db()
        try:
            budget.save_config(conn, {
                "food_monthly": 1, "other_monthly": 1,
                "email_schedule": {"daily": {"on": True, "hour": 7}}})
            tz = worker._tz()
            # worker's first sweep of the day is at LOCAL 20:00 — hour 7 was
            # missed (down / DST). Must still be due (>=), exactly once.
            local = dt.datetime.now(tz).replace(hour=20, minute=0, second=0,
                                                microsecond=0)
            now_utc = local.astimezone(dt.timezone.utc)
            self.assertIn("daily", worker.emails_due(conn, now_utc))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
