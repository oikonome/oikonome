"""The shared demonstration login cannot change its own account posture.

That login is handed out as a password, second factor waived, to everyone
who needs to look at the demonstration household. It is not a demo_mode
tenant, so the demo lockdown does not cover it. If any one holder could
change its password or email, enrol or drop factors, or sign the other
sessions out, every other holder would be locked out. Each posture door
refuses it; ordinary accounts are unaffected."""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import demoguard

from .util import _ensure_db

PW = "correct-horse-battery"


class SharedDemoLoginPosture(unittest.TestCase):
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
        store = security._shared_store()
        if store is not None:
            try:
                store._r.flushdb()
            except Exception:
                pass

    def _login(self, waived: bool):
        c = TestClient(self.app)
        email = f"shared-{uuid.uuid4().hex[:10]}@example.dev"
        data = {"email": email, "password": PW}
        if os.environ.get("OIKONOME_HOSTED"):
            from oikonome.auth import signup_invites
            admin = tenancy.admin_connect()
            try:
                data["invite"] = signup_invites.mint(admin, email)
            finally:
                admin.close()
        self.assertEqual(c.post("/api/signup", data=data).status_code, 200)
        if waived:
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE users SET second_factor_waived=TRUE "
                              "WHERE email=%s", (email,))
            finally:
                admin.close()
        return c, email

    def _password_hash(self, email):
        admin = tenancy.admin_connect()
        try:
            return admin.execute("SELECT password_hash, email FROM users "
                                 "WHERE email=%s", (email,)).fetchone()
        finally:
            admin.close()

    def test_posture_doors_refuse_the_shared_login(self):
        c, email = self._login(waived=True)
        before = self._password_hash(email)
        doors = [
            ("/api/password/change", {"json": {
                "current_password": PW, "password": PW,
                "new_password": "another-horse-battery"}}),
            ("/api/email/change", {"data": {
                "new_email": f"taken-{uuid.uuid4().hex[:6]}@example.dev",
                "password": PW}}),
            ("/api/totp/enroll", {"data": {"password": PW}}),
            ("/api/passkeys/options", {"json": {"password": PW}}),
            ("/api/sessions/revoke", {"json": {"all_others": True,
                                               "password": PW}}),
            ("/api/account/leave", {"data": {"password": PW}}),
            ("/api/account/delete", {"data": {"password": PW}}),
        ]
        for path, kw in doors:
            r = c.post(path, **kw)
            self.assertEqual(r.status_code, 403, f"{path}: {r.text}")
            self.assertIn("shared demonstration login", r.text, path)
        # nothing moved
        self.assertEqual(self._password_hash(email), before)

    def test_ordinary_account_is_not_refused(self):
        c, email = self._login(waived=False)
        new = f"moved-{uuid.uuid4().hex[:8]}@example.dev"
        r = c.post("/api/email/change", data={"new_email": new,
                                              "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_guard_reads_the_waiver_not_the_role(self):
        with self.assertRaises(Exception) as cm:
            demoguard.deny_shared_login(
                {"tenant_id": str(uuid.uuid4()), "role": "owner",
                 "second_factor_waived": True})
        self.assertEqual(getattr(cm.exception, "status_code", None), 403)


if __name__ == "__main__":
    unittest.main()
