"""A credential rotation or factor re-enrolment ends any recovery still
waiting to land.

A cooling-off factor-clearing recovery strips every factor when it lands.
If the owner changes their password, re-binds an authenticator, adds a
passkey, turns TOTP off, mints fresh recovery codes or steps up with a real
second factor while one is pending, the recovery must stop there: landing
later, it would strip the credentials the owner has just put on. Its
mailed cancel link then answers that there is nothing left to stop.

The last test keeps the rule structural: every door in web/app.py that
re-enrols a factor or pauses outbound for a rotation must go through the
one helper that voids pending recoveries.
"""
import ast
import datetime as dt
import inspect
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import factor_reset
from oikonome.auth import reset as reset_mod
from oikonome.db import crypto, tenancy

from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"


def _code(secret):
    from oikonome.auth import totp
    clear_totp_burn()
    return totp.code_now(secret)


class _Base(unittest.TestCase):
    hosted = False

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        if cls.hosted:
            os.environ["OIKONOME_HOSTED"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    @classmethod
    def tearDownClass(cls):
        if cls.hosted:
            os.environ.pop("OIKONOME_HOSTED", None)

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.email = f"rotcancel-{uuid.uuid4().hex[:10]}@example.dev"
        self.client = TestClient(self.app)
        data = {"email": self.email, "password": PW}
        if self.hosted:
            from oikonome.auth import signup_invites
            admin = tenancy.admin_connect()
            try:
                data["invite"] = signup_invites.mint(admin, self.email)
            finally:
                admin.close()
        r = self.client.post("/api/signup", data=data)
        self.assertEqual(r.status_code, 200, r.text)
        self.uid = self._sql("SELECT id FROM users WHERE email=%s",
                             (self.email,))["id"]

    def tearDown(self):
        self._sql("DELETE FROM passkeys WHERE user_id=%s", (self.uid,),
                  fetch=False)

    def _sql(self, q, args, fetch=True):
        admin = tenancy.admin_connect()
        try:
            cur = admin.execute(q, args)
            return cur.fetchone() if fetch else None
        finally:
            admin.close()

    def _give_totp(self):
        from oikonome.auth import totp
        self.secret = totp.new_secret()
        self._sql("UPDATE users SET totp_secret=%s WHERE id=%s",
                  (crypto.encrypt_cp(self.secret), self.uid), fetch=False)

    def _start_recovery(self):
        """A recovery started after the session, already landed — the
        worst case: the next reset link would strip every factor."""
        conn = tenancy.control_connect()
        try:
            _row, cancel = factor_reset.request(
                conn, self.uid, cooling_off=dt.timedelta(seconds=-1))
            reset_token = reset_mod.create_reset(conn, self.uid)
        finally:
            conn.close()
        self.assertIsNotNone(self._matured())
        return cancel, reset_token

    def _matured(self):
        conn = tenancy.control_connect()
        try:
            return factor_reset.pending(conn, self.uid)
        finally:
            conn.close()

    def assertRecoveryEnded(self, cancel, reason="rotation"):
        self.assertIsNone(self._matured())
        row = self._sql(
            "SELECT cancel_reason FROM factor_resets WHERE user_id=%s "
            "ORDER BY requested_at DESC LIMIT 1", (self.uid,))
        self.assertEqual(row["cancel_reason"], reason)
        r = TestClient(self.app).get(f"/recover/cancel?token={cancel}")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("no longer pending", r.text)


class SelfHostDoors(_Base):
    def test_password_change_ends_it(self):
        cancel, reset_token = self._start_recovery()
        r = self.client.post("/api/password/change", json={
            "current_password": PW,
            "new_password": "another-long-passphrase-42"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertRecoveryEnded(cancel)
        conn = tenancy.control_connect()
        try:
            self.assertIsNone(reset_mod.lookup_reset(conn, reset_token))
        finally:
            conn.close()

    def test_totp_confirm_ends_it(self):
        secret = self.client.post("/api/totp/enroll",
                                  data={"password": PW}).json()["secret"]
        cancel, _ = self._start_recovery()
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertRecoveryEnded(cancel)

    def test_totp_disable_ends_it(self):
        self._give_totp()
        cancel, _ = self._start_recovery()
        r = self.client.post("/api/totp/disable", data={
            "code": _code(self.secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertRecoveryEnded(cancel)

    def test_recovery_regenerate_ends_it(self):
        self._give_totp()
        cancel, _ = self._start_recovery()
        r = self.client.post("/api/totp/recovery-regenerate", data={
            "password": PW, "totp_code": _code(self.secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertRecoveryEnded(cancel)

    def test_elevate_with_a_live_code_ends_it(self):
        self._give_totp()
        cancel, _ = self._start_recovery()
        r = self.client.post("/api/auth/elevate", json={
            "password": PW, "totp_code": _code(self.secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertRecoveryEnded(cancel, reason="step-up")

    def test_elevate_with_a_bare_password_on_a_factorless_account_does_not(
            self):
        self._start_recovery()
        r = self.client.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(self._matured())


class _FakeReg:
    def __init__(self):
        self.credential_id = uuid.uuid4().bytes
        self.credential_public_key = b"\x05\x06\x07\x08"
        self.sign_count = 0


class HostedPasskeyDoors(_Base):
    hosted = True

    def _register(self, **proof):
        o = self.client.post("/api/passkeys/options",
                             json={"password": PW, **proof}).json()
        with mock.patch(
                "oikonome.auth.passkeys.verify_registration_response",
                return_value=_FakeReg()):
            return self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"],
                "credential": {"id": "AQIDBA",
                               "response": {"transports": ["internal"]}},
                "label": "k", "password": PW, **proof})

    def test_first_passkey_ends_it(self):
        cancel, _ = self._start_recovery()
        r = self._register()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertRecoveryEnded(cancel)

    def test_an_added_passkey_ends_it(self):
        self._give_totp()
        cancel, _ = self._start_recovery()
        r = self._register(totp_code=_code(self.secret))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertRecoveryEnded(cancel)


class EveryRotationDoorEndsPendingRecoveries(unittest.TestCase):
    """A door that re-enrols a factor or rotates a credential marks itself
    by pausing the household's outbound destinations, re-verifying its SMS
    number, minting recovery codes or binding a passkey. Every such door
    must call `_end_pending_recoveries` (directly or through
    `_revoke_bearer_persistence`) — a new door that forgets is the bug this
    rule exists to stop recurring."""

    MARKERS = ("_unverify_sms_number", "recovery.issue", "bind()")
    VOIDERS = ("_end_pending_recoveries(", "_revoke_bearer_persistence(")

    def test_every_marked_door_voids_recoveries(self):
        import oikonome.web.app as appmod
        tree = ast.parse(inspect.getsource(appmod))
        src = inspect.getsource(appmod).splitlines()
        missing = []
        checked = 0
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            body = "\n".join(src[node.lineno - 1:node.end_lineno])
            if not any(m in body for m in self.MARKERS):
                continue
            checked += 1
            if not any(v in body for v in self.VOIDERS):
                missing.append(node.name)
        self.assertGreaterEqual(checked, 5)
        self.assertEqual(missing, [])


class AReclaimMoveOutEndsThePendingRecovery(unittest.TestCase):
    """The reclaim's member road moves a person into a household of their
    own with a password the clicker just chose. A recovery that was
    pending on the account must not follow them there and land."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        self.sent = []
        self._p = [
            mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                         "OIKONOME_OPEN_SIGNUP": "1"}),
            mock.patch.object(self.appmod, "DEV_MODE", False),
            mock.patch.object(self.appmod, "_deliver_verification"),
            mock.patch.object(
                self.appmod, "_deliver_reclaim",
                side_effect=lambda e, t: self.sent.append((e, t))),
        ]
        for p in self._p:
            p.start()
        from oikonome.web import security
        security._limiter._hits.clear()

    def tearDown(self):
        for p in reversed(self._p):
            p.stop()

    def _signup(self, email):
        return TestClient(self.appmod.app).post(
            "/api/signup", data={"email": email, "password": PW,
                                 "invite": "", "ref": ""})

    def test_the_move_out_cancels_it(self):
        host = f"rhost-{uuid.uuid4().hex[:8]}@example.dev"
        member = f"rmem-{uuid.uuid4().hex[:8]}@example.dev"
        self.assertEqual(self._signup(host).status_code, 200)
        admin = tenancy.admin_connect()
        try:
            tid = admin.execute("SELECT tenant_id FROM users WHERE email=%s",
                                (host,)).fetchone()["tenant_id"]
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'member') RETURNING id",
                (tid, member)).fetchone()["id"]
        finally:
            admin.close()
        self.assertEqual(self._signup(member).status_code, 409)
        conn = tenancy.control_connect()
        try:
            factor_reset.request(conn, uid,
                                 cooling_off=dt.timedelta(seconds=-1))
        finally:
            conn.close()
        r = TestClient(self.appmod.app).post(
            "/signup/reclaim", data={"token": self.sent[-1][1],
                                     "password": "clicker-chosen-password"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        conn = tenancy.control_connect()
        try:
            self.assertIsNone(factor_reset.pending(conn, uid))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
