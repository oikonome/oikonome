"""Today back-nav — /api/today/full?date=YYYY-MM-DD.

A past day renders that day's verdict/plan/buckets with gather()'s existing
historical notion (no forked math) while the NOW-facts are suppressed
(live=False): cash forecast, runway/headroom, and alerts come back
None/empty and nothing is written to the alert log. Dates clamp to
[first transaction date, real today].
"""

import datetime as dt
import unittest
import uuid

from .util import add_txn, seed_accounts, write_config

TODAY = dt.date.today()                    # API path uses the real today
# always a different month, pinned mid-month: on days 1–3 the engine
# deliberately collapses UNDER into ON BUDGET (EARLY_MONTH_FRAC), so a
# floating past day made this suite fail three days of every month
PAST = (TODAY - dt.timedelta(days=40)).replace(day=15)


class TodayBacknavTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        from .util import _ensure_db
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from fastapi.testclient import TestClient

        from oikonome.db import tenancy
        from oikonome.web.app import app
        cls.client = TestClient(app)
        r = cls.client.post("/api/signup",
                            data={"email": f"back-{uuid.uuid4().hex[:8]}@x.dev",
                                  "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=3000, other_monthly=3100)
            add_txn(conn, PAST, 100.0, "OLD SPEND")     # the ledger's floor
            add_txn(conn, TODAY, 42.5, "SAFEWAY",
                    primary="FOOD_AND_DRINK")
        finally:
            conn.close()

    def test_no_date_is_live_today(self):
        r = self.client.get("/api/today/full").json()
        self.assertEqual(r["date"], TODAY.isoformat())
        self.assertTrue(r["is_today"])
        self.assertEqual(r["real_today"], TODAY.isoformat())
        self.assertEqual(r["min_date"], PAST.isoformat())

    def test_past_day_verdict_without_now_facts(self):
        r = self.client.get("/api/today/full",
                            params={"date": PAST.isoformat()}).json()
        self.assertEqual(r["date"], PAST.isoformat())
        self.assertFalse(r["is_today"])
        # that day's verdict pair: the seeded $100 is the month's only
        # spend through PAST, deeply under the $6,100/mo pace
        self.assertEqual(r["variable_actual"], 100.0)
        self.assertEqual(r["verdict"], "UNDER BUDGET")
        self.assertEqual(r["day_of_month"], PAST.day)
        # now-facts are suppressed, not recomputed for a past date
        self.assertIsNone(r["forecast"])
        self.assertEqual(r["forecast_rows"], [])
        self.assertIsNone(r["runway"])
        self.assertIsNone(r["headroom"])
        self.assertEqual(r["alerts"], [])
        # the plan pane + money map inputs are still there
        self.assertIn("buckets", r)
        self.assertIn("plan_surplus", r)
        # that day's recent window (day-1 → day) holds the seeded txn
        self.assertEqual(len(r["recent"]), 1)

    def test_today_as_date_is_live(self):
        r = self.client.get("/api/today/full",
                            params={"date": TODAY.isoformat()}).json()
        self.assertTrue(r["is_today"])
        self.assertEqual(r["date"], TODAY.isoformat())

    def test_clamps_to_ledger_floor_and_real_today(self):
        r = self.client.get("/api/today/full",
                            params={"date": "1990-01-01"}).json()
        self.assertEqual(r["date"], PAST.isoformat())   # ledger floor
        self.assertFalse(r["is_today"])
        fut = (TODAY + dt.timedelta(days=5)).isoformat()
        r2 = self.client.get("/api/today/full", params={"date": fut}).json()
        self.assertEqual(r2["date"], TODAY.isoformat())  # ceiling: today
        self.assertTrue(r2["is_today"])

    def test_bad_date_is_400(self):
        self.assertEqual(self.client.get(
            "/api/today/full", params={"date": "nope"}).status_code, 400)

    def test_past_day_never_logs_alerts(self):
        from oikonome.db import tenancy
        tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            before = conn.execute(
                "SELECT COUNT(*) AS n FROM alerts_log").fetchone()["n"]
            self.client.get("/api/today/full",
                            params={"date": PAST.isoformat()})
            after = conn.execute(
                "SELECT COUNT(*) AS n FROM alerts_log").fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(after, before)
