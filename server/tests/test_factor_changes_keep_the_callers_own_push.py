"""Adding or removing an authenticator, or enrolling a first passkey,
keeps the requesting browser's own web-push subscription.

These doors evict every OTHER session and the persistence that rides on
them, browser push included. The browser making the request keeps its
session, and a subscription lives exactly as long as the session it was
made under, so it keeps that too. Dropping it left a browser that still
held its local subscription, never re-registered, receiving nothing.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"


def _code(secret):
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        from oikonome.auth import passwords, sessions
        security._limiter._hits.clear()
        self.email = f"kp-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(admin, self.email)
            self.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (self.tid, self.email,
                 passwords.hash_password(PW))).fetchone()["id"]
        finally:
            admin.close()
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/login", data={"email": self.email,
                                             "password": PW},
                             follow_redirects=False)
        assert r.status_code == 303, r.text
        other = TestClient(self.appmod.app)
        r = other.post("/login", data={"email": self.email, "password": PW},
                       follow_redirects=False)
        assert r.status_code == 303, r.text
        import hashlib
        admin = tenancy.admin_connect()
        try:
            def sid(c):
                tok = c.cookies.get(sessions.COOKIE_NAME)
                return admin.execute(
                    "SELECT id FROM sessions WHERE token_hash=%s",
                    (hashlib.sha256(tok.encode()).hexdigest(),)
                ).fetchone()["id"]
            self.mine = f"https://push.example/{uuid.uuid4().hex}"
            self.theirs = f"https://push.example/{uuid.uuid4().hex}"
            for ep, s in ((self.mine, sid(self.client)),
                          (self.theirs, sid(other))):
                admin.execute(
                    "INSERT INTO push_subscriptions (user_id, tenant_id, "
                    "endpoint, p256dh, auth, session_id) "
                    "VALUES (%s, %s, %s, 'k', 'a', %s)",
                    (self.uid, self.tid, ep, s))
        finally:
            admin.close()

    def _live(self):
        admin = tenancy.admin_connect()
        try:
            return {r["endpoint"] for r in admin.execute(
                "SELECT endpoint FROM push_subscriptions WHERE user_id=%s",
                (self.uid,)).fetchall()}
        finally:
            admin.close()

    def _enroll(self):
        from oikonome.auth import totp as totp_mod
        secret = totp_mod.new_secret()
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _code(secret), "password": PW})
        self.assertEqual(200, r.status_code, r.text)
        return secret


class FactorChangesKeepOwnPush(_Base):
    def test_adding_an_authenticator_keeps_this_browsers_push(self):
        self._enroll()
        self.assertEqual({self.mine}, self._live())

    def test_removing_it_keeps_this_browsers_push(self):
        secret = self._enroll()
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth) VALUES (%s, %s, %s, 'k', 'a')",
                (self.uid, self.tid, self.theirs))
        finally:
            admin.close()
        r = self.client.post("/api/totp/disable", data={
            "code": _code(secret), "password": PW})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual({self.mine}, self._live())


class TotpDisableEvictsInsideItsTransaction(_Base):
    """Turning the authenticator off and evicting everyone else is one act
    under the factor lock. Run as two, a device token minted from a ticket
    redeemed between them is invisible to the sweep and outlives it."""

    def _secret(self):
        admin = tenancy.admin_connect()
        try:
            return admin.execute("SELECT totp_secret FROM users WHERE id=%s",
                                 (self.uid,)).fetchone()["totp_secret"]
        finally:
            admin.close()

    def test_a_failed_eviction_leaves_the_factor_in_place(self):
        from unittest import mock
        secret = self._enroll()
        client = TestClient(self.appmod.app, raise_server_exceptions=False)
        client.cookies = self.client.cookies
        with mock.patch.object(self.appmod, "_revoke_bearer_persistence",
                               side_effect=RuntimeError("boom")):
            r = client.post("/api/totp/disable", data={
                "code": _code(secret), "password": PW})
        self.assertEqual(500, r.status_code, r.text)
        self.assertIsNotNone(self._secret(),
                             "the factor must not go without the eviction")

    def test_a_secret_rebound_mid_request_is_not_cleared(self):
        """The code was checked against one secret; a re-enrolment that
        committed in between bound another, which it says nothing about."""
        from unittest import mock
        from oikonome.db import crypto
        secret = self._enroll()
        real = self.appmod._totp_check

        def rebind(*a, **k):
            out = real(*a, **k)
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE users SET totp_secret=%s WHERE id=%s",
                              (crypto.encrypt_cp("NEWSECRETNEWSECRET"),
                               self.uid))
            finally:
                admin.close()
            return out
        with mock.patch.object(self.appmod, "_totp_check", rebind):
            r = self.client.post("/api/totp/disable", data={
                "code": _code(secret), "password": PW})
        self.assertEqual(409, r.status_code, r.text)
        self.assertIsNotNone(self._secret())


if __name__ == "__main__":
    unittest.main()
