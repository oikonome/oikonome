"""Someone already signed in can see, and stop, a factor-clearing recovery.

The cooling-off recovery is safe only if the real owner can stop it. The
notice tells them to sign in or use the mailed link; a person who is
already signed in makes no new sign-in, and the mailed link sits in the
inbox the request may have come from. So the account's own status says a
recovery is pending, and one deliberate click inside the app stops it —
for a view-only member too, whose factors are their own. Merely using an
open session does NOT stop it: an old tab left open by someone who really
has lost every factor must not cancel their own recovery by polling.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import passwords
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class PendingRecoveryInAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _user(self, role="owner"):
        email = f"pend-{uuid.uuid4().hex[:10]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, email)
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s, %s, %s, %s, now()) RETURNING id",
                (tid, email, passwords.hash_password(PW), role)
            ).fetchone()["id"]
        finally:
            admin.close()
        client = TestClient(self.app)
        r = client.post("/login", data={"email": email, "password": PW},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        return client, uid

    def _request_recovery(self, uid):
        """Started AFTER the session exists, as the attack would be."""
        from oikonome.auth import factor_reset
        conn = tenancy.control_connect()
        try:
            factor_reset.request(conn, uid)
        finally:
            conn.close()

    def _pending(self, uid):
        from oikonome.auth import factor_reset
        conn = tenancy.control_connect()
        try:
            return factor_reset.pending(conn, uid)
        finally:
            conn.close()

    def test_me_names_the_pending_recovery_and_when_it_lands(self):
        c, uid = self._user()
        self.assertIsNone(c.get("/api/me").json()["factor_reset_pending"])
        self._request_recovery(uid)
        self.assertTrue(c.get("/api/me").json()["factor_reset_pending"])

    def test_the_signed_in_owner_can_cancel_it(self):
        c, uid = self._user()
        self._request_recovery(uid)
        r = c.post("/api/recovery/cancel-pending")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["cancelled"], 1)
        self.assertIsNone(self._pending(uid))
        self.assertIsNone(c.get("/api/me").json()["factor_reset_pending"])

    def test_a_view_only_member_can_cancel_their_own(self):
        c, uid = self._user(role="viewer")
        self._request_recovery(uid)
        r = c.post("/api/recovery/cancel-pending")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self._pending(uid))

    def test_using_an_open_session_does_not_cancel_it_by_itself(self):
        c, uid = self._user()
        self._request_recovery(uid)
        self.assertEqual(c.get("/api/me").status_code, 200)
        self.assertIsNotNone(self._pending(uid))

    def test_signed_out_callers_cannot_reach_it(self):
        r = TestClient(self.app).post("/api/recovery/cancel-pending")
        self.assertEqual(r.status_code, 401, r.text)

    def test_both_clients_offer_the_cancel(self):
        import pathlib
        import oikonome
        root = pathlib.Path(oikonome.__file__).parent.parent.parent
        for rel in ("webapp/src/api/client.ts", "mobile/src/lib/api.ts"):
            f = root / rel
            if not f.exists():
                self.skipTest(f"{rel} not present")
            self.assertTrue(
                "/api/recovery/cancel-pending" in f.read_text(), rel)
        for rel in ("webapp/src/pages/Settings.tsx",
                    "mobile/src/app/security.tsx"):
            self.assertTrue(
                "factor_reset_pending" in (root / rel).read_text(), rel)


if __name__ == "__main__":
    unittest.main()
