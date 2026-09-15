"""A browser's web-push subscription must not outlive the session
eviction that a password change or "sign out everywhere else" performs.

Web push carries the verdict text and alert titles; a browser whose
cookie was just revoked kept receiving them because nothing tied the
subscription to the session. Native tickles already followed the device
token's revocation.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PASSWORD = "correct-horse-battery"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.email = f"wp-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(admin, self.email)
            from oikonome.auth import passwords
            self.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (self.tid, self.email,
                 passwords.hash_password(PASSWORD))).fetchone()["id"]
            self.endpoints = [f"https://push.example/{uuid.uuid4().hex}"
                              for _ in range(2)]
            for ep in self.endpoints:
                admin.execute(
                    "INSERT INTO push_subscriptions (user_id, tenant_id, "
                    "endpoint, p256dh, auth) VALUES (%s, %s, %s, 'k', 'a')",
                    (self.uid, self.tid, ep))
        finally:
            admin.close()
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/login", data={"email": self.email,
                                             "password": PASSWORD},
                             follow_redirects=False)
        assert r.status_code == 303, r.text

    def _live(self) -> set[str]:
        admin = tenancy.admin_connect()
        try:
            return {r["endpoint"] for r in admin.execute(
                "SELECT endpoint FROM push_subscriptions WHERE user_id=%s",
                (self.uid,)).fetchall()}
        finally:
            admin.close()


class WebPushRevocationTests(_Base):
    def test_password_change_drops_every_subscription(self):
        r = self.client.post("/api/password/change", json={
            "current_password": PASSWORD, "new_password": "another-long-pw"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._live(), set())

    def test_sign_out_everywhere_else_keeps_only_this_browsers(self):
        keep = self.endpoints[0]
        r = self.client.post("/api/sessions/revoke", json={
            "all_others": True, "password": PASSWORD,
            "keep_push_endpoint": keep})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._live(), {keep})

    def test_kicking_one_other_session_drops_the_other_browsers_push(self):
        # a subscription is not tied to a session, so the kicked browser's
        # cannot be singled out — every one but this browser's goes
        from oikonome.auth import sessions
        from oikonome.web.app import _control_conn
        with _control_conn() as conn:
            sessions.create_session(conn, self.uid, self.tid,
                                    user_agent="Mozilla/5.0 other")
        other = [s for s in self.client.get("/api/sessions").json()["sessions"]
                 if not s["current"]][0]["id"]
        keep = self.endpoints[1]
        r = self.client.post("/api/sessions/revoke", json={
            "id": other, "password": PASSWORD, "keep_push_endpoint": keep})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._live(), {keep})

    def test_email_change_drops_the_other_browsers_push(self):
        keep = self.endpoints[0]
        r = self.client.post("/api/email/change", data={
            "new_email": f"wp-new-{uuid.uuid4().hex[:8]}@x.dev",
            "password": PASSWORD, "keep_push_endpoint": keep})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._live(), {keep})

    def test_sign_out_everywhere_else_without_an_endpoint_drops_all(self):
        r = self.client.post("/api/sessions/revoke", json={
            "all_others": True, "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._live(), set())


if __name__ == "__main__":
    unittest.main()
