"""Disabling TOTP must not strip a hosted account's last strong factor.

passkey_delete has always refused to remove the LAST strong factor while
hosted forced-2FA is in effect, but totp_disable did not — so a captured
password + session + one live code could permanently downgrade a TOTP-only
account to password-only login. The disable door now applies the same
last-factor refusal: blocked when TOTP is the only factor, allowed when a
passkey remains, allowed for operator-waived accounts (forced-2FA never
applied to them), and untouched on self-hosted installs.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import clear_totp_burn, _ensure_db

PW = "correct-horse-battery"


def _fresh_code(secret):
    """A code accepted right now; clears the replay burn first (these
    tests exercise the guard, not replay protection)."""
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)


def _signup(email):
    admin = tenancy.admin_connect()
    try:
        tid = tenancy.create_tenant(admin, email)
        from oikonome.auth import passwords
        uid = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, verified_at) "
            "VALUES (%s, %s, %s, now()) RETURNING id",
            (tid, email, passwords.hash_password(PW))).fetchone()["id"]
    finally:
        admin.close()
    return uid


class TotpDisableLastFactorTests(unittest.TestCase):
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
        os.environ["OIKONOME_HOSTED"] = "1"
        self.email = f"lf-{uuid.uuid4().hex[:8]}@x.dev"
        self.uid = _signup(self.email)
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/login", data={
            "email": self.email, "password": PW}, follow_redirects=False)
        assert r.status_code == 303, r.text

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def _enroll_totp(self):
        from oikonome.auth import totp as totp_mod
        secret = totp_mod.new_secret()
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret), "password": PW})
        assert r.status_code == 200, r.text
        return secret

    def _add_passkey(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, %s, 0)",
                (self.uid, f"cred-{uuid.uuid4().hex}", "x"))
        finally:
            admin.close()

    def test_totp_only_hosted_account_cannot_disable(self):
        """The downgrade path: no passkey to fall back on → refused with
        the last_factor signal both clients already render, and the
        factor stays bound."""
        secret = self._enroll_totp()
        r = self.client.post("/api/totp/disable", data={
            "code": _fresh_code(secret), "password": PW})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("last_factor", r.text)
        self.assertTrue(self.client.get("/api/me").json()["totp_enabled"])

    def test_refusal_does_not_burn_the_presented_code(self):
        """The guard answers before the password/code checks run, so the
        live code offered with a refused disable still works afterwards
        (a rejected operation must not consume proof)."""
        secret = self._enroll_totp()
        code = _fresh_code(secret)
        r = self.client.post("/api/totp/disable", data={
            "code": code, "password": PW})
        self.assertEqual(r.status_code, 400, r.text)
        # same code still opens a code-checking door (grow a passkey so
        # disable itself becomes legal, then disable with that very code)
        self._add_passkey()
        r2 = self.client.post("/api/totp/disable", data={
            "code": code, "password": PW})
        self.assertEqual(r2.status_code, 200, r2.text)

    def test_a_passkey_vanishing_after_the_precheck_still_refuses(self):
        """The pre-check runs on a stale read before the argon2/TOTP work;
        a concurrent passkey_delete can remove the fallback passkey in
        that window. The write re-checks under the same per-user advisory
        lock passkey_delete serializes on — simulated here by letting the
        pre-check see a passkey that no longer exists at write time."""
        from unittest import mock
        secret = self._enroll_totp()
        with mock.patch.object(self.appmod, "_has_passkey",
                               return_value=True):
            r = self.client.post("/api/totp/disable", data={
                "code": _fresh_code(secret), "password": PW})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("last_factor", r.text)
        self.assertTrue(self.client.get("/api/me").json()["totp_enabled"])

    def test_totp_with_passkey_may_disable(self):
        """A passkey remains after the disable, so a strong factor
        survives — allowed."""
        secret = self._enroll_totp()
        self._add_passkey()
        r = self.client.post("/api/totp/disable", data={
            "code": _fresh_code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.client.get("/api/me").json()["totp_enabled"])

    def test_waived_account_may_disable(self):
        """The operator waiver means forced-2FA never applied to this
        account (an account the operator exempted from enrolment), so the last-factor
        refusal must step aside exactly as the enrollment gate does."""
        secret = self._enroll_totp()
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE users SET second_factor_waived=TRUE WHERE id=%s",
                (self.uid,))
        finally:
            admin.close()
        r = self.client.post("/api/totp/disable", data={
            "code": _fresh_code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_self_host_disable_unaffected(self):
        """Forced-2FA is a hosted-only policy — a self-hosted install may
        always turn TOTP off."""
        secret = self._enroll_totp()
        os.environ.pop("OIKONOME_HOSTED", None)
        r = self.client.post("/api/totp/disable", data={
            "code": _fresh_code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
