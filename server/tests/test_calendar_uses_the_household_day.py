"""The calendar strip runs on the household's day, like every other page.

Every live day-math site reads the household's configured timezone, never
the instance clock. Miss one endpoint and an evening request from a US
household on a UTC instance has its bill and card events computed and
labelled against tomorrow's date on that one screen, while Today, the
lenses and the bills list all agree on the local day.
"""

import datetime as dt
import unittest
import uuid
import zoneinfo
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config

# a fixed local moment far from the process clock, so a leak of
# date.today() into either the walk or the "start" field shows up on
# any day this suite runs
LOCAL = dt.datetime(2026, 7, 15, 21, 30,
                    tzinfo=zoneinfo.ZoneInfo("Pacific/Kiritimati"))


class CalendarTodayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"cal-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=0, other_monthly=0,
                         timezone="Pacific/Kiritimati")
        finally:
            conn.close()

    def test_calendar_starts_on_the_households_day(self):
        self.assertNotEqual(LOCAL.date(), dt.date.today())
        with mock.patch("oikonome.localtime.now_local", return_value=LOCAL):
            body = self.client.get("/api/calendar?days=35").json()
        self.assertEqual(body["start"], "2026-07-15")
        # the walk ran on the same day: the card with no due date is
        # scheduled "tomorrow" relative to the household, not the server
        by_date = {d["date"]: d for d in body["calendar"]}
        self.assertIn("2026-07-16", by_date)
        self.assertEqual([e["label"] for e in by_date["2026-07-16"]["events"]],
                         ["Test Card payment"])
        self.assertTrue(all(d >= "2026-07-15" for d in by_date))


if __name__ == "__main__":
    unittest.main()
