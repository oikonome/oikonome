"""Every sign-in, session, second-factor, passkey and invite-claim door is
called and its reply matches the shape it documents (web/schemas/auth.py).

The conformance hook (tests/api_conformance.py) validates each reply; this
module reaches every door itself, so the shapes stay checked whatever the
rest of the suite happens to call. The WebAuthn verifier and the mail
senders are stood in for. A branch no test instance can reach through the
door (a demonstration instance's printed credentials) is held to its shape
by validating what the handler's own helper builds.
"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import totp as totp_mod
from oikonome.db import tenancy

from . import api_conformance
from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"
NEW_PW = "another-horse-battery"


class _FakeReg:
    credential_id = b"\x0a\x0b\x0c\x0d\x0e"
    credential_public_key = b"\x05\x06\x07\x08"
    sign_count = 0


class _FakeAuth:
    new_sign_count = 9


def _quiet():
    """The verification and security-notice mail goes nowhere."""
    return (mock.patch("oikonome.web.app._deliver_verification"),
            mock.patch("oikonome.web.app._notify_security_change"))


class _Doors(unittest.TestCase):
    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self._mail = [p.start() for p in _quiet()]
        self.addCleanup(mock.patch.stopall)

    def _ok(self, client, method, path, **kw):
        before = sum(api_conformance.validated().values())
        r = client.request(method, "/api" + path, **kw)
        self.assertEqual(r.status_code, 200, f"{method} {path}: {r.text}")
        self.assertGreater(sum(api_conformance.validated().values()), before,
                           f"{method} {path}: the reply was not validated")
        return r.json()


class PasswordAndCodeDoors(_Doors):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.client = TestClient(appmod.app)
        cls.email = f"authshape-{uuid.uuid4().hex[:8]}@example.dev"

    def _elevate(self, client, password=PW, code=None):
        body = {"password": password}
        if code is not None:
            body["totp_code"] = code
        return self._ok(client, "POST", "/auth/elevate", json=body)

    def test_the_whole_account_lifecycle(self):
        c = self.client
        signed = self._ok(c, "POST", "/signup",
                          data={"email": self.email, "password": PW})
        self.assertTrue(signed["mint_ticket"])
        self._ok(c, "GET", "/access")
        self.assertFalse(self._ok(c, "GET", "/demo-login")["demo"])
        self._ok(c, "POST", "/verify-email/resend", json={})
        self._ok(c, "POST", "/recovery/cancel-pending", json={})

        status = self._ok(c, "GET", "/auth/elevation")
        self.assertFalse(status["elevated"])
        self.assertTrue(self._elevate(c)["elevated"])

        # an authenticator: enrol, confirm (the first factor mints a sheet)
        enrol = self._ok(c, "POST", "/totp/enroll", data={})
        secret = enrol["secret"]
        confirmed = self._ok(c, "POST", "/totp/confirm", data={
            "secret": secret, "code": totp_mod.code_now(secret)})
        self.assertTrue(confirmed["recovery_codes"])
        clear_totp_burn(self.email)
        self._elevate(c, code=totp_mod.code_now(secret))
        regen = self._ok(c, "POST", "/totp/recovery-regenerate", data={})
        self.assertTrue(regen["recovery_codes"])

        # a second browser signs in with the code; the first lists and
        # signs it out
        other = TestClient(self.app)
        clear_totp_burn(self.email)
        self._ok(other, "POST", "/login", data={
            "email": self.email, "password": PW,
            "totp_code": totp_mod.code_now(secret)})
        rows = self._ok(c, "GET", "/sessions")["sessions"]
        self.assertEqual(len(rows), 2)
        theirs = next(r["id"] for r in rows if not r["current"])
        self._ok(c, "POST", "/sessions/revoke", json={"id": theirs})
        self._ok(c, "POST", "/sessions/revoke", json={"all_others": True})

        # the password changes, then the authenticator goes
        changed = self._ok(c, "POST", "/password/change", json={
            "current_password": PW, "new_password": NEW_PW,
            "reissue_codes": True})
        self.assertIsNotNone(changed["recovery_codes"])
        clear_totp_burn(self.email)
        self._ok(c, "POST", "/totp/disable", data={
            "code": totp_mod.code_now(secret), "reissue_codes": "1"})
        # with nothing enrolled the same door answers its short shape
        self._elevate(c, password=NEW_PW)
        clear_totp_burn(self.email)
        short = self._ok(c, "POST", "/totp/disable", data={"code": "000000"})
        self.assertEqual(set(short), {"ok", "sms_number_needs_verify"})

        self._ok(c, "POST", "/logout", json={})

    def test_an_invite_is_peeked_and_claimed(self):
        owner = TestClient(self.app)
        email = f"authshape-owner-{uuid.uuid4().hex[:8]}@example.dev"
        self._ok(owner, "POST", "/signup",
                 data={"email": email, "password": PW})
        self._elevate(owner)
        inv = owner.post("/api/invites", json={"label": "family",
                                               "role": "viewer"})
        self.assertEqual(inv.status_code, 200, inv.text)
        token = inv.json()["url"].rsplit("token=", 1)[1]
        peek = self._ok(TestClient(self.app), "GET",
                        f"/invite/peek?token={token}")
        self.assertEqual(peek["role"], "viewer")
        self._ok(TestClient(self.app), "POST", "/invite/claim", json={
            "token": token,
            "email": f"authshape-member-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})

    def test_a_demonstration_instances_credentials_match_the_shape(self):
        from oikonome.web.app import _demo_login_payload
        from oikonome.web.schemas.auth import DemoLogin
        cfg = {"demo_mode": True,
               "demo_login": {"email": "demo@example.dev",
                              "password": "demo-pass"}}
        DemoLogin.model_validate(_demo_login_payload(1, cfg), strict=True)


class PasskeyDoors(_Doors):
    """Passkeys are offered on a hosted instance only."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        os.environ["OIKONOME_HOSTED"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.client = TestClient(appmod.app)
        cls.email = f"authshape-pk-{uuid.uuid4().hex[:8]}@example.dev"
        from oikonome.auth import signup_invites
        admin = tenancy.admin_connect()
        try:
            invite = signup_invites.mint(admin, cls.email)
        finally:
            admin.close()
        with _quiet()[0], _quiet()[1]:
            r = cls.client.post("/api/signup", data={
                "email": cls.email, "password": PW, "invite": invite})
        assert r.status_code == 200, r.text

    @classmethod
    def tearDownClass(cls):
        os.environ.pop("OIKONOME_HOSTED", None)
        import psycopg
        # credential ids are unique across the instance; leave none behind
        with psycopg.connect(tenancy.APP_DSN, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM passkeys WHERE user_id IN "
                "(SELECT id FROM users WHERE email = %s)", (cls.email,))

    def test_register_list_assert_sign_in_and_remove(self):
        c = self.client
        self._ok(c, "POST", "/auth/elevate", json={"password": PW})
        opts = self._ok(c, "POST", "/passkeys/options", json={})
        cred = {"id": "CgsMDQ4", "response": {"transports": ["internal"]}}
        with mock.patch("oikonome.auth.passkeys.verify_registration_response",
                        return_value=_FakeReg()):
            added = self._ok(c, "POST", "/passkeys", json={
                "challenge_id": opts["challenge_id"], "credential": cred,
                "label": "laptop"})
        # the first strong factor came with a rotation and a sheet
        self.assertTrue(added["recovery_codes"])
        keys = self._ok(c, "GET", "/passkeys")["passkeys"]
        self.assertEqual(len(keys), 1)

        step = self._ok(c, "POST", "/stepup/passkey/options", json={})
        with mock.patch(
                "oikonome.auth.passkeys.verify_authentication_response",
                return_value=_FakeAuth()):
            ticket = self._ok(c, "POST", "/stepup/passkey", json={
                "challenge_id": step["challenge_id"],
                "credential": {"id": "CgsMDQ4"}})["stepup_ticket"]
        self._ok(c, "POST", "/auth/elevate",
                 json={"stepup_ticket": ticket})

        anon = TestClient(self.app)
        self._ok(anon, "POST", "/login/passkey/options", json={})
        lo = self._ok(anon, "POST", "/login/passkey/options",
                      json={"email": self.email})
        self.assertTrue(lo["options"]["allowCredentials"])
        with mock.patch(
                "oikonome.auth.passkeys.verify_authentication_response",
                return_value=_FakeAuth()):
            self._ok(anon, "POST", "/login/passkey", json={
                "challenge_id": lo["challenge_id"],
                "credential": {"id": "CgsMDQ4"}})

        # an authenticator beside the key, so the key is not the last
        # factor and may go
        enrol = self._ok(c, "POST", "/totp/enroll", data={})
        self._ok(c, "POST", "/totp/confirm", data={
            "secret": enrol["secret"],
            "code": totp_mod.code_now(enrol["secret"])})
        removed = self._ok(c, "DELETE", f"/passkeys/{keys[0]['id']}",
                           json={"reissue_codes": True})
        self.assertFalse(removed["signed_out"])


if __name__ == "__main__":
    unittest.main()
