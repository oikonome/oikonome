"""Passkey-only accounts: step-up is demanded where a password cannot stand in.

Password-only *login* is refused for a hosted account whose only strong
factor is a passkey (the password login path never presents the passkey).
Every posture-change *step-up* must hold the same line: trusting the
password ALONE there would let a session-thief holding the password change
it, delete the account, or add their own passkey.

So for a passkey-only hosted account, step-up demands the passkey or a
one-time RECOVERY code (which a password-only thief does not hold), and
enrolling a passkey as the first strong factor issues recovery codes so a
legitimate owner has them. A lost-passkey owner can also use a recovery
code to log in.
"""

import os
import unittest
import uuid
from unittest import mock

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


def _signup(email):
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


class PasskeyOnlyStepupTests(unittest.TestCase):
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
        os.environ["OIKONOME_HOSTED"] = "1"
        self.email = f"pk-{uuid.uuid4().hex[:8]}@x.dev"
        self.uid = _signup(self.email)
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/login", data={
            "email": self.email, "password": "correct-horse-battery"},
            follow_redirects=False)
        assert r.status_code == 303, r.text

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def _make_passkey_only(self, with_codes=True):
        """Give the account a passkey (its only strong factor) and, when
        asked, a fresh batch of recovery codes. Returns the plaintext codes."""
        admin = tenancy.admin_connect()
        try:
            # base64url-decodable (stepup_options/verify decode both):
            # "cred" + 32 hex = 36 chars, all in the b64u alphabet; the
            # public key is b64u("public-key") — verification is mocked
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, %s, 0)",
                (self.uid, f"cred{uuid.uuid4().hex}", "cHVibGljLWtleQ"))
            codes = recovery.issue(admin, self.uid) if with_codes else []
        finally:
            admin.close()
        return codes

    # ---- login ---------------------------------------------------------
    def test_passkey_only_password_login_blocked(self):
        self._make_passkey_only()
        anon = TestClient(self.appmod.app)
        r = anon.post("/api/login", data={
            "email": self.email, "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("passkey_required", r.text)

    def test_passkey_only_recovery_login_escape(self):
        codes = self._make_passkey_only()
        anon = TestClient(self.appmod.app)
        r = anon.post("/api/login", data={
            "email": self.email, "password": "correct-horse-battery",
            "totp_code": codes[0]})
        self.assertEqual(r.status_code, 200, r.text)
        # burned — same code fails now
        from oikonome.web import security
        security._limiter._hits.clear()
        anon2 = TestClient(self.appmod.app)
        r2 = anon2.post("/api/login", data={
            "email": self.email, "password": "correct-horse-battery",
            "totp_code": codes[0]})
        self.assertEqual(r2.status_code, 401)

    # ---- step-up -------------------------------------------------------
    def test_passkey_only_password_change_needs_recovery(self):
        self._make_passkey_only()
        # password alone must be refused: the
        # second proof now rides the elevation window, which a
        # passkey-only account can only open with the passkey / a code
        r = self.client.post("/api/password/change", json={
            "current_password": "correct-horse-battery",
            "new_password": "brand-new-passphrase"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("elevation_required", r.text)
        r = self.client.post("/api/auth/elevate", json={
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("recovery_required", r.text)

    def test_passkey_only_password_change_with_recovery(self):
        codes = self._make_passkey_only()
        r = self.client.post("/api/password/change", json={
            "current_password": "correct-horse-battery",
            "new_password": "brand-new-passphrase",
            "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_passkey_only_account_delete_needs_recovery(self):
        self._make_passkey_only()
        r = self.client.post("/api/account/delete", data={
            "password": "correct-horse-battery", "confirm": self.email})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("recovery_required", r.text)

    def test_passkey_only_token_mint_and_revoke_with_recovery(self):
        """Token mint/revoke must forward recovery_code to _step_up, or a
        passkey-only account cannot mint a script token at all. Password
        alone still 401s; password + recovery code goes through, for both
        mint and revoke."""
        codes = self._make_passkey_only()
        r = self.client.post("/api/tokens", json={
            "name": "collector", "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("recovery_required", r.text)
        r = self.client.post("/api/tokens", json={
            "name": "collector", "password": "correct-horse-battery",
            "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 200, r.text)
        tok_id = r.json()["id"]
        r = self.client.post("/api/tokens/revoke", json={
            "id": tok_id, "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 401, r.text)
        r = self.client.post("/api/tokens/revoke", json={
            "id": tok_id, "password": "correct-horse-battery",
            "recovery_code": codes[1]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_session_revoke_by_id_needs_password(self):
        """Revoking another session by id demands the password — and
        succeeds when it is supplied, which a client that sends none never
        gets to see."""
        # mint a second session
        other = TestClient(self.appmod.app)
        r = other.post("/login", data={
            "email": self.email, "password": "correct-horse-battery"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        sess = self.client.get("/api/sessions").json()["sessions"]
        target = next(s for s in sess if not s["current"])
        r = self.client.post("/api/sessions/revoke", json={"id": target["id"]})
        self.assertEqual(r.status_code, 403)         # elevation_required
        r = self.client.post("/api/sessions/revoke", json={
            "id": target["id"], "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["revoked"], 1)

    def test_totp_account_stepup_unaffected(self):
        """The common case — a TOTP account — must NOT be asked for a recovery
        code at step-up (its caller already verified the TOTP)."""
        from oikonome.auth import totp as totp_mod
        secret = totp_mod.new_secret()
        r0 = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret),
            "password": "correct-horse-battery"})
        self.assertEqual(r0.status_code, 200, r0.text)
        # password + TOTP, no recovery_code → still works
        r = self.client.post("/api/password/change", json={
            "current_password": "correct-horse-battery",
            "new_password": "brand-new-passphrase",
            "totp_code": _fresh_code(secret)})
        self.assertEqual(r.status_code, 200, r.text)

    # ---- the passkey ITSELF satisfies step-up --------------------------
    def _stepup_ticket(self, cred_id):
        """Inline assertion → ticket, WebAuthn verification mocked like
        test_passkeys (the library call is not what's under test)."""
        o = self.client.post("/api/stepup/passkey/options", json={})
        self.assertEqual(o.status_code, 200, o.text)

        class _FakeAuth:
            new_sign_count = 3
        with mock.patch(
                "oikonome.auth.passkeys.verify_authentication_response",
                return_value=_FakeAuth()) as var:
            r = self.client.post("/api/stepup/passkey", json={
                "challenge_id": o.json()["challenge_id"],
                "credential": {"id": cred_id}})
        self.assertEqual(r.status_code, 200, r.text)
        # the assertion must carry its own second factor
        self.assertTrue(var.call_args.kwargs.get("require_user_verification"))
        return r.json()["stepup_ticket"]

    def _cred_id(self):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT credential_id FROM passkeys WHERE user_id=%s",
                (self.uid,)).fetchone()["credential_id"]
        finally:
            admin.close()

    def test_passkey_assertion_satisfies_stepup(self):
        self._make_passkey_only(with_codes=False)   # no recovery codes at all
        ticket = self._stepup_ticket(self._cred_id())
        r = self.client.post("/api/password/change", json={
            "current_password": "correct-horse-battery",
            "new_password": "brand-new-passphrase",
            "recovery_code": ticket})
        self.assertEqual(r.status_code, 200, r.text)

    def test_stepup_ticket_is_single_use(self):
        """A redeemed step-up ticket is dead — replaying it must fail
        even though the same password would otherwise open the door.

        Every credential-rotating endpoint now evicts passkeys, so the
        account is no longer passkey-only after the first change; the
        second attempt re-enrols before replaying, otherwise the replay
        would be waved through on the password alone and the burned
        ticket never consulted."""
        self._make_passkey_only(with_codes=False)
        ticket = self._stepup_ticket(self._cred_id())
        r = self.client.post("/api/email/change", data={
            "new_email": f"n1-{self.email}",
            "password": "correct-horse-battery",
            "recovery_code": ticket})
        self.assertEqual(r.status_code, 200, r.text)
        self._make_passkey_only(with_codes=False)
        r2 = self.client.post("/api/email/change", data={
            "new_email": f"n2-{self.email}",
            "password": "correct-horse-battery",
            "recovery_code": ticket})
        self.assertEqual(r2.status_code, 401, r2.text)
        self.assertIn("recovery_required", r2.text)

    def test_stepup_ticket_hashed_at_rest(self):
        """The bearer ticket is sha256'd in webauthn_challenges, never
        stored plaintext — a DB read during the 5-min TTL can't redeem it."""
        import hashlib
        self._make_passkey_only(with_codes=False)
        ticket = self._stepup_ticket(self._cred_id())
        admin = tenancy.admin_connect()
        try:
            stored = [r["challenge"] for r in admin.execute(
                "SELECT challenge FROM webauthn_challenges "
                "WHERE purpose='stepup-ticket' AND user_id=%s",
                (self.uid,)).fetchall()]
        finally:
            admin.close()
        self.assertTrue(stored)
        self.assertNotIn(ticket, stored)                     # not plaintext
        self.assertIn(hashlib.sha256(ticket.encode()).hexdigest(), stored)

    def test_stepup_ticket_is_user_bound(self):
        """A ticket minted for one account is worthless on another —
        challenge AND ticket rows are keyed to the asserting user."""
        self._make_passkey_only(with_codes=False)
        ticket = self._stepup_ticket(self._cred_id())
        other_email = f"pk-{uuid.uuid4().hex[:8]}@x.dev"
        other_uid = _signup(other_email)
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, %s, 0)",
                (other_uid, f"cred{uuid.uuid4().hex}", "x"))
        finally:
            admin.close()
        from oikonome.auth import passkeys as pk_mod
        admin = tenancy.admin_connect()
        try:
            # (redeemed, passkey_id) — the id names the credential the
            # assertion was signed with, so a rotation can keep that one
            # and evict every other
            ok, _ = pk_mod.redeem_stepup_ticket(admin, other_uid, ticket)
            self.assertFalse(ok)
            # still redeemable by its rightful owner afterwards
            ok, pk_id = pk_mod.redeem_stepup_ticket(admin, self.uid, ticket)
            self.assertTrue(ok)
            self.assertIsNotNone(pk_id)
        finally:
            admin.close()

    def test_stepup_options_requires_enrolled_passkey(self):
        """No passkey → no options. The hosted forced-2FA gate answers
        first (403 enroll-a-factor); either way, no challenge is minted."""
        r = self.client.post("/api/stepup/passkey/options", json={})
        self.assertIn(r.status_code, (400, 403), r.text)


class ViewerStepupTests(unittest.TestCase):
    """A view-only member owns their OWN second factor, so the inline
    WebAuthn step-up must be open to viewers. A role gate that 403s the
    step-up POSTs before the handler runs would leave a passkey-only viewer
    burning a finite recovery code on every posture change. The endpoints only assert the caller's own passkey and mint a
    self-scoped single-use ticket — opening them grants no privilege."""

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
        os.environ["OIKONOME_HOSTED"] = "1"
        self.email = f"vw-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, self.email)
            from oikonome.auth import passwords
            self.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s, %s, %s, 'viewer', now()) "
                "RETURNING id",
                (tid, self.email,
                 passwords.hash_password("correct-horse-battery"))
                ).fetchone()["id"]
        finally:
            admin.close()
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/login", data={
            "email": self.email, "password": "correct-horse-battery"},
            follow_redirects=False)
        assert r.status_code == 303, r.text
        # the passkey lands AFTER login — once it exists, password-only
        # login is refused for the account (the factor must be presented)
        admin = tenancy.admin_connect()
        try:
            # base64url-decodable credential id, like the owner-side tests
            self.cred_id = f"cred{uuid.uuid4().hex}"
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, %s, 0)",
                (self.uid, self.cred_id, "cHVibGljLWtleQ"))
        finally:
            admin.close()

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def _stepup_ticket(self):
        o = self.client.post("/api/stepup/passkey/options", json={})
        self.assertEqual(o.status_code, 200, o.text)

        class _FakeAuth:
            new_sign_count = 3
        with mock.patch(
                "oikonome.auth.passkeys.verify_authentication_response",
                return_value=_FakeAuth()):
            r = self.client.post("/api/stepup/passkey", json={
                "challenge_id": o.json()["challenge_id"],
                "credential": {"id": self.cred_id}})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["stepup_ticket"]

    def test_viewer_can_mint_a_stepup_ticket(self):
        self.assertTrue(self._stepup_ticket())

    def test_viewer_leaves_household_via_inline_stepup(self):
        """End-to-end: passkey-only viewer asserts the passkey, then the
        ticket satisfies the account/leave step-up — no recovery code
        spent, and the member row is gone."""
        ticket = self._stepup_ticket()
        r = self.client.post("/api/account/leave", data={
            "password": "correct-horse-battery", "recovery_code": ticket})
        self.assertEqual(r.status_code, 200, r.text)
        admin = tenancy.admin_connect()
        try:
            row = admin.execute("SELECT 1 FROM users WHERE id=%s",
                                (self.uid,)).fetchone()
        finally:
            admin.close()
        self.assertIsNone(row)


if __name__ == "__main__":
    unittest.main()
