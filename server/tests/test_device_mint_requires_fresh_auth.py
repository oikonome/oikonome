"""Minting a mobile device token is planting durable full-access
persistence, so a bare session cookie must not be enough once the session
has aged past its login: a stolen cookie could otherwise mint a 90-day
sliding bearer that outlives the cookie itself. A session still inside
its first minutes after credential verification vouches for itself (the
mobile app mints immediately after a full login and sends no password
with the mint); anything older owes the same password step-up as the
script-token mint and the other posture-change doors."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import TEST_DB, _admin_dsn, _ensure_db, seed_accounts, write_config

PASSWORD = "correct-horse-battery"


def _age_sessions(user_id, hours=1):
    """Backdate every session for the user past the freshness window."""
    admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
    try:
        admin.execute(
            "UPDATE sessions SET created_at = now() - make_interval(hours "
            "=> %s), last_seen = now() - interval '2 minutes' "
            "WHERE user_id = %s", (hours, user_id))
    finally:
        admin.close()


class DeviceMintFreshAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app

    def _signed_in_client(self):
        client = TestClient(self.app)
        email = f"fresh-{uuid.uuid4().hex[:8]}@example.dev"
        r = client.post("/api/signup", data={
            "email": email, "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        me = client.get("/api/me").json()
        conn = tenancy.tenant_connect(me["tenant_id"])
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            uid = admin.execute("SELECT id FROM users WHERE email=%s",
                                (email,)).fetchone()["id"]
        finally:
            admin.close()
        return client, uid

    def test_fresh_login_mints_without_password(self):
        # The shipped mobile app's contract: login, then immediately POST
        # only {device_name, platform}. A just-verified credential is the
        # proof; no second password prompt.
        client, _uid = self._signed_in_client()
        r = client.post("/api/devices", json={
            "device_name": "Pixel 8", "platform": "android"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["token"].startswith("oikd_"))

    def test_stale_session_cannot_mint_bare(self):
        # The theft scenario: an attacker holding only an aged cookie —
        # no password — must not walk away with a durable bearer token.
        client, uid = self._signed_in_client()
        _age_sessions(uid)
        r = client.post("/api/devices", json={
            "device_name": "thief phone", "platform": "android"})
        self.assertEqual(r.status_code, 403, r.text)   # elevation_required
        self.assertEqual(r.json()["error"], "elevation_required")
        # and nothing was planted
        devices = client.get("/api/devices").json()["devices"]
        self.assertEqual([d for d in devices
                          if d["device_name"] == "thief phone"], [])

    def test_stale_session_mints_with_password(self):
        # The owner on an old session proves the password and proceeds —
        # same step-up shape as the script-token mint.
        client, uid = self._signed_in_client()
        _age_sessions(uid)
        r = client.post("/api/devices", json={
            "device_name": "owner phone", "platform": "android",
            "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["token"].startswith("oikd_"))

    def test_stale_session_wrong_password_denied(self):
        client, uid = self._signed_in_client()
        _age_sessions(uid)
        r = client.post("/api/devices", json={
            "device_name": "thief phone", "platform": "android",
            "password": "not-the-password"})
        self.assertEqual(r.status_code, 401, r.text)

    def test_stale_session_totp_account_needs_live_code(self):
        """A TOTP account's stale mint takes the same live-code bar as
        script-token mint — password alone must not plant a 90-day
        bearer that never asks TOTP again."""
        from oikonome.auth import totp as totp_mod
        from .util import clear_totp_burn
        client, uid = self._signed_in_client()
        secret = totp_mod.new_secret()
        r = client.post("/api/totp/confirm", data={
            "secret": secret, "code": totp_mod.code_now(secret),
            "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        _age_sessions(uid)
        r = client.post("/api/devices", json={
            "device_name": "thief phone", "platform": "android",
            "password": PASSWORD})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("totp_required", r.text)
        clear_totp_burn()
        r = client.post("/api/devices", json={
            "device_name": "owner phone", "platform": "android",
            "password": PASSWORD,
            "totp_code": totp_mod.code_now(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["token"].startswith("oikd_"))


if __name__ == "__main__":
    unittest.main()
