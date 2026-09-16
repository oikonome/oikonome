"""A credential rotation keeps the key that PROVED the caller, and no other.

Changing the password or the email address evicts passkeys, because a
hijacked session could have enrolled one and WebAuthn login would outlive
every session and token the rotation revokes. On a hosted account whose
only strong factor IS a passkey, evicting all of them re-enables
password-only login at /api/login — the exact downgrade passkey_delete and
totp_disable each refuse — so exactly one key survives: the credential
behind this session's step-up ticket.

"Recently used" is not that key and cannot stand in for it. The step-up
door is reachable by any authenticated session with no elevation of its
own, so an attacker who plants a passkey can re-assert it every few minutes
and keep it as fresh as the owner's: a recency rule spares the planted key
alongside the real one, and on the lost-authenticator road — where the
owner's key is the stale one — it deletes the owner's key, keeps the
stranger's, and tells the owner that stranger's credential is their only
second factor.

When no passkey proved the caller (a recovery code, a password, a live TOTP
code) every key goes, which is where the password-reset road lands on the
same proof.
"""

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import passkeys as passkeys_mod
from oikonome.auth import passwords, recovery, sessions
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"
NEW = "brand-new-password-9"


class RotationKeepsTheProvingPasskey(unittest.TestCase):
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
        self._env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"})
        self._env.start()
        self.email = f"lf-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            self.tid = str(tenancy.create_tenant(admin, self.email))
            self.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (self.tid, self.email,
                 passwords.hash_password(PW))).fetchone()["id"]
            admin.commit()
        finally:
            admin.close()
        self.client = TestClient(self.appmod.app)

    def tearDown(self):
        self._env.stop()

    # ---- fixtures ------------------------------------------------------

    def _passkey(self, *, used: bool) -> str:
        """A credential on this account. `used` stamps last_used, which a
        passkey sign-in and a passkey step-up both do — and which an
        attacker holding a session can re-stamp at will, which is why it
        decides nothing here."""
        admin = tenancy.admin_connect()
        try:
            pk = admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count, last_used) VALUES (%s, %s, 'x', 0, "
                "CASE WHEN %s THEN now() ELSE NULL END) RETURNING id",
                (self.uid, f"cred-{uuid.uuid4().hex}", used)).fetchone()["id"]
            admin.commit()
        finally:
            admin.close()
        return str(pk)

    def _signed_in(self) -> None:
        """A live session for this account. Minted directly because
        password login is deliberately refused on a passkey-only hosted
        account."""
        admin = tenancy.admin_connect()
        try:
            token = sessions.create_session(admin, self.uid, self.tid, "ua")
            admin.commit()
        finally:
            admin.close()
        self.client.cookies.set(sessions.COOKIE_NAME, token)

    def _elevate_with_passkey(self, pk_id: str) -> None:
        """Elevate the way the step-up sheet does: a WebAuthn assertion
        against ONE credential mints a ticket that names it, and the
        elevate door redeems the ticket. The assertion itself needs a real
        authenticator, so the ticket is minted here exactly as
        `stepup_verify` mints it."""
        ticket = "pkstep-" + uuid.uuid4().hex
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                """INSERT INTO webauthn_challenges (purpose, user_id,
                                challenge, expires_at, passkey_id)
                   VALUES ('stepup-ticket', %s, %s, %s, %s)""",
                (self.uid, passkeys_mod._h(ticket),
                 dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5),
                 pk_id))
            admin.commit()
        finally:
            admin.close()
        r = self.client.post("/api/auth/elevate",
                             json={"stepup_ticket": ticket})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["elevated"])

    def _elevate_with_recovery_code(self) -> None:
        """The lost-authenticator road: a recovery code opens the window on
        a passkey-only hosted account, and no key has proved anything."""
        admin = tenancy.admin_connect()
        try:
            code = recovery.issue(admin, self.uid)[0]
            admin.commit()
        finally:
            admin.close()
        r = self.client.post("/api/auth/elevate", json={"recovery_code": code})
        self.assertEqual(r.status_code, 200, r.text)

    def _passkey_ids(self) -> set:
        admin = tenancy.admin_connect()
        try:
            return {str(r["id"]) for r in admin.execute(
                "SELECT id FROM passkeys WHERE user_id=%s",
                (self.uid,)).fetchall()}
        finally:
            admin.close()

    def _enroll_totp(self) -> None:
        from oikonome.auth import totp as totp_mod
        from oikonome.db import crypto
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET totp_secret=%s WHERE id=%s",
                          (crypto.encrypt_cp(totp_mod.new_secret()),
                           self.uid))
            admin.commit()
        finally:
            admin.close()

    # ---- password change ------------------------------------------------

    def test_password_change_keeps_only_the_key_that_proved_it(self):
        """The attacker's key is FRESHLY USED — planted on a hijacked
        session and re-asserted minutes ago — and it still goes. Only the
        credential named by this session's step-up ticket stays."""
        proving = self._passkey(used=True)
        strangers = self._passkey(used=True)
        self._signed_in()
        self._elevate_with_passkey(proving)
        r = self.client.post("/api/password/change",
                             json={"current_password": PW,
                                   "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["passkeys_removed"], 1)
        self.assertEqual(r.json()["passkeys_kept"], 1)
        self.assertEqual(self._passkey_ids(), {proving})
        self.assertNotIn(strangers, self._passkey_ids())

    def test_password_change_leaves_password_only_login_refused(self):
        """The reason one key is spared at all: without it the rotation
        drops the account to zero factors, and a password that opens nothing
        at the login door suddenly opens the whole household."""
        proving = self._passkey(used=True)
        self._signed_in()
        self._elevate_with_passkey(proving)
        self.assertEqual(self.client.post(
            "/api/password/change",
            json={"current_password": PW, "new_password": NEW}
            ).status_code, 200)
        from oikonome.web import security
        security._limiter._hits.clear()
        r = TestClient(self.appmod.app).post(
            "/api/login", data={"email": self.email, "password": NEW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("passkey_required", r.text)

    def test_password_change_on_the_recovery_road_evicts_every_key(self):
        """No key proved anything, so none is spared — including the fresh
        one a stranger keeps warm. Leaving it would hand the account back
        to them and call it "your only second factor"."""
        strangers = self._passkey(used=True)
        self._signed_in()
        self._elevate_with_recovery_code()
        r = self.client.post("/api/password/change",
                             json={"current_password": PW,
                                   "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["passkeys_removed"], 1)
        self.assertEqual(r.json()["passkeys_kept"], 0)
        self.assertEqual(self._passkey_ids(), set())
        self.assertNotIn(strangers, self._passkey_ids())

    def test_a_later_password_elevation_clears_the_earlier_passkey_proof(self):
        """The stamp is state on the session row, so it has to be replaced,
        not merged: a window re-opened with a password proves no key, and a
        rotation in that window must not still be sparing the one an
        earlier window recorded."""
        first = self._passkey(used=True)
        self._enroll_totp()          # so the password road can elevate
        self._signed_in()
        self._elevate_with_passkey(first)
        admin = tenancy.admin_connect()
        try:                          # drop TOTP again: passkey-only now
            admin.execute("UPDATE users SET totp_secret=NULL WHERE id=%s",
                          (self.uid,))
            admin.commit()
        finally:
            admin.close()
        self._elevate_with_recovery_code()
        r = self.client.post("/api/password/change",
                             json={"current_password": PW,
                                   "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["passkeys_kept"], 0)
        self.assertEqual(self._passkey_ids(), set())

    def test_an_account_with_totp_still_loses_every_passkey(self):
        """A second factor remains either way, so the eviction stays
        whole — the guard must not weaken the rotation it was added to."""
        proving = self._passkey(used=True)
        self._passkey(used=False)
        self._signed_in()
        self._elevate_with_passkey(proving)
        self._enroll_totp()
        r = self.client.post("/api/password/change",
                             json={"current_password": PW,
                                   "new_password": NEW,
                                   "totp_code": ""})
        self.assertEqual(r.status_code, 401, r.text)   # TOTP owed now
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET totp_secret=NULL WHERE id=%s",
                          (self.uid,))
            admin.commit()
        finally:
            admin.close()
        # the same rotation on a TOTP account, taken through the door the
        # elevation window opens
        self._enroll_totp()
        with mock.patch.object(self.appmod, "_totp_consume",
                               return_value=True):
            r = self.client.post("/api/password/change",
                                 json={"current_password": PW,
                                       "new_password": NEW,
                                       "totp_code": "123456"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["passkeys_removed"], 2)
        self.assertEqual(r.json()["passkeys_kept"], 0)
        self.assertEqual(self._passkey_ids(), set())

    # ---- a proof that names a key which is gone --------------------------

    def _delete_key_behind_the_stamp(self, pk_id: str) -> None:
        """Drop the row and leave the elevation stamp naming it, which is
        what any deletion that does not go through the API door leaves
        behind."""
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM passkeys WHERE id=%s", (pk_id,))
            admin.commit()
        finally:
            admin.close()

    def test_a_rotation_refuses_when_the_key_its_proof_names_is_gone(self):
        """A name is a proof only while the credential is there.

        `sessions.elevated_passkey_id` is deliberately not a foreign key,
        so the id outlives the row. Trusting a dangling name, the keep
        branch deletes every OTHER key to spare one that does not exist —
        on a passkey-only account that is the last factor, password-only
        login is re-enabled, and the response reports a key kept that
        nobody can use. Refuse the rotation instead: nothing is removed,
        and re-confirming with a key that still exists works."""
        proving = self._passkey(used=True)
        other = self._passkey(used=True)
        self._signed_in()
        self._elevate_with_passkey(proving)
        self._delete_key_behind_the_stamp(proving)
        r = self.client.post("/api/password/change",
                             json={"current_password": PW,
                                   "new_password": NEW})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("elevation_required", r.text)
        self.assertEqual(self._passkey_ids(), {other})
        # the password did not rotate either — the refusal is total
        admin = tenancy.admin_connect()
        try:
            h = admin.execute("SELECT password_hash FROM users WHERE id=%s",
                              (self.uid,)).fetchone()["password_hash"]
        finally:
            admin.close()
        self.assertTrue(passwords.verify_password(h, PW))
        # and the account still refuses password-only login, which is the
        # posture the eviction exists to preserve
        from oikonome.web import security
        security._limiter._hits.clear()
        login = TestClient(self.appmod.app).post(
            "/api/login", data={"email": self.email, "password": PW})
        self.assertEqual(login.status_code, 401, login.text)
        self.assertIn("passkey_required", login.text)
        # re-confirming with the key that IS there rotates normally
        self._elevate_with_passkey(other)
        ok = self.client.post("/api/password/change",
                              json={"current_password": PW,
                                    "new_password": NEW})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual(self._passkey_ids(), {other})

    def test_deleting_a_passkey_closes_the_window_it_proved(self):
        """The stamp must not outlive the credential.

        Elevate with A, delete A on the Security page (allowed while B
        remains), and the window is still open with A's name on it. The
        rotation that follows would spare A and delete B. Deleting the key
        closes the window it opened, so the next posture change asks for a
        proof the account can still give."""
        proving = self._passkey(used=True)
        other = self._passkey(used=True)
        self._signed_in()
        self._elevate_with_passkey(proving)
        gone = self.client.delete(f"/api/passkeys/{proving}")
        self.assertEqual(gone.status_code, 200, gone.text)
        state = self.client.get("/api/auth/elevation")
        self.assertFalse(state.json()["elevated"], state.text)
        r = self.client.post("/api/password/change",
                             json={"current_password": PW,
                                   "new_password": NEW})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("elevation_required", r.text)
        self.assertEqual(self._passkey_ids(), {other})

    # ---- email change (the twin door) -----------------------------------

    def test_email_change_keeps_only_the_key_that_proved_it(self):
        proving = self._passkey(used=True)
        strangers = self._passkey(used=True)
        self._signed_in()
        self._elevate_with_passkey(proving)
        r = self.client.post(
            "/api/email/change",
            data={"new_email": f"moved-{uuid.uuid4().hex[:8]}@x.dev"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["passkeys_removed"], 1)
        self.assertEqual(r.json()["passkeys_kept"], 1)
        self.assertEqual(self._passkey_ids(), {proving})
        self.assertNotIn(strangers, self._passkey_ids())

    def test_email_change_leaves_password_only_login_refused(self):
        proving = self._passkey(used=True)
        self._signed_in()
        self._elevate_with_passkey(proving)
        moved = f"moved-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self.client.post(
            "/api/email/change", data={"new_email": moved}).status_code, 200)
        from oikonome.web import security
        security._limiter._hits.clear()
        r = TestClient(self.appmod.app).post(
            "/api/login", data={"email": moved, "password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("passkey_required", r.text)

    def test_email_change_refuses_when_the_key_its_proof_names_is_gone(self):
        """The twin door takes the same proof and evicts the same way, so
        it refuses the same dangling name — and the address does not move
        either."""
        proving = self._passkey(used=True)
        other = self._passkey(used=True)
        self._signed_in()
        self._elevate_with_passkey(proving)
        self._delete_key_behind_the_stamp(proving)
        r = self.client.post(
            "/api/email/change",
            data={"new_email": f"moved-{uuid.uuid4().hex[:8]}@x.dev"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("elevation_required", r.text)
        self.assertEqual(self._passkey_ids(), {other})
        admin = tenancy.admin_connect()
        try:
            self.assertEqual(admin.execute(
                "SELECT email FROM users WHERE id=%s",
                (self.uid,)).fetchone()["email"], self.email)
        finally:
            admin.close()

    def _other_session_count(self, mine: str) -> int:
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT count(*) AS n FROM sessions WHERE user_id=%s "
                "AND token_hash != %s",
                (self.uid, sessions._h(mine))).fetchone()["n"]
        finally:
            admin.close()

    def test_a_late_refusal_leaves_the_address_and_the_sessions_alone(self):
        """The whole rotation happens or none of it does.

        The eviction's own check runs under the passkey lock, so it can
        still refuse after the pre-flight one passed — the key was there a
        moment ago and is gone now. With the address moving on one
        connection and the revocations running on another, that refusal
        would answer 403 to a caller whose login email had in fact changed
        and whose other sessions were already gone: a door that says it did
        nothing while having done half. The rotation is one transaction, so
        the refusal takes the address back with it.
        """
        proving = self._passkey(used=True)
        self._passkey(used=True)
        self._signed_in()
        self._elevate_with_passkey(proving)
        mine = self.client.cookies.get(sessions.COOKIE_NAME)
        admin = tenancy.admin_connect()
        try:
            sessions.create_session(admin, self.uid, self.tid, "other-ua")
            admin.commit()
        finally:
            admin.close()
        self.assertEqual(self._other_session_count(mine), 1)
        real = self.appmod._proved_passkey_or_refuse

        def vanishes(user_id, keep_passkey_id):
            # exactly the race the eviction's second check exists for: the
            # key is there for the pre-flight and deleted before the lock
            kept = real(user_id, keep_passkey_id)
            self._delete_key_behind_the_stamp(proving)
            return kept

        with mock.patch.object(self.appmod, "_proved_passkey_or_refuse",
                               vanishes):
            r = self.client.post(
                "/api/email/change",
                data={"new_email": f"moved-{uuid.uuid4().hex[:8]}@x.dev"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("elevation_required", r.text)
        admin = tenancy.admin_connect()
        try:
            self.assertEqual(
                admin.execute("SELECT email FROM users WHERE id=%s",
                              (self.uid,)).fetchone()["email"], self.email,
                "the door answered 403 — the address must not have moved")
        finally:
            admin.close()
        self.assertEqual(
            self._other_session_count(mine), 1,
            "a rotation reported as refused must not have evicted anything")


if __name__ == "__main__":
    unittest.main()
