"""Every surface describes the same daily email the worker sends.

Settings, the welcome wizard, the member card and mobile Notifications all
read one effective schedule, and the worker sends by it: with no saved
schedule the daily email is on, at 07:00 household-local.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.notify.schedule import effective_email_schedule


class EffectiveScheduleTests(unittest.TestCase):
    def test_nothing_saved_is_daily_on_at_seven(self):
        self.assertEqual(effective_email_schedule({}),
                         {"daily": {"on": True, "hour": 7}})

    def test_a_saved_schedule_is_returned_as_is(self):
        s = {"daily": {"on": False, "hour": 9}, "weekly": {"on": True}}
        self.assertIs(effective_email_schedule({"email_schedule": s}), s)

    def test_an_old_utc_hour_is_published_as_the_local_hour(self):
        cfg = {"email_send_hour_utc": 14, "timezone": "America/Denver"}
        now = dt.datetime(2026, 7, 15, 3, tzinfo=dt.timezone.utc)
        self.assertEqual(effective_email_schedule(cfg, now)["daily"]["hour"], 8)

    def test_a_malformed_old_hour_falls_back_to_seven(self):
        with mock.patch.dict("os.environ", {"OIKONOME_TZ": "UTC"}):
            self.assertEqual(
                effective_email_schedule({"email_send_hour_utc": "x"})["daily"]["hour"], 7)


if __name__ == "__main__":
    unittest.main()
