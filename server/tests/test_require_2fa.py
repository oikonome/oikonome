"""hosted accounts must enroll a second factor; recovery codes.

/api/me.needs_2fa is true on hosted for any factor-less account, viewers
included. Enrolling TOTP (first factor) mints one-time recovery codes shown
once; a recovery code logs you in when the authenticator is lost, and is
burned on use.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import recovery
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


def _signup(client, email, hosted):
    if hosted:
        os.environ["OIKONOME_HOSTED"] = "1"
    # create the account directly, verified, on the admin connection
    admin = tenancy.admin_connect()
    try:
        tid = tenancy.create_tenant(admin, email)
        from oikonome.auth import passwords
        uid = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, verified_at) "
            "VALUES (%s, %s, %s, now()) RETURNING id",
            (tid, email, passwords.hash_password("correct-horse-battery"))
            ).fetchone()["id"]
    finally:
        admin.close()
    return uid


class Needs2FATests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.email = f"tfa-{uuid.uuid4().hex[:8]}@x.dev"
        self.uid = _signup(None, self.email, hosted=True)
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/login", data={
            "email": self.email, "password": "correct-horse-battery"},
            follow_redirects=False)
        assert r.status_code == 303, r.text

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def test_hosted_factorless_user_needs_2fa(self):
        me = self.client.get("/api/me").json()
        self.assertTrue(me["needs_2fa"])
        self.assertEqual(me["recovery_codes_left"], 0)

    def test_factorless_hosted_account_can_still_sign_out_of_mobile(self):
        """Signing out must never require enrolling first. The web door
        (/api/sessions/revoke) always had the exemption; the mobile door
        (/api/devices/revoke) did not, so a factorless phone got 'enroll a
        second factor' on every sign-out attempt — a dead end the client
        then misreported as a failed revocation."""
        r = self.client.post("/api/devices/revoke", json={"id": "not-a-real-id"})
        # the gate must not answer: any error past it (unknown id → 4xx
        # from the route itself) proves the door is reachable
        self.assertNotEqual(r.status_code, 403, r.text)
        self.assertNotIn("enroll a second factor", r.text)

    def test_self_host_never_needs_2fa(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        self.assertFalse(self.client.get("/api/me").json()["needs_2fa"])

    def test_totp_enroll_issues_recovery_codes_and_clears_gate(self):
        from oikonome.auth import totp as totp_mod
        secret = totp_mod.new_secret()
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret),
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        codes = r.json()["recovery_codes"]
        self.assertEqual(len(codes), recovery.BATCH)
        # gate cleared, codes counted
        me = self.client.get("/api/me").json()
        self.assertFalse(me["needs_2fa"])
        self.assertEqual(me["recovery_codes_left"], recovery.BATCH)

    def test_recovery_code_logs_in_and_burns(self):
        from oikonome.auth import totp as totp_mod
        secret = totp_mod.new_secret()
        codes = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret),
            "password": "correct-horse-battery"}).json()["recovery_codes"]
        code = codes[0]
        anon = TestClient(self.appmod.app)
        # wrong TOTP but valid recovery code → in
        r = anon.post("/api/login", data={
            "email": self.email, "password": "correct-horse-battery",
            "totp_code": code})
        self.assertEqual(r.status_code, 200, r.text)
        # burned: same code fails now
        anon2 = TestClient(self.appmod.app)
        from oikonome.web import security
        security._limiter._hits.clear()
        r2 = anon2.post("/api/login", data={
            "email": self.email, "password": "correct-horse-battery",
            "totp_code": code})
        self.assertEqual(r2.status_code, 401)
        # one code consumed
        self.assertEqual(self.client.get("/api/me").json()[
            "recovery_codes_left"], recovery.BATCH - 1)

    def test_passkeyless_totpless_but_has_passkey_row_satisfies(self):
        # a user with a passkey (no TOTP) does NOT need 2fa
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, %s, 0)",
                (self.uid, f"cred-{uuid.uuid4().hex}", "x"))
        finally:
            admin.close()
        self.assertFalse(self.client.get("/api/me").json()["needs_2fa"])

    def test_password_reset_clears_totp_and_recovery_codes(self):
        # attacker-planted TOTP (+ its recovery codes) must NOT survive a
        # password reset, or it locks the real owner out
        from oikonome.auth import reset, totp as totp_mod
        secret = totp_mod.new_secret()
        self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret),
            "password": "correct-horse-battery"})
        admin = tenancy.admin_connect()
        try:
            self.assertIsNotNone(admin.execute(
                "SELECT totp_secret FROM users WHERE id=%s",
                (self.uid,)).fetchone()["totp_secret"])
            self.assertGreater(admin.execute(
                "SELECT count(*) n FROM recovery_codes WHERE user_id=%s",
                (self.uid,)).fetchone()["n"], 0)
            rid = admin.execute(
                "INSERT INTO password_resets (user_id, token_hash, expires_at) "
                "VALUES (%s,%s, now()+interval '1 hour') RETURNING id",
                (self.uid, uuid.uuid4().hex)).fetchone()["id"]
            self.assertTrue(reset.consume(admin, rid, self.uid, "newhash$1"))
            admin.commit()
            # TOTP is cleared → the attacker's second factor is gone and the
            # owner logs in with just the new password (stale recovery codes
            # are now inert — login only consults them when totp_secret is set)
            self.assertIsNone(admin.execute(
                "SELECT totp_secret FROM users WHERE id=%s",
                (self.uid,)).fetchone()["totp_secret"])
        finally:
            admin.close()

    def test_regenerate_requires_step_up_and_voids_old(self):
        from oikonome.auth import totp as totp_mod
        secret = totp_mod.new_secret()
        old = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret),
            "password": "correct-horse-battery"}).json()["recovery_codes"]
        # password ALONE is not enough on a TOTP account — minting fresh
        # recovery codes (a TOTP-bypass primitive) demands a live code
        self.assertEqual(self.client.post(
            "/api/totp/recovery-regenerate",
            data={"password": "correct-horse-battery"}).status_code, 401)
        # wrong password → rejected
        self.assertEqual(self.client.post(
            "/api/totp/recovery-regenerate",
            data={"password": "nope",
                  "totp_code": _fresh_code(secret)}).status_code, 401)
        # right password + live code → new batch, old ones dead
        r = self.client.post("/api/totp/recovery-regenerate",
                             data={"password": "correct-horse-battery",
                                   "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200)
        new = r.json()["recovery_codes"]
        self.assertNotEqual(set(old), set(new))
        anon = TestClient(self.appmod.app)
        from oikonome.web import security
        security._limiter._hits.clear()
        # an old code no longer works
        self.assertEqual(anon.post("/api/login", data={
            "email": self.email, "password": "correct-horse-battery",
            "totp_code": old[0]}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
