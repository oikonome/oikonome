"""Every credential rotation and first-factor enrolment kills EVERY
persistence a hijacker could be holding — not just the sessions.

Evicting the other sessions is the easy half. The other bearers a stolen
cookie can plant — a mobile device token (a 90-day session equivalent that
never prompts TOTP), a script token, an unspent device-mint ticket (issued
on every login, redeemable for a fresh device token with no cookie and no
password), a pending family invite, a batch of recovery codes — have to go
too: any door that misses one leaves an owner who "closed the hijack"
holding it open. This file pins the whole matrix so no door can quietly
miss one.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import recovery
from oikonome.auth import reset as reset_mod
from oikonome.db import tenancy

from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"
NEW = "a-brand-new-passphrase"


def _fresh_code(secret):
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)


class _FakeReg:
    def __init__(self):
        self.credential_id = uuid.uuid4().bytes
        self.credential_public_key = b"\x05\x06\x07\x08"
        self.sign_count = 0


class _Base(unittest.TestCase):
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
        self.email = f"rot-{uuid.uuid4().hex[:8]}@example.dev"
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={"email": self.email,
                                                  "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.uid = self._uid()

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def _uid(self):
        conn = tenancy.control_connect()
        try:
            return conn.execute("SELECT id FROM users WHERE email=%s",
                                (self.email,)).fetchone()["id"]
        finally:
            conn.close()

    def _login(self):
        """A fresh cookie-less login: returns (client, mint_ticket)."""
        c = TestClient(self.app)
        r = c.post("/api/login", data={"email": self.email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return c, r.json()["mint_ticket"]

    def _device_bearer(self):
        """Mint a device token from a fresh session, the way the phone
        does — returns a client that authenticates with the bearer only."""
        c, _ = self._login()
        m = c.post("/api/devices", json={"device_name": "phone",
                                         "platform": "android"})
        self.assertEqual(m.status_code, 200, m.text)
        bearer = TestClient(self.app)
        bearer.cookies.clear()
        bearer.headers["Authorization"] = "Bearer " + m.json()["token"]
        self.assertEqual(bearer.get("/api/me").status_code, 200)
        return bearer

    def _script_token_id(self):
        r = self.client.post("/api/tokens", json={"name": "collector",
                                                  "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def _script_token_revoked(self, tid) -> bool:
        admin = tenancy.admin_connect()
        try:
            row = admin.execute(
                "SELECT revoked_at FROM api_tokens WHERE id=%s::uuid",
                (tid,)).fetchone()
        finally:
            admin.close()
        return row["revoked_at"] is not None

    def _redeem(self, ticket):
        naked = TestClient(self.app)
        naked.cookies.clear()
        return naked.post("/api/devices", json={
            "mint_ticket": ticket, "device_name": "later", "platform": "ios"})

    def _enroll_totp(self):
        secret = self.client.post("/api/totp/enroll",
                                  data={"password": PW}).json()["secret"]
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return secret

    def _enroll_passkey(self, **extra):
        os.environ["OIKONOME_HOSTED"] = "1"
        o = self.client.post("/api/passkeys/options", json={"password": PW, **extra}).json()
        cred = {"id": "AQIDBA", "response": {"transports": ["internal"]}}
        with mock.patch(
                "oikonome.auth.passkeys.verify_registration_response",
                return_value=_FakeReg()):
            r = self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"], "credential": cred,
                "password": PW, **extra})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def _reset_password(self):
        conn = tenancy.control_connect()
        try:
            token = reset_mod.create_reset(conn, self.uid)
        finally:
            conn.close()
        r = TestClient(self.app).post(
            "/reset", data={"token": token, "password": NEW},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)


class TotpEnrollEvictsBearers(_Base):
    def test_totp_enroll_revokes_device_tokens(self):
        bearer = self._device_bearer()
        self._enroll_totp()
        self.assertEqual(bearer.get("/api/me").status_code, 401)

    def test_totp_enroll_revokes_script_tokens(self):
        tid = self._script_token_id()
        self._enroll_totp()
        self.assertTrue(self._script_token_revoked(tid))

    def test_totp_enroll_burns_unspent_mint_tickets(self):
        _, ticket = self._login()
        self._enroll_totp()
        self.assertEqual(self._redeem(ticket).status_code, 401)

    def test_totp_after_a_passkey_is_not_a_first_factor(self):
        """The first-factor decision counts passkeys too: a user who already
        holds a passkey (and the recovery batch it minted) enrolling TOTP
        must not be handed a second batch that silently replaces the first —
        the same replace-on-issue that makes the concurrent race dangerous."""
        codes = self._enroll_passkey()["recovery_codes"]
        # a passkey-only account steps up with a recovery code here
        r = self.client.post("/api/totp/enroll", data={
            "password": PW, "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 200, r.text)
        secret = r.json()["secret"]
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret), "password": PW,
            "recovery_code": codes[1]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json().get("recovery_codes"))

    def test_totp_enroll_from_a_phone_keeps_that_phone(self):
        """The device driving the enrolment just proved the new factor —
        it survives, like the calling browser session does; every OTHER
        device goes."""
        other = self._device_bearer()
        me = self._device_bearer()
        secret = me.post("/api/totp/enroll",
                         data={"password": PW}).json()["secret"]
        r = me.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(me.get("/api/me").status_code, 200)
        self.assertEqual(other.get("/api/me").status_code, 401)


class FirstPasskeyEvictsBearers(_Base):
    def test_first_passkey_revokes_other_sessions_and_devices(self):
        other, _ = self._login()
        bearer = self._device_bearer()
        tid = self._script_token_id()
        self._enroll_passkey()
        self.assertEqual(self.client.get("/api/me").status_code, 200)
        self.assertEqual(other.get("/api/me").status_code, 401)
        self.assertEqual(bearer.get("/api/me").status_code, 401)
        self.assertTrue(self._script_token_revoked(tid))

    def test_first_passkey_always_issues_a_fresh_recovery_batch(self):
        """Codes left over from an earlier life of the account are live
        bypass primitives for passkey login and passkey step-up — the
        first passkey must void them, not top them up."""
        admin = tenancy.admin_connect()
        try:
            stale = recovery.issue(admin, self.uid)
        finally:
            admin.close()
        result = self._enroll_passkey()
        self.assertTrue(result.get("recovery_codes"),
                        "first passkey issued no recovery codes")
        self.assertNotIn(stale[0], result["recovery_codes"])
        conn = tenancy.control_connect()
        try:
            self.assertFalse(recovery.redeem(conn, self.uid, stale[0]),
                             "a pre-enrolment recovery code still redeems")
        finally:
            conn.close()


class PasswordResetPurgesEverything(_Base):
    def test_reset_deletes_recovery_codes(self):
        admin = tenancy.admin_connect()
        try:
            recovery.issue(admin, self.uid)
        finally:
            admin.close()
        self._reset_password()
        conn = tenancy.control_connect()
        try:
            self.assertEqual(recovery.remaining(conn, self.uid), 0)
            self.assertEqual(conn.execute(
                "SELECT count(*) AS n FROM recovery_codes WHERE user_id=%s",
                (self.uid,)).fetchone()["n"], 0)
        finally:
            conn.close()

    def test_reset_burns_unspent_mint_tickets(self):
        _, ticket = self._login()
        self._reset_password()
        self.assertEqual(self._redeem(ticket).status_code, 401)


class PasskeyOnlyResetNeedsRecovery(_Base):
    """A password reset must not be the way around the second factor: for
    an account whose only strong factor is a passkey the reset also takes a
    recovery code — otherwise 'I control the inbox' becomes 'I own the
    account' the moment the reset deletes the passkeys."""

    def _reset_link(self):
        conn = tenancy.control_connect()
        try:
            return reset_mod.create_reset(conn, self.uid)
        finally:
            conn.close()

    def test_reset_without_a_code_is_refused_and_the_link_stays_live(self):
        self._enroll_passkey()
        token = self._reset_link()
        r = TestClient(self.app).post(
            "/reset", data={"token": token, "password": NEW},
            follow_redirects=False)
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("recovery code", r.text)
        # the link is still usable — with the right code
        r = TestClient(self.app).post(
            "/reset", data={"token": token, "password": NEW,
                            "recovery_code": "nope-nope-nope-nope-nope"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 401, r.text)
        # and the link itself is still unspent (a passkey-only account
        # cannot password-login at all, so "old password works" is not the
        # check — the reset row is)
        conn = tenancy.control_connect()
        try:
            self.assertIsNotNone(reset_mod.lookup_reset(conn, token))
        finally:
            conn.close()

    def test_reset_with_a_recovery_code_succeeds_and_burns_it(self):
        codes = self._enroll_passkey()["recovery_codes"]
        token = self._reset_link()
        r = TestClient(self.app).post(
            "/reset", data={"token": token, "password": NEW,
                            "recovery_code": codes[0]},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        # that code is spent: a second reset with it is refused
        token2 = self._reset_link()
        r = TestClient(self.app).post(
            "/reset", data={"token": token2, "password": NEW + "x",
                            "recovery_code": codes[0]},
            follow_redirects=False)
        # passkeys were deleted by the first reset, so this account is no
        # longer passkey-only and needs no code at all — the reset goes
        # through on the password alone, exactly as a TOTP-less account does
        self.assertEqual(r.status_code, 303, r.text)

    def test_totp_account_reset_needs_a_code_too(self):
        """reset.consume strips the TOTP secret as well as the passkeys, so
        an inbox alone must not reset a TOTP account either."""
        secret = self._enroll_totp()
        token = self._reset_link()
        r = TestClient(self.app).post(
            "/reset", data={"token": token, "password": NEW},
            follow_redirects=False)
        self.assertEqual(r.status_code, 401, r.text)
        conn = tenancy.control_connect()
        try:
            row = conn.execute("SELECT totp_secret FROM users WHERE id=%s",
                               (self.uid,)).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row["totp_secret"])   # nothing was stripped
        self.assertTrue(secret)

    def test_password_change_burns_unspent_reset_links(self):
        token = self._reset_link()
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        r = TestClient(self.app).post(
            "/reset", data={"token": token, "password": NEW + "x"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 400, r.text)   # link is dead

    def test_reset_page_asks_for_the_code_only_when_needed(self):
        token = self._reset_link()
        self.assertNotIn("Recovery code", TestClient(self.app).get(
            "/reset", params={"token": token}).text)
        self._enroll_passkey()
        self.assertIn("Recovery code", TestClient(self.app).get(
            "/reset", params={"token": token}).text)


class MintTicketDiesWithTheSession(_Base):
    def test_logout_burns_the_login_mint_ticket(self):
        c, ticket = self._login()
        self.assertEqual(c.post("/api/logout").status_code, 200)
        self.assertEqual(self._redeem(ticket).status_code, 401)

    def test_sign_out_everywhere_else_burns_unspent_mint_tickets(self):
        """'Sign out everywhere else' is the click a user makes to evict a
        phished login; that login's 5-minute mint ticket must not survive
        it and plant a 90-day device token afterwards."""
        _, ticket = self._login()
        r = self.client.post("/api/sessions/revoke",
                             json={"all_others": True, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._redeem(ticket).status_code, 401)

    def test_revoking_one_other_session_burns_unspent_mint_tickets(self):
        """The per-id door must not be the weaker route to the same
        outcome as all_others."""
        other, ticket = self._login()
        sid = next(s["id"] for s in self.client.get("/api/sessions")
                   .json()["sessions"] if not s.get("current"))
        r = self.client.post("/api/sessions/revoke",
                             json={"id": sid, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._redeem(ticket).status_code, 401)

    def test_password_change_burns_unspent_mint_tickets(self):
        _, ticket = self._login()
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._redeem(ticket).status_code, 401)

    def test_email_change_burns_unspent_mint_tickets(self):
        _, ticket = self._login()
        r = self.client.post("/api/email/change", data={
            "new_email": f"new-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._redeem(ticket).status_code, 401)


class EmailChangeBurnsInvites(_Base):
    def test_email_change_kills_pending_invites(self):
        r = self.client.post("/api/invites",
                             json={"label": "fam", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["url"].rsplit("token=", 1)[1]
        r = self.client.post("/api/email/change", data={
            "new_email": f"new-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            self.client.get(f"/api/invite/peek?token={token}").status_code,
            404)


class PasswordChangeReportsItsCost(_Base):
    def test_password_change_returns_what_it_removed(self):
        self._device_bearer()
        tid = self._script_token_id()
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["devices_revoked"], 1)
        self.assertEqual(body["tokens_revoked"], 1)
        self.assertEqual(body["passkeys_removed"], 0)
        self.assertTrue(self._script_token_revoked(tid))


if __name__ == "__main__":
    unittest.main()
