"""Removing a passkey ends what that key signed in.

A key that is removed must no longer be able to act: someone holding a
lost or stolen authenticator who had already signed in with it would
otherwise keep the browser session, the 90-day device token minted from
it and any unspent mint ticket after the owner removed the key. Sessions
opened another way (a password) are untouched."""

import base64
import os
import secrets
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class _FakeAuth:
    new_sign_count = 7


class RemovingAPasskeyEndsWhatItSignedIn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        cls._hosted = os.environ.get("OIKONOME_HOSTED")
        os.environ["OIKONOME_HOSTED"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.client = TestClient(cls.app)
        cls.email = f"pkend-{uuid.uuid4().hex[:8]}@example.dev"
        from oikonome.auth import signup_invites
        admin = tenancy.admin_connect()
        try:
            invite = signup_invites.mint(admin, cls.email)
        finally:
            admin.close()
        r = cls.client.post("/api/signup", data={
            "email": cls.email, "password": PW, "invite": invite})
        assert r.status_code == 200, r.text
        # a TOTP factor too, so removing the key is never the last factor
        from oikonome.auth import totp as _totp
        from oikonome.db import crypto as _crypto
        cls.totp_secret = _totp.new_secret()
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET totp_secret=%s WHERE email=%s",
                          (_crypto.encrypt_cp(cls.totp_secret), cls.email))
        finally:
            admin.close()

    @classmethod
    def tearDownClass(cls):
        if cls._hosted is None:
            os.environ.pop("OIKONOME_HOSTED", None)
        else:
            os.environ["OIKONOME_HOSTED"] = cls._hosted
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM passkeys WHERE user_id IN "
                          "(SELECT id FROM users WHERE email=%s)",
                          (cls.email,))
        finally:
            admin.close()

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _code(self):
        from oikonome.auth import totp as _totp
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET totp_last_counter=NULL "
                          "WHERE email=%s", (self.email,))
        finally:
            admin.close()
        return _totp.code_now(self.totp_secret)

    def _enroll(self) -> tuple[str, str]:
        """A fresh passkey on the account: (its id, its credential id)."""
        raw = secrets.token_bytes(16)

        class _Reg:
            credential_id = raw
            credential_public_key = b"\x05\x06\x07\x08"
            sign_count = 0
        o = self.client.post("/api/passkeys/options", json={
            "password": PW, "totp_code": self._code()}).json()
        with mock.patch("oikonome.auth.passkeys.verify_registration_response",
                        return_value=_Reg()):
            r = self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"],
                "credential": {"id": _b64u(raw),
                               "response": {"transports": ["internal"]}},
                "label": f"key-{raw.hex()[:6]}", "password": PW,
                "totp_code": self._code()})
        self.assertEqual(r.status_code, 200, r.text)
        cred = _b64u(raw)
        pk = [p for p in self.client.get("/api/passkeys").json()["passkeys"]
              if p["label"] == f"key-{raw.hex()[:6]}"][0]
        return pk["id"], cred

    def _passkey_login(self, cred: str) -> tuple[TestClient, str]:
        """Sign in with the key on a fresh client: (client, mint ticket)."""
        c = TestClient(self.app)
        lo = c.post("/api/login/passkey/options",
                    json={"email": self.email}).json()
        with mock.patch(
                "oikonome.auth.passkeys.verify_authentication_response",
                return_value=_FakeAuth()):
            r = c.post("/api/login/passkey", json={
                "challenge_id": lo["challenge_id"],
                "credential": {"id": cred}})
        self.assertEqual(r.status_code, 200, r.text)
        return c, r.json()["mint_ticket"]

    def _remove(self, pk_id, via=None, headers=None):
        r = (via or self.client).request(
            "DELETE", f"/api/passkeys/{pk_id}", headers=headers,
            json={"password": PW, "totp_code": self._code()})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _bearer_ok(self, token) -> bool:
        anon = TestClient(self.app)
        return anon.get("/api/me", headers={
            "Authorization": f"Bearer {token}"}).status_code == 200

    def test_a_session_signed_in_by_the_key_dies_with_it(self):
        pk_id, cred = self._enroll()
        browser, _ = self._passkey_login(cred)
        self.assertEqual(browser.get("/api/me").status_code, 200)
        self._remove(pk_id)
        self.assertEqual(browser.get("/api/me").status_code, 401)

    def test_a_password_session_survives_the_removal(self):
        pk_id, _ = self._enroll()
        self._remove(pk_id)
        self.assertEqual(self.client.get("/api/me").status_code, 200)

    def test_the_answer_says_the_caller_was_not_signed_out(self):
        pk_id, _ = self._enroll()
        self.assertIs(self._remove(pk_id)["signed_out"], False)

    def test_removing_the_key_you_signed_in_with_says_so(self):
        pk_id, cred = self._enroll()
        browser, _ = self._passkey_login(cred)
        self.assertIs(self._remove(pk_id, via=browser)["signed_out"], True)
        self.assertEqual(browser.get("/api/me").status_code, 401)

    def test_a_phone_removing_the_key_its_token_came_from_is_told(self):
        pk_id, cred = self._enroll()
        _, ticket = self._passkey_login(cred)
        r = TestClient(self.app).post("/api/devices", json={
            "mint_ticket": ticket, "device_name": "phone",
            "platform": "android"})
        self.assertEqual(r.status_code, 200, r.text)
        auth = {"Authorization": f"Bearer {r.json()['token']}"}
        out = self._remove(pk_id, via=TestClient(self.app), headers=auth)
        self.assertIs(out["signed_out"], True)
        self.assertFalse(self._bearer_ok(r.json()["token"]))

    def test_a_session_signed_in_by_another_key_survives(self):
        pk_a, cred_a = self._enroll()
        _, cred_b = self._enroll()
        browser_b, _ = self._passkey_login(cred_b)
        self._remove(pk_a)
        self.assertEqual(browser_b.get("/api/me").status_code, 200)

    def test_a_device_token_minted_from_the_keys_sign_in_is_revoked(self):
        pk_id, cred = self._enroll()
        # the phone's road: the login's mint ticket buys the device token
        _, ticket = self._passkey_login(cred)
        r = TestClient(self.app).post("/api/devices", json={
            "mint_ticket": ticket, "device_name": "phone",
            "platform": "android"})
        self.assertEqual(r.status_code, 200, r.text)
        phone = r.json()["token"]
        # the browser's road: a fresh passkey session mints by cookie
        browser, _ = self._passkey_login(cred)
        r = browser.post("/api/devices", json={"device_name": "tablet",
                                               "platform": "ios"})
        self.assertEqual(r.status_code, 200, r.text)
        tablet = r.json()["token"]
        self.assertTrue(self._bearer_ok(phone))
        self.assertTrue(self._bearer_ok(tablet))
        self._remove(pk_id)
        self.assertFalse(self._bearer_ok(phone))
        self.assertFalse(self._bearer_ok(tablet))

    def test_an_unspent_mint_ticket_from_the_key_is_burned(self):
        pk_id, cred = self._enroll()
        _, ticket = self._passkey_login(cred)
        self._remove(pk_id)
        r = TestClient(self.app).post("/api/devices", json={
            "mint_ticket": ticket, "device_name": "phone",
            "platform": "android"})
        self.assertEqual(r.status_code, 401, r.text)


if __name__ == "__main__":
    unittest.main()
