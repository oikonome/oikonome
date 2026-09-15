"""TOTP seeds encrypted at rest on the control-plane `users`
table, so a database dump is never a 2FA-seed dump (with the seed, an
attacker who also phishes the password walks through the second factor).
Seeds encrypt under the master key directly (the
per-tenant envelope can't apply: control-plane connections have no
ambient tenant). Pre-encryption rows keep verifying via passthrough and
the migrate sweep rewrites them on the next boot."""

import os
import unittest
import uuid

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from oikonome.auth import totp as totp_mod
from oikonome.db import crypto, tenancy

from .util import clear_totp_burn, _ensure_db


def _fresh_code(secret):
    """A code that is accepted now. Step-up doors burn the code, so
    these tests — which are about the step-up GATE,
    not replay — clear the burn first. See util.clear_totp_burn.
    """
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)

PW = "correct-horse-battery"


def _control():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                           autocommit=True)


class TotpAtRestTests(unittest.TestCase):
    def setUp(self):
        os.environ["OIKONOME_DEV"] = "1"
        self._prev = os.environ.get("OIKONOME_MASTER_KEY")
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        from oikonome.web.app import app
        self.client = TestClient(app)
        self.email = f"b1-{uuid.uuid4().hex[:8]}@example.dev"
        self.client.post("/api/signup",
                         data={"email": self.email, "password": PW})

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("OIKONOME_MASTER_KEY", None)
        else:
            os.environ["OIKONOME_MASTER_KEY"] = self._prev

    def _enroll(self):
        secret = self.client.post(
            "/api/totp/enroll", data={"password": PW}).json()["secret"]
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret),
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return secret

    def _raw(self):
        with _control() as conn:
            return conn.execute(
                "SELECT totp_secret FROM users WHERE email=%s",
                (self.email,)).fetchone()["totp_secret"]

    def test_raw_sql_shows_ciphertext_not_the_seed(self):
        secret = self._enroll()
        raw = self._raw()
        self.assertTrue(raw.startswith(crypto.CP_PREFIX), raw[:20])
        self.assertNotIn(secret, raw)
        # and the round trip is intact
        self.assertEqual(crypto.decrypt_cp(raw), secret)

    def test_full_login_cycle_with_encrypted_seed(self):
        secret = self._enroll()
        self.client.post("/api/logout")
        r = self.client.post("/api/login", data={
            "email": self.email, "password": PW})
        self.assertEqual(r.status_code, 401)
        self.assertIn("totp_required", r.text)
        r = self.client.post("/api/login", data={
            "email": self.email, "password": PW,
            "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200)
        # disable verifies the password + the decrypted seed, then
        # clears it
        r = self.client.post("/api/totp/disable",
                             data={"code": _fresh_code(secret),
                                   "password": PW})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(self._raw())

    def test_legacy_plaintext_row_still_verifies(self):
        # a seed enrolled before sits plaintext until the sweep —
        # login must keep working in the meantime (decrypt passthrough)
        secret = totp_mod.new_secret()
        with _control() as conn:
            conn.execute("UPDATE users SET totp_secret=%s WHERE email=%s",
                         (secret, self.email))
        self.client.post("/api/logout")
        r = self.client.post("/api/login", data={
            "email": self.email, "password": PW,
            "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200)

    def test_migrate_sweep_encrypts_legacy_rows(self):
        from oikonome.db.migrate import _encrypt_totp_secrets
        secret = totp_mod.new_secret()
        with _control() as conn:
            conn.execute("UPDATE users SET totp_secret=%s WHERE email=%s",
                         (secret, self.email))
        admin = tenancy.admin_connect()
        try:
            n = _encrypt_totp_secrets(admin)
        finally:
            admin.close()
        self.assertGreaterEqual(n, 1)
        raw = self._raw()
        self.assertTrue(raw.startswith(crypto.CP_PREFIX))
        self.assertEqual(crypto.decrypt_cp(raw), secret)
        # idempotent: a second sweep rewrites nothing
        admin = tenancy.admin_connect()
        try:
            self.assertEqual(_encrypt_totp_secrets(admin), 0)
        finally:
            admin.close()
        # and login still works after the sweep
        self.client.post("/api/logout")
        r = self.client.post("/api/login", data={
            "email": self.email, "password": PW,
            "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200)

    def test_no_master_key_stays_plaintext_but_working(self):
        # dev mode: no key → passthrough (same contract as tenant crypto);
        # the sweep declines rather than half-encrypting
        os.environ.pop("OIKONOME_MASTER_KEY", None)
        secret = self._enroll()
        self.assertEqual(self._raw(), secret)
        from oikonome.db.migrate import _encrypt_totp_secrets
        admin = tenancy.admin_connect()
        try:
            self.assertEqual(_encrypt_totp_secrets(admin), 0)
        finally:
            admin.close()
        self.client.post("/api/logout")
        r = self.client.post("/api/login", data={
            "email": self.email, "password": PW,
            "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
