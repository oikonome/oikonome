"""On the hosted product a login without a second factor is blocked from
writing, from adding a device, and is told to enrol (/api/me needs_2fa).
The one excused login is the operator-set per-user waiver that
`demo-seed --viewer` writes for a demonstration household: no request can
set it, and every other user — owner or viewer — is gated as before."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _admin_dsn, _ensure_db, TEST_DB

PW = "correct-horse-battery"


class SecondFactorWaiverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        from oikonome.auth import passwords
        cls.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        cls.tid = str(tenancy.create_tenant(cls.admin, f"waiv-{uuid.uuid4().hex[:6]}"))
        cls.plain = f"waiv-plain-{uuid.uuid4().hex[:6]}@example.dev"
        cls.waived = f"waiv-demo-{uuid.uuid4().hex[:6]}@example.dev"
        h = passwords.hash_password(PW)
        cls.admin.execute(
            "INSERT INTO users (tenant_id,email,password_hash,role,verified_at,"
            "second_factor_waived) VALUES (%s,%s,%s,'viewer',now(),FALSE), "
            "(%s,%s,%s,'viewer',now(),TRUE)",
            (cls.tid, cls.plain, h, cls.tid, cls.waived, h))

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()

    def _login(self, email):
        c = TestClient(self.app)
        r = c.post("/api/login", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return c, r.json().get("mint_ticket")

    def test_waived_login_is_not_asked_to_enrol_and_may_add_a_device(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            c, ticket = self._login(self.waived)
            self.assertFalse(c.get("/api/me").json()["needs_2fa"])
            naked = TestClient(self.app); naked.cookies.clear()
            m = naked.post("/api/devices", json={"mint_ticket": ticket,
                                                 "device_name": "p", "platform": "android"})
            self.assertEqual(m.status_code, 200, m.text)

    def test_an_ordinary_login_is_still_gated(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            c, ticket = self._login(self.plain)
            self.assertTrue(c.get("/api/me").json()["needs_2fa"])
            naked = TestClient(self.app); naked.cookies.clear()
            m = naked.post("/api/devices", json={"mint_ticket": ticket,
                                                 "device_name": "p", "platform": "android"})
            self.assertEqual(m.status_code, 403, m.text)

    def test_a_waived_account_may_remove_its_last_passkey(self):
        """The waiver means forced-2FA does not apply, so the last-factor
        refusal on passkey delete must step aside — otherwise a waived
        account with exactly one passkey could never remove it, even
        though it is not required to hold one at all."""
        from oikonome.auth import recovery
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            c, _ = self._login(self.waived)
            uid = self.admin.execute(
                "SELECT id FROM users WHERE email=%s",
                (self.waived,)).fetchone()["id"]
            pk = self.admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, %s, 0) RETURNING id",
                (uid, f"cred-{uuid.uuid4().hex}", "x")).fetchone()["id"]
            codes = recovery.issue(self.admin, uid)
            r = c.request("DELETE", f"/api/passkeys/{pk}", json={
                "password": PW, "recovery_code": codes[0]})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(
                len(c.get("/api/passkeys").json()["passkeys"]), 0)

    def test_the_waiver_cannot_be_set_through_any_settings_door(self):
        # the column is not in any writable settings/profile allowlist:
        # a request naming it changes nothing
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            c, _ = self._login(self.plain)
            c.post("/api/settings", json={"second_factor_waived": True})
            c.post("/api/me/email", json={"second_factor_waived": True, "muted": False})
            row = self.admin.execute("SELECT second_factor_waived FROM users WHERE email=%s",
                                     (self.plain,)).fetchone()
            self.assertFalse(row["second_factor_waived"])


if __name__ == "__main__":
    unittest.main()
