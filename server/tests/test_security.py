"""Security layer: origin-check middleware, rate limiting, alerts
dismiss/restore routes, doctor page."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web.security import RateLimiter

from .util import TODAY, _ensure_db, seed_accounts, write_config


class RateLimiterTests(unittest.TestCase):
    def test_sliding_window(self):
        rl = RateLimiter()
        for _ in range(3):
            self.assertTrue(rl.check(("r", "ip"), 3, 60))
        self.assertFalse(rl.check(("r", "ip"), 3, 60))
        # a different key is unaffected
        self.assertTrue(rl.check(("r", "other-ip"), 3, 60))


class WebSecurityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app
        cls.client = TestClient(app)
        cls.email = f"sec-{uuid.uuid4().hex[:8]}@example.dev"
        cls.client.post("/api/signup", data={
            "email": cls.email, "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_cross_origin_write_blocked(self):
        r = self.client.post("/api/login",
                             data={"email": self.email,
                                   "password": "correct-horse-battery"},
                             headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        # same-origin passes
        r = self.client.post("/api/login",
                             data={"email": self.email,
                                   "password": "correct-horse-battery"},
                             headers={"Origin": "http://testserver"})
        self.assertEqual(r.status_code, 200)
        # GETs are never origin-blocked
        r = self.client.get("/healthz",
                            headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 200)
        # an opaque "null" Origin (sandboxed iframe / data: / file:)
        # is present-but-unmatchable → blocked, not waved through
        r = self.client.post("/api/login",
                             data={"email": self.email,
                                   "password": "correct-horse-battery"},
                             headers={"Origin": "null"})
        self.assertEqual(r.status_code, 403)

    def test_no_origin_header_allowed(self):
        # a genuinely absent claim (curl, mobile app) still reaches the
        # handler — only PRESENT cross-origin claims are blocked
        from oikonome.web import security
        security._limiter._hits.clear()
        r = self.client.post("/api/login",
                             data={"email": self.email,
                                   "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200)
        security._limiter._hits.clear()

    def test_login_rate_limit(self):
        from oikonome.web import security
        # burn the window with failed logins from one IP
        for i in range(12):
            r = self.client.post("/api/login",
                                 data={"email": "nobody@example.dev",
                                       "password": "wrong-wrong-wrong"})
            if r.status_code == 429:
                break
        self.assertEqual(r.status_code, 429)
        security._limiter._hits.clear()          # don't poison other tests

    def test_alerts_dismiss_and_restore_routes(self):
        from oikonome.engine import alerts
        conn = tenancy.tenant_connect(self.tid)
        try:
            alerts.log(conn, [{"kind": "anomaly", "severity": "warn",
                               "message": "sec-test alert"}], TODAY)
        finally:
            conn.close()
        r = self.client.post("/alerts/dismiss",
                             data={"kind": "anomaly",
                                   "message": "sec-test alert",
                                   "back": "//evil.example"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/today")   # open-redirect safe
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute("SELECT dismissed FROM alerts_log "
                               "WHERE message='sec-test alert'").fetchone()
            self.assertEqual(row["dismissed"], 1)
        finally:
            conn.close()
        self.client.post("/alerts/restore",
                         data={"kind": "anomaly", "message": "sec-test alert"},
                         follow_redirects=False)
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute("SELECT dismissed FROM alerts_log "
                               "WHERE message='sec-test alert'").fetchone()
            self.assertEqual(row["dismissed"], 0)
        finally:
            conn.close()

    def test_doctor_page_and_bundle(self):
        # Jinja page retired → SPA; the checks come from the API
        r = self.client.get("/doctor", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/app/doctor")
        names0 = {c["name"] for c in
                  self.client.get("/api/doctor").json()["checks"]}
        self.assertIn("token encryption", names0)
        b = self.client.get("/api/doctor/bundle").json()
        names = {c["name"] for c in b["checks"]}
        self.assertIn("database", names)
        self.assertIn("ledger", names)
        self.assertNotIn("tok-test", str(b))     # no tokens in the bundle


if __name__ == "__main__":
    unittest.main()
