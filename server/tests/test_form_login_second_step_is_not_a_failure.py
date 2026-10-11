"""The no-JS /login form counts only real failures toward the human check.

Step one of a two-factor sign-in is the right password with no code yet.
`/api/login` answers it `totp_required` / `passkey_required` and records
nothing; the form must do the same. Counted as a failure, it adds an
auto-ban strike and a risk mark on the address and the account, and a few
ordinary sign-ins behind one address then demand a human check that a
browser without scripts cannot complete. A wrong password, or a wrong code,
still counts.
"""
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import totp
from oikonome.db import tenancy

from .util import TEST_DB, _admin_dsn, _ensure_db

PW = "correct-horse-battery"


class FormLoginSecondStepIsNotAFailure(unittest.TestCase):
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
        self.email = f"formstep-{uuid.uuid4().hex[:8]}@example.dev"
        r = TestClient(self.app).post("/api/signup", data={
            "email": self.email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def _post(self, **data):
        with mock.patch("oikonome.web.security.record_auth_failure") as rec:
            r = TestClient(self.app).post(
                "/login", data={"email": self.email, **data},
                follow_redirects=False)
        return r, rec

    def _enrol_totp(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute("UPDATE users SET totp_secret=%s WHERE email=%s",
                          (totp.new_secret(), self.email))
        finally:
            admin.close()

    def test_right_password_on_a_totp_account_is_not_counted(self):
        self._enrol_totp()
        r, rec = self._post(password=PW)
        self.assertEqual(r.status_code, 401)
        self.assertIn("6-digit code", r.text)
        rec.assert_not_called()

    def test_a_wrong_code_on_a_totp_account_is_counted(self):
        self._enrol_totp()
        r, rec = self._post(password=PW, totp_code="000000")
        self.assertEqual(r.status_code, 401)
        rec.assert_called_once()

    def test_a_wrong_password_is_counted(self):
        r, rec = self._post(password="not-the-password")
        self.assertEqual(r.status_code, 401)
        rec.assert_called_once()

    def test_right_password_on_a_passkey_only_account_is_not_counted(self):
        with mock.patch("oikonome.web.app._passkey_only_login_blocked",
                        return_value=True):
            r, rec = self._post(password=PW)
            self.assertEqual(r.status_code, 401)
            rec.assert_not_called()
            r, rec = self._post(password=PW, totp_code="not-a-recovery-code")
            self.assertEqual(r.status_code, 401)
            rec.assert_called_once()


if __name__ == "__main__":
    unittest.main()
