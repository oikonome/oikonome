"""A reset that lands while a factor-planting request is in flight wins.

The doors that plant a factor or a bearer credential — TOTP confirm, a
passkey, a device token, a script token, a recovery batch, a password or
email change — check the caller's session once, when the request arrives,
and then do slow work (an argon2 step-up, a code check) before they write.
A password reset that commits in that gap deletes the session and every
factor. The door must then refuse with 401 and write nothing: otherwise a
hijacker's in-flight request plants its own TOTP secret, token or password
on the account its owner just recovered.

Each test stands the reset in the gap by running it from the door's own
slow step, then checks the door's answer and that nothing it would have
written is there.
"""
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import reset as reset_mod
from oikonome.db import tenancy

from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"


def _fresh_code(secret):
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)


class AResetMidRequestStopsFactorEnrolment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.email = f"midreset-{uuid.uuid4().hex[:8]}@example.dev"
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={"email": self.email,
                                                  "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        admin = tenancy.admin_connect()
        try:
            self.uid = admin.execute("SELECT id FROM users WHERE email=%s",
                                     (self.email,)).fetchone()["id"]
        finally:
            admin.close()

    def _reset_now(self):
        """What a completed password reset does to the account, committed."""
        admin = tenancy.admin_connect()
        try:
            with admin.transaction():
                reset_mod.revoke_everything(admin, self.uid)
        finally:
            admin.close()

    def _user(self, *cols):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                f"SELECT {', '.join(cols)} FROM users WHERE id=%s",
                (self.uid,)).fetchone()
        finally:
            admin.close()

    def _count(self, sql):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(sql, (self.uid,)).fetchone()["n"]
        finally:
            admin.close()

    def _reset_during(self, target):
        """Patch `target` so the reset commits while it runs."""
        real = target[1]

        def wrapped(*a, **kw):
            out = real(*a, **kw)
            self._reset_now()
            return out
        return mock.patch.object(target[0], target[2], wrapped)

    def test_totp_confirm_after_a_reset_writes_no_secret(self):
        from oikonome.auth import totp
        secret = self.client.post("/api/totp/enroll",
                                  data={"password": PW}).json()["secret"]
        code = _fresh_code(secret)
        with self._reset_during((totp, totp.verify_used, "verify_used")):
            r = self.client.post("/api/totp/confirm", data={
                "secret": secret, "code": code, "password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIsNone(self._user("totp_secret")["totp_secret"])
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM recovery_codes WHERE user_id=%s"), 0)

    def test_password_change_after_a_reset_keeps_the_old_hash(self):
        from oikonome.auth import passwords
        before = self._user("password_hash")["password_hash"]
        with self._reset_during(
                (passwords, passwords.hash_password, "hash_password")):
            r = self.client.post("/api/password/change", json={
                "current_password": PW,
                "new_password": "another-long-passphrase-42"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._user("password_hash")["password_hash"], before)

    def test_script_token_mint_after_a_reset_mints_nothing(self):
        appmod = self.appmod
        with self._reset_during((appmod, appmod._require_elevation,
                                 "_require_elevation")):
            r = self.client.post("/api/tokens", json={"name": "collector",
                                                      "password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM api_tokens WHERE created_by=%s"), 0)

    def test_recovery_regenerate_after_a_reset_issues_nothing(self):
        appmod = self.appmod
        with self._reset_during((appmod, appmod._require_elevation,
                                 "_require_elevation")):
            r = self.client.post("/api/totp/recovery-regenerate",
                                 data={"password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM recovery_codes WHERE user_id=%s"), 0)

    def test_device_mint_after_a_reset_mints_nothing(self):
        appmod = self.appmod
        # an older session owes the step-up — the slow step the reset
        # lands in
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE sessions SET created_at = now() - "
                          "interval '1 day' WHERE user_id=%s", (self.uid,))
        finally:
            admin.close()
        with self._reset_during((appmod, appmod._require_elevation,
                                 "_require_elevation")):
            r = self.client.post("/api/devices", json={
                "device_name": "phone", "platform": "android",
                "password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM device_tokens WHERE user_id=%s "
            "AND revoked_at IS NULL"), 0)

    def test_email_change_after_a_reset_keeps_the_address(self):
        appmod = self.appmod
        with self._reset_during((appmod, appmod._in_session_password_ok,
                                 "_in_session_password_ok")):
            r = self.client.post("/api/email/change", data={
                "new_email": f"moved-{uuid.uuid4().hex[:6]}@example.dev",
                "password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._user("email")["email"], self.email)

    def _elevation_gap(self):
        appmod = self.appmod
        return self._reset_during((appmod, appmod._require_elevation,
                                   "_require_elevation"))

    def test_webhook_create_after_a_reset_creates_nothing(self):
        with self._elevation_gap():
            r = self.client.post("/api/webhooks", json={
                "url": "http://127.0.0.1:9/hook", "events": ["test.ping"],
                "name": "h", "password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM webhooks WHERE created_by_user=%s"), 0)

    def test_webhook_enable_after_a_reset_leaves_it_paused(self):
        r = self.client.post("/api/webhooks", json={
            "url": "http://127.0.0.1:9/hook", "events": ["test.ping"],
            "name": "h", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        hid = r.json()["id"]
        with self._elevation_gap():
            r = self.client.post(f"/api/webhooks/{hid}", json={
                "enabled": True, "password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM webhooks WHERE created_by_user=%s "
            "AND enabled"), 0)

    def test_webhook_url_change_after_a_reset_keeps_the_old_url(self):
        r = self.client.post("/api/webhooks", json={
            "url": "http://127.0.0.1:9/hook", "events": ["test.ping"],
            "name": "h", "password": PW})
        hid = r.json()["id"]
        with self._elevation_gap():
            r = self.client.post(f"/api/webhooks/{hid}", json={
                "url": "http://127.0.0.1:9/elsewhere", "password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM webhooks WHERE created_by_user=%s "
            "AND url LIKE '%%elsewhere'"), 0)

    def test_family_invite_after_a_reset_mints_nothing(self):
        with self._elevation_gap():
            r = self.client.post("/api/invites", json={
                "label": "spouse", "password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM invites WHERE created_by=%s "
            "AND used_at IS NULL"), 0)

    def test_push_subscribe_after_a_reset_stores_nothing(self):
        from oikonome.web import api

        def allowed(endpoint):
            self._reset_now()
            return True
        with mock.patch.object(api, "_valid_push_endpoint", allowed):
            r = self.client.post("/api/notify/push/subscribe", json={
                "endpoint": f"https://push.example.com/{uuid.uuid4().hex}",
                "keys": {"p256dh": "p", "auth": "a"}, "password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM push_subscriptions WHERE user_id=%s"),
            0)

    def test_the_eviction_waits_for_a_door_holding_the_factor_lock(self):
        """A door that is writing under the factor lock commits before the
        eviction removes anything, so the eviction then sees what it
        wrote."""
        import psycopg
        holder = tenancy.admin_connect()
        evictor = tenancy.admin_connect()
        try:
            with holder.transaction():
                reset_mod.lock_user_factors(holder, self.uid)
                evictor.execute("SET lock_timeout = '250ms'")
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    with evictor.transaction():
                        reset_mod.revoke_everything(evictor, self.uid)
        finally:
            holder.close()
            evictor.close()

    def test_without_a_reset_the_doors_still_work(self):
        r = self.client.post("/api/tokens", json={"name": "collector",
                                                  "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        secret = self.client.post("/api/totp/enroll",
                                  data={"password": PW}).json()["secret"]
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
