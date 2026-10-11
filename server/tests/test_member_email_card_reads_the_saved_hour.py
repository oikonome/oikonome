"""Every member's daily-email card reads the hour from /api/me, and it must
be the hour the worker sends at. Midnight is hour 0, a real choice the
Settings clients offer, so it may not be read as "unset" and replaced by the
7am default; only a value that is not an hour at all falls back."""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db


class MemberEmailCardHour(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"hour-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def _put_daily(self, daily):
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
            cfg["email_schedule"] = {"daily": daily}
            budget.save_config(conn, cfg)
        finally:
            conn.close()

    def test_midnight_reads_as_midnight(self):
        r = self.client.post("/api/settings", json={
            "email_schedule": {"daily": {"on": True, "hour": 0}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            self.client.get("/api/me").json()["daily_email"]["hour"], 0)

    def test_a_value_that_is_not_an_hour_reads_as_the_default(self):
        for bad in (None, "x", 31, -1):
            with self.subTest(bad=bad):
                self._put_daily({"on": True, "hour": bad})
                d = self.client.get("/api/me").json()["daily_email"]
                self.assertEqual(d["hour"], 7)
                self.assertTrue(d["on"])

    def test_the_rest_of_the_card_survives_a_bad_hour(self):
        self._put_daily({"on": False, "hour": "x"})
        self.assertFalse(
            self.client.get("/api/me").json()["daily_email"]["on"])


if __name__ == "__main__":
    unittest.main()
