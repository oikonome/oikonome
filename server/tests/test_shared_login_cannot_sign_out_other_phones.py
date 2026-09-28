"""A shared demonstration login cannot sign out another holder's phone.

That login's second factor is waived and its credentials are published, so
anyone can complete a "full login" with it. The device-mint door's
`replace` field revokes a named device without a step-up, on the grounds
that a full login just happened — which proves nothing for this login. The
invariant: for a login whose second factor is waived, `replace` is refused
on both mint paths (ticket and session cookie) and the named device stays
live, and neither the device roster nor the session roster (other
holders' addresses and browsers) is served to it. An ordinary login may
still swap a device out and still sees its own sessions.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import passwords
from oikonome.db import tenancy

from .util import _admin_dsn, _ensure_db, TEST_DB

PW = "correct-horse-battery"


class SharedLoginCannotSignOutOtherPhones(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        cls.tid = str(tenancy.create_tenant(
            cls.admin, f"shr-{uuid.uuid4().hex[:6]}"))
        cls.plain = f"shr-plain-{uuid.uuid4().hex[:6]}@example.dev"
        cls.waived = f"shr-demo-{uuid.uuid4().hex[:6]}@example.dev"
        h = passwords.hash_password(PW)
        cls.admin.execute(
            "INSERT INTO users (tenant_id,email,password_hash,role,"
            "verified_at,second_factor_waived) VALUES "
            "(%s,%s,%s,'viewer',now(),FALSE), (%s,%s,%s,'viewer',now(),TRUE)",
            (cls.tid, cls.plain, h, cls.tid, cls.waived, h))

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _login(self, email):
        c = TestClient(self.app)
        r = c.post("/api/login", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return c, r.json().get("mint_ticket")

    def _phone(self, email) -> str:
        _c, ticket = self._login(email)
        naked = TestClient(self.app)
        m = naked.post("/api/devices", json={
            "mint_ticket": ticket, "device_name": "visitor",
            "platform": "android"})
        self.assertEqual(m.status_code, 200, m.text)
        return m.json()["id"]

    def _live(self, device_id) -> bool:
        return self.admin.execute(
            "SELECT revoked_at IS NULL AS live FROM device_tokens "
            "WHERE id=%s", (device_id,)).fetchone()["live"]

    def test_ticket_mint_refuses_replace_for_shared_login(self):
        victim = self._phone(self.waived)
        _c, ticket = self._login(self.waived)
        r = TestClient(self.app).post("/api/devices", json={
            "mint_ticket": ticket, "device_name": "other",
            "platform": "android", "replace": victim})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertTrue(self._live(victim),
                        "a shared login signed out another holder's phone")

    def test_session_mint_refuses_replace_for_shared_login(self):
        victim = self._phone(self.waived)
        c, _ticket = self._login(self.waived)
        r = c.post("/api/devices", json={
            "device_name": "other", "platform": "android",
            "replace": victim})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertTrue(self._live(victim),
                        "a shared login signed out another holder's phone")

    def test_shared_login_is_not_shown_the_device_roster(self):
        self._phone(self.waived)
        c, _ticket = self._login(self.waived)
        got = c.get("/api/devices").json()
        self.assertEqual(got["devices"], [])
        self.assertTrue(got.get("hidden"))

    def test_shared_login_is_not_shown_the_session_roster(self):
        self._login(self.waived)          # another holder, elsewhere
        c, _ticket = self._login(self.waived)
        got = c.get("/api/sessions").json()
        self.assertEqual(got["sessions"], [],
                         "a shared login was shown other holders' sessions")
        self.assertTrue(got.get("hidden"))

    def test_ordinary_login_still_sees_its_sessions(self):
        c, _ticket = self._login(self.plain)
        got = c.get("/api/sessions").json()
        self.assertFalse(got.get("hidden"))
        self.assertTrue(any(s["current"] for s in got["sessions"]))

    def test_ordinary_login_may_still_swap_a_device_out(self):
        old = self._phone(self.plain)
        _c, ticket = self._login(self.plain)
        r = TestClient(self.app).post("/api/devices", json={
            "mint_ticket": ticket, "device_name": "new",
            "platform": "android", "replace": old})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self._live(old))


if __name__ == "__main__":
    unittest.main()
