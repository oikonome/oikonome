"""A credential rotation ends every support-access grant the account made.

A grant opens the household's data to the operator for up to a week. One a
hijacker opened with a stolen session and password must not outlive the
owner's answer to it — a password change or a change of second factor —
any more than it outlives a recovery. Revoked, never deleted: the row is
the audit of who granted what.
"""
import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"


class RotationEndsSupportAccess(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.auth import passwords
        from oikonome.web import security
        security._limiter._hits.clear()
        self.email = f"sa-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(admin, self.email)
            self.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (self.tid, self.email,
                 passwords.hash_password(PW))).fetchone()["id"]
            self.grant = admin.execute(
                "INSERT INTO support_consents (tenant_id, granted_by, "
                "expires_at, reason) VALUES (%s, %s, %s, 'test') "
                "RETURNING id",
                (self.tid, self.uid, dt.datetime.now(dt.timezone.utc)
                 + dt.timedelta(days=7))).fetchone()["id"]
        finally:
            admin.close()
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/login", data={"email": self.email,
                                             "password": PW},
                             follow_redirects=False)
        assert r.status_code == 303, r.text

    def _revoked(self):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT revoked_at FROM support_consents WHERE id=%s",
                (self.grant,)).fetchone()["revoked_at"] is not None
        finally:
            admin.close()

    def test_a_password_change_ends_the_grant(self):
        r = self.client.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": "brand-new-passphrase"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["support_access_revoked"], 1)
        self.assertTrue(self._revoked())

    def test_an_email_change_ends_the_grant(self):
        new = f"sa-new-{uuid.uuid4().hex[:8]}@x.dev"
        r = self.client.post("/api/email/change",
                             data={"new_email": new, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["support_access_revoked"], 1)
        self.assertTrue(self._revoked())

    def test_adding_an_authenticator_ends_the_grant(self):
        from oikonome.auth import totp as totp_mod
        secret = totp_mod.new_secret()
        clear_totp_burn()
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": totp_mod.code_now(secret),
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self._revoked())


if __name__ == "__main__":
    unittest.main()
