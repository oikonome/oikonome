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
        s = {"daily": {"on": False, "hour": 9}, "monthly": {"on": True}}
        self.assertIs(effective_email_schedule({"email_schedule": s}), s)

    def test_an_old_utc_hour_is_published_as_the_local_hour(self):
        cfg = {"email_send_hour_utc": 14, "timezone": "America/Denver"}
        now = dt.datetime(2026, 7, 15, 3, tzinfo=dt.timezone.utc)
        self.assertEqual(effective_email_schedule(cfg, now)["daily"]["hour"], 8)

    def test_a_malformed_old_hour_falls_back_to_seven(self):
        with mock.patch.dict("os.environ", {"OIKONOME_TZ": "UTC"}):
            self.assertEqual(
                effective_email_schedule({"email_send_hour_utc": "x"})["daily"]["hour"], 7)


class SettingsRepliesAgreeTests(unittest.TestCase):
    """Clients seed their cached settings from the save reply, so the
    reply must publish the same effective schedule the read does. A reply
    carrying the raw (unsaved) value reads as "every email off", and the
    next cadence tap then writes the daily email off for real."""

    @classmethod
    def setUpClass(cls):
        import os
        import uuid

        from fastapi.testclient import TestClient

        from .util import _ensure_db
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"sched-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def test_a_save_reply_carries_the_schedule_the_read_does(self):
        got = self.client.get("/api/settings").json()["email_schedule"]
        self.assertEqual(got["daily"]["on"], True)
        r = self.client.post("/api/settings", json={"today_view": "advanced"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["email_schedule"], got)

    def test_saving_one_cadence_keeps_the_daily_email_the_worker_sends(self):
        """A body that names only the monthly card says nothing about the
        daily email, so the daily email the household gets stays on."""
        r = self.client.post("/api/settings", json={
            "email_schedule": {"monthly": {"on": True, "hour": 8}}})
        self.assertEqual(r.status_code, 200, r.text)
        sched = self.client.get("/api/settings").json()["email_schedule"]
        self.assertEqual(sched["daily"], {"on": True, "hour": 7})
        self.assertTrue(sched["monthly"]["on"])
        # an explicit off is still an off
        self.client.post("/api/settings", json={
            "email_schedule": {"daily": {"on": False, "hour": 7}}})
        sched = self.client.get("/api/settings").json()["email_schedule"]
        self.assertFalse(sched["daily"]["on"])
        self.assertTrue(sched["monthly"]["on"])
        # a weekly entry from before the report was retired is dropped
        r = self.client.post("/api/settings", json={
            "email_schedule": {"weekly": {"on": True, "hour": 8, "weekday": 0}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("weekly",
                         self.client.get("/api/settings").json()["email_schedule"])
        self.client.post("/api/settings", json={"email_schedule": None})


if __name__ == "__main__":
    unittest.main()
