"""The scheduled-mail due-check must survive a malformed schedule and a
household timezone change.

A schedule entry whose hour is not a number (a restore ZIP can plant one —
the settings door validates, an archive does not) must not raise out of the
whole due-check: that would silence EVERY cadence for the household on every
sweep with nothing but a worker log line to show for it.

The once-per-local-day guard must not compare the heartbeat against local
midnight in the zone in effect NOW. Moving the household to a zone far
enough ahead that "now" is already tomorrow's date would push that midnight
past the morning's send, and the next sweep would send the same day's
verdict again.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import alerts, budget
from oikonome.jobs import worker

from .util import make_db, write_config


def _utc(y, mo, d, h):
    return dt.datetime(y, mo, d, h, 5, tzinfo=dt.timezone.utc)


class CadenceGuardTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _cfg(self, **over):
        cfg = budget.load_config(self.conn)
        cfg.update(over)
        budget.save_config(self.conn, cfg)

    def test_a_malformed_hour_skips_that_cadence_only(self):
        self._cfg(email_schedule={
            "daily": {"on": True, "hour": "x"},
            "weekly": {"on": True, "hour": 7, "weekday": 2}})
        with mock.patch.dict("os.environ", {"OIKONOME_TZ": "UTC"}):
            # 2026-07-15 is a Wednesday (weekday 2): weekly is due, the
            # broken daily is skipped, and nothing raises
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 9)), ["weekly"])

    def test_an_out_of_range_weekday_never_fires(self):
        self._cfg(email_schedule={"weekly": {"on": True, "hour": 7,
                                             "weekday": 9}})
        with mock.patch.dict("os.environ", {"OIKONOME_TZ": "UTC"}):
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 9)), [])

    def test_a_malformed_legacy_hour_does_not_raise(self):
        cfg = budget.load_config(self.conn)
        cfg.pop("email_schedule", None)
        cfg["email_send_hour_utc"] = "noon"
        budget.save_config(self.conn, cfg)
        self.assertEqual(
            worker.emails_due(self.conn, _utc(2026, 7, 15, 20)), [])

    def test_a_zone_change_does_not_resend_the_same_local_day(self):
        self._cfg(timezone="America/Los_Angeles",
                  email_schedule={"daily": {"on": True, "hour": 7}})
        with mock.patch.dict("os.environ", {"OIKONOME_TZ": "UTC"}):
            # 07:05 Pacific on the 15th → due; the worker stamps the run in
            # the zone it reckoned the day in
            now = _utc(2026, 7, 15, 14)
            self.assertEqual(worker.emails_due(self.conn, now), ["daily"])
            self.conn.execute(
                "INSERT INTO job_runs (job, ran_at, zone) VALUES "
                "('daily-email', %s, 'America/Los_Angeles')", (now,))
            # 15:05 Pacific the same day the household moves to Tokyo,
            # where it is already 07:05 on the 16th — a "new" local day
            # manufactured out of eight real hours. Not due.
            self._cfg(timezone="Asia/Tokyo")
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 15, 22)), [])
            # the next real Tokyo morning (07:05 on the 17th) is due: both
            # the old zone and the new one agree the day has turned
            self.assertEqual(
                worker.emails_due(self.conn, _utc(2026, 7, 16, 22)), ["daily"])

    def test_heartbeat_records_the_zone(self):
        alerts.heartbeat(self.conn, "daily-email", "x", zone="Asia/Tokyo")
        row = self.conn.execute(
            "SELECT zone FROM job_runs WHERE job='daily-email'").fetchone()
        self.assertEqual(row["zone"], "Asia/Tokyo")


if __name__ == "__main__":
    unittest.main()
