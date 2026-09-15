"""The daily email's two shapes (email_schedule.daily.summary).

Summary = the verdict pane only — the hero card with the left-to-spend
tiles and pinned bills — for households that want the answer without the
report. Extensive (the default) is the full email exactly as before. Both
renders cut at the same boundary so the HTML and the plain text stay
mirrors of each other.
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.web import report

from .util import _ensure_db, add_bill, add_txn, make_db, write_config

# sections that belong to the EXTENSIVE email only
EXTENSIVE_MARKS = ("Monthly budget", "Cash timeline",
                   "Savings goals")


class SummaryRenderTests(unittest.TestCase):
    NOW = dt.date.today()          # gather() is historical off-month — see
                                   # the pinned-bill tests for the rationale

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Helper", 1600.0, bill_type="envelope",
                 frequency=None, interval=1, category="CHILD CARE",
                 match_category=True, show_today=True)
        add_txn(self.conn, self.NOW, 42.5, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        self.d = report.gather(self.conn, self.NOW)

    def tearDown(self):
        self.conn.close()

    def test_summary_is_the_verdict_pane_only(self):
        subject, plain, html = report.build(self.d, summary=True)
        # the pane is all there: verdict tiles + pinned bills
        self.assertIn("Left to spend today", html)
        self.assertIn("Pinned bills", html)
        self.assertIn("LEFT TO SPEND TODAY", plain)
        self.assertIn("Pinned bills:", plain)
        # and nothing of the report survives, in either render
        for mark in EXTENSIVE_MARKS:
            self.assertNotIn(mark, html, mark)
            self.assertNotIn(mark, plain, mark)
        self.assertIn("summary email", plain)

    def test_default_is_the_full_report(self):
        _, plain, html = report.build(self.d)
        self.assertIn("Cash timeline", html)
        self.assertNotIn("summary email", plain)

    def test_subject_is_the_same_either_way(self):
        s_full, _, _ = report.build(self.d)
        s_sum, _, _ = report.build(self.d, summary=True)
        self.assertEqual(s_full, s_sum)


class SummaryToggleApiTests(unittest.TestCase):
    """The Settings checkbox round-trips through /api/settings."""

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
            "email": f"sum-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def _save(self, daily):
        return self.client.post("/api/settings", json={
            "email_schedule": {"daily": daily}})

    def test_round_trip(self):
        r = self._save({"on": True, "hour": 7, "summary": True})
        self.assertEqual(r.status_code, 200, r.text)
        got = self.client.get("/api/settings").json()["email_schedule"]
        self.assertTrue(got["daily"]["summary"])
        # unchecking removes the key (extensive is the default, so the
        # stored config stays minimal)
        r = self._save({"on": True, "hour": 7, "summary": False})
        self.assertEqual(r.status_code, 200, r.text)
        got = self.client.get("/api/settings").json()["email_schedule"]
        self.assertNotIn("summary", got["daily"])

    def test_summary_is_daily_only(self):
        r = self.client.post("/api/settings", json={
            "email_schedule": {"daily": {"on": True, "hour": 7},
                               "weekly": {"on": True, "hour": 7,
                                          "weekday": 0, "summary": True}}})
        self.assertEqual(r.status_code, 200, r.text)
        got = self.client.get("/api/settings").json()["email_schedule"]
        self.assertNotIn("summary", got["weekly"])


if __name__ == "__main__":
    unittest.main()
