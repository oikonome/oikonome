"""Step-up auth and reset-token hygiene.

Enrolment — adding a second factor needed only a LIVE SESSION. So a
stolen session could enroll its OWN authenticator (or passkey), and enroll
revokes every other session — theft escalated into locking the real owner
out of their own money, using the 2FA machinery as the weapon. The password
is the thing a session thief does not have.

The passkey side carries its own requirement: user verification is
REQUIRED, not merely preferred — passkey login skips TOTP, so the key must
verify the user itself. UV coverage lives in tests/test_passkeys.py; this
file owns the step-up side.

Reset tokens — a "forgot password" click that minted another token valid
for an hour would leave five live keys under the mat after five clicks.
Minting burns the
older ones — the newest link is the only one that works, which is what
anyone clicking "resend" already assumes.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import reset
from oikonome.auth import totp as totp_mod
from oikonome.db import tenancy

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


class TotpStepUpTests(unittest.TestCase):
    def setUp(self):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        from oikonome.web.app import app
        self.client = TestClient(app)
        self.email = f"stepup-{uuid.uuid4().hex[:8]}@example.dev"
        self.client.post("/api/signup",
                         data={"email": self.email, "password": PW})

    def test_first_enroll_without_password_is_refused(self):
        r = self.client.post("/api/totp/enroll")
        self.assertEqual(r.status_code, 403)         # elevation_required
        self.assertIn("elevation_required", r.text)

    def test_first_enroll_with_wrong_password_is_refused(self):
        r = self.client.post("/api/totp/enroll", data={"password": "nope"})
        self.assertEqual(r.status_code, 401)

    def test_first_enroll_with_password_works(self):
        r = self.client.post("/api/totp/enroll", data={"password": PW})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["qr"].startswith("data:image/svg+xml"))

    def test_confirm_without_password_cannot_bind_a_secret(self):
        # confirm is the route that BINDS and revokes other sessions, so
        # guarding only enroll would leave the real door open: a thief can
        # generate a secret locally and post it straight here
        secret = totp_mod.new_secret()
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 403)         # elevation_required
        self.assertIn("elevation_required", r.text)
        self.assertFalse(self.client.get("/api/me").json()["totp_enabled"])

    def test_confirm_with_password_enables_2fa(self):
        secret = self.client.post(
            "/api/totp/enroll", data={"password": PW}).json()["secret"]
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret),
            "password": PW})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.client.get("/api/me").json()["totp_enabled"])

    def test_re_enroll_uses_the_current_code_not_the_password(self):
        # once 2FA exists, proving the CURRENT authenticator is a stronger
        # claim than the password — the password must not be a way around it
        secret = self.client.post(
            "/api/totp/enroll", data={"password": PW}).json()["secret"]
        self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret),
            "password": PW})
        # a legacy body (password, no code) gets the old 401 — the store
        # apps prompt for the code on it
        r = self.client.post("/api/totp/enroll", data={"password": PW})
        self.assertEqual(r.status_code, 401)
        self.assertIn("current one-time code", r.text)
        r = self.client.post("/api/totp/enroll",
                             data={"current_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200)


class ResetTokenTests(unittest.TestCase):
    def setUp(self):
        _ensure_db()
        self.conn = _control()
        self.addCleanup(self.conn.close)
        admin = tenancy.admin_connect()
        self.addCleanup(admin.close)
        tid = tenancy.create_tenant(admin, f"reset-{uuid.uuid4().hex[:8]}")
        self.uid = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s,%s,'x') RETURNING id",
            (tid, f"reset-{uuid.uuid4().hex[:6]}@example.dev")).fetchone()["id"]

    def test_minting_burns_the_previous_token(self):
        first = reset.create_reset(self.conn, self.uid)
        self.assertIsNotNone(reset.lookup_reset(self.conn, first))
        second = reset.create_reset(self.conn, self.uid)
        # the old link stops working the moment a new one is issued
        self.assertIsNone(reset.lookup_reset(self.conn, first))
        self.assertIsNotNone(reset.lookup_reset(self.conn, second))

    def test_five_clicks_leave_exactly_one_live_token(self):
        tokens = [reset.create_reset(self.conn, self.uid) for _ in range(5)]
        live = [t for t in tokens if reset.lookup_reset(self.conn, t)]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0], tokens[-1])

    def test_another_users_token_is_untouched(self):
        admin = tenancy.admin_connect()
        self.addCleanup(admin.close)
        tid2 = tenancy.create_tenant(admin, f"reset-other-{uuid.uuid4().hex[:8]}")
        other = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s,%s,'x') RETURNING id",
            (tid2, f"reset-other-{uuid.uuid4().hex[:6]}@example.dev")).fetchone()["id"]
        theirs = reset.create_reset(self.conn, other)
        reset.create_reset(self.conn, self.uid)      # our mint
        self.assertIsNotNone(reset.lookup_reset(self.conn, theirs))


if __name__ == "__main__":
    unittest.main()
