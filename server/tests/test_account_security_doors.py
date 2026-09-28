"""Two invariants on the account-security doors.

/api/totp/disable takes a password guess, so it carries the same rate
limit its sibling doors do (password_change and account_delete throttle
at 5/h). Unthrottled, a stolen session could brute the password against
it at network speed.

/api/account/delete erases the WHOLE TENANT, so the handler carries an
explicit _owner_only gate of its own rather than relying on the global
viewer write-gate in current_user — a path-prefix allowlist that a
future edit could silently widen into household erasure by a viewer.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db

PW = "correct-horse-battery"


def _fresh_client(app):
    client = TestClient(app)
    email = f"acctdoor-{uuid.uuid4().hex[:10]}@example.dev"
    r = client.post("/api/signup", data={"email": email, "password": PW})
    assert r.status_code == 200, r.text
    return client, email


class TotpDisableThrottleTests(unittest.TestCase):
    """The disable door throttles like the other password doors."""

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

    def test_totp_disable_is_rate_limited(self):
        client, _ = _fresh_client(self.app)
        # no 2FA enrolled → the handler no-ops with 200, but the limiter
        # sits in front of the handler, exactly like account_delete's
        for _ in range(5):
            r = client.post("/api/totp/disable",
                            data={"code": "000000", "password": "wrong-x-y"})
            self.assertEqual(r.status_code, 200)
        r = client.post("/api/totp/disable",
                        data={"code": "000000", "password": "wrong-x-y"})
        self.assertEqual(r.status_code, 429)


class AccountDeleteOwnerGateTests(unittest.TestCase):
    """Household erasure is owner-only, explicitly."""

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

    def test_viewer_cannot_delete_the_household(self):
        owner, _ = _fresh_client(self.app)
        inv = owner.post("/api/invites", json={"label": "spouse", "password": "correct-horse-battery"})
        self.assertEqual(inv.status_code, 200, inv.text)
        token = inv.json()["url"].rsplit("token=", 1)[1]
        viewer = TestClient(self.app)
        viewer_pw = "viewer-pass-12345"
        r = viewer.post("/api/invite/claim", json={
            "token": token,
            "email": f"fam-{uuid.uuid4().hex[:8]}@example.dev",
            "password": viewer_pw})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(viewer.get("/api/me").json()["role"], "viewer")
        # correct password: the refusal must be the role, not credentials
        r = viewer.post("/api/account/delete", data={"password": viewer_pw})
        self.assertEqual(r.status_code, 403)
        # the household survived — the owner is still signed in
        self.assertEqual(owner.get("/api/me").status_code, 200)

    def test_owner_still_deletes(self):
        owner, _ = _fresh_client(self.app)
        r = owner.post("/api/account/delete", data={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(owner.get("/api/me").status_code, 401)


if __name__ == "__main__":
    unittest.main()
