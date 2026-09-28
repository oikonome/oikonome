"""The email is the login identifier — changing it is a credential
rotation, and every persistence a hijacked session could have planted
must die with it. Sessions, script tokens and device tokens are revoked
with it; this file pins the passkey half: a passkey enrolled before the
change (possibly by the hijacker) must not survive it, exactly as
password change and password reset already guarantee. A surviving rogue
passkey would keep working at /api/login/passkey no matter how many
sessions were killed."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"


class _FakeReg:
    """Mocked WebAuthn verifier result (the library call is not what is
    under test) — with a RANDOM credential id: passkeys.credential_id is
    globally UNIQUE on a control-plane table, so a constant id would leave
    a poisoned row behind if an assertion ever failed mid-test."""

    def __init__(self):
        self.credential_id = uuid.uuid4().bytes
        self.credential_public_key = b"\x05\x06\x07\x08"
        self.sign_count = 0


class EmailChangeEvictsPasskeysTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        os.environ["OIKONOME_HOSTED"] = "1"   # passkeys are hosted-only
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("OIKONOME_HOSTED", None)

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        self.email = f"ecr-{uuid.uuid4().hex[:8]}@example.dev"
        from oikonome.auth import signup_invites
        admin = tenancy.admin_connect()
        try:
            invite = signup_invites.mint(admin, self.email)
        finally:
            admin.close()
        r = self.client.post("/api/signup", data={
            "email": self.email, "password": PW, "invite": invite})
        assert r.status_code == 200, r.text
        # Give the account TOTP so it is never passkey-only — the email
        # change then steps up with password + TOTP, and the passkey-only
        # recovery-code layer (which has its own tests) stays out of the
        # way of what this file is about.
        from oikonome.auth import totp as _totp
        from oikonome.db import crypto as _crypto
        self.totp_secret = _totp.new_secret()
        admin = tenancy.admin_connect()
        try:
            self.uid = admin.execute(
                "UPDATE users SET totp_secret=%s WHERE email=%s "
                "RETURNING id",
                (_crypto.encrypt_cp(self.totp_secret), self.email)
            ).fetchone()["id"]
        finally:
            admin.close()

    def _totp_now(self):
        # step-up doors burn the code; these tests exercise the rotation,
        # not replay protection, so clear the burn before minting
        clear_totp_burn(self.email)
        from oikonome.auth import totp as _totp
        return _totp.code_now(self.totp_secret)

    def _enroll_passkey(self):
        o = self.client.post("/api/passkeys/options", json={"password": PW, "totp_code": self._totp_now()}).json()
        cred = {"id": "AQIDBA", "response": {"transports": ["internal"]}}
        with mock.patch(
                "oikonome.auth.passkeys.verify_registration_response",
                return_value=_FakeReg()):
            r = self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"], "credential": cred,
                "password": PW, "totp_code": self._totp_now()})
        assert r.status_code == 200, r.text

    def _passkey_count(self):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT COUNT(*) AS n FROM passkeys WHERE user_id=%s",
                (self.uid,)).fetchone()["n"]
        finally:
            admin.close()

    def test_email_change_deletes_every_passkey(self):
        self._enroll_passkey()
        self.assertEqual(self._passkey_count(), 1)
        new = f"rotated-{uuid.uuid4().hex[:8]}@example.dev"
        r = self.client.post("/api/email/change", data={
            "new_email": new, "password": PW,
            "totp_code": self._totp_now()})
        self.assertEqual(r.status_code, 200, r.text)
        # the response tells the client the keys are gone, so the UI can
        # explain why the user's passkey suddenly stopped working
        self.assertEqual(r.json()["passkeys_removed"], 1)
        self.assertEqual(self._passkey_count(), 0)

    def test_no_op_change_keeps_passkeys(self):
        """Re-submitting the CURRENT address is answered ok without any
        rotation — nothing changed, so nothing should be evicted."""
        self._enroll_passkey()
        r = self.client.post("/api/email/change", data={
            "new_email": self.email, "password": PW,
            "totp_code": self._totp_now()})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._passkey_count(), 1)


if __name__ == "__main__":
    unittest.main()
