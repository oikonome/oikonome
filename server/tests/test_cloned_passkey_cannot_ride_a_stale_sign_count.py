"""A second assertion judged against a counter that has already moved is
refused.

The WebAuthn signature counter is the only signal an authenticator gives
that a copy of it exists: a real one's counter climbs with every use, so
an assertion that does not beat the last count we recorded came from a
clone. Verifying that is a read, a verify and a write on an autocommit
connection, so if the write is unconditional, two assertions for one
credential that overlap are both measured against the same stale baseline
and both get in — precisely the pair the counter exists to split. The
write is conditional on the baseline instead, so whichever lands second
matches no row and is refused.

Authenticators that keep no counter at all (most platform passkeys,
forever reporting 0) must be unaffected: they have no clone signal to
give, and their sign-ins have to keep working however they overlap.
"""

import os
import unittest
import uuid
from unittest import mock

from oikonome.auth import passkeys
from oikonome.db import tenancy

from .util import _ensure_db

# A syntactically real stored public key: login_verify decodes it
# before the verifier is ever called.
PUBLIC_KEY = "BQYHCA"
RP_ID = "localhost"
ORIGIN = "http://localhost"


class _Asserted:
    """What the WebAuthn verifier hands back — the counter the
    authenticator signed."""

    def __init__(self, new_sign_count):
        self.new_sign_count = new_sign_count


class ClonedPasskeySignCount(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()

    def setUp(self):
        self.email = f"pkc-{uuid.uuid4().hex[:8]}@example.dev"
        # stored the way enrollment stores it: base64url, so the
        # options calls can decode it back into a descriptor
        self.cred_id = passkeys._b64u(uuid.uuid4().bytes)
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, self.email)
            from oikonome.auth import passwords
            self.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (tid, self.email,
                 passwords.hash_password("correct-horse-battery"))
            ).fetchone()["id"]
        finally:
            admin.close()

    def _enroll(self, sign_count: int) -> None:
        admin = tenancy.admin_connect()
        try:
            self.pk_id = admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, %s, %s) RETURNING id",
                (self.uid, self.cred_id, PUBLIC_KEY,
                 sign_count)).fetchone()["id"]
        finally:
            admin.close()

    def _stored_count(self) -> int:
        admin = tenancy.admin_connect()
        try:
            return admin.execute("SELECT sign_count FROM passkeys "
                                 "WHERE id = %s",
                                 (self.pk_id,)).fetchone()["sign_count"]
        finally:
            admin.close()

    def _overlapping(self, lands_first: int, signed: int):
        """Stand in for the verifier, and make the OTHER assertion land
        while this one is being checked — the window the race lives in."""
        def verify(**_kw):
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE passkeys SET sign_count = %s "
                              "WHERE id = %s", (lands_first, self.pk_id))
            finally:
                admin.close()
            return _Asserted(signed)
        return mock.patch.object(passkeys, "verify_authentication_response",
                                 side_effect=verify)

    def _credential(self) -> dict:
        return {"id": self.cred_id, "rawId": self.cred_id}

    def test_a_clone_racing_the_owner_is_refused_at_sign_in(self):
        """The owner's assertion carries the counter to 9 while the
        clone's — signed at 6, off the same stale reading of 5 — is being
        verified. The clone must not sign in, and must not drag the stored
        counter backwards on its way out."""
        self._enroll(5)
        conn = tenancy.control_connect()
        try:
            challenge_id = passkeys.login_options(conn, RP_ID)["challenge_id"]
            with self._overlapping(lands_first=9, signed=6):
                with self.assertRaisesRegex(ValueError, "mid sign-in"):
                    passkeys.login_verify(conn, challenge_id,
                                          self._credential(), RP_ID, ORIGIN)
        finally:
            conn.close()
        self.assertEqual(self._stored_count(), 9)

    def test_a_clone_racing_the_owner_is_refused_at_step_up(self):
        """Step-up re-asserts the same credential for a destructive
        change, so it needs the same answer — and must mint no ticket."""
        self._enroll(5)
        conn = tenancy.control_connect()
        try:
            challenge_id = passkeys.stepup_options(
                conn, self.uid, RP_ID)["challenge_id"]
            with self._overlapping(lands_first=9, signed=6):
                with self.assertRaisesRegex(ValueError, "mid sign-in"):
                    passkeys.stepup_verify(conn, self.uid, challenge_id,
                                           self._credential(), RP_ID, ORIGIN)
            tickets = conn.execute(
                "SELECT count(*) AS n FROM webauthn_challenges "
                "WHERE purpose = 'stepup-ticket' AND user_id = %s",
                (self.uid,)).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(tickets, 0)
        self.assertEqual(self._stored_count(), 9)

    def test_an_authenticator_that_keeps_no_counter_still_signs_in(self):
        """A platform passkey reports 0 for ever, so two of its sign-ins
        overlapping leave the row at 0 either way. There is no clone
        signal to read there, and refusing on that reading would lock out
        the commonest authenticator there is."""
        self._enroll(0)
        conn = tenancy.control_connect()
        try:
            challenge_id = passkeys.login_options(conn, RP_ID)["challenge_id"]
            with self._overlapping(lands_first=0, signed=0):
                r = passkeys.login_verify(conn, challenge_id,
                                          self._credential(), RP_ID, ORIGIN)
        finally:
            conn.close()
        self.assertEqual(r["user_id"], self.uid)
        self.assertEqual(self._stored_count(), 0)

    def test_an_uncontested_assertion_advances_the_counter(self):
        """The ordinary sign-in: nothing else touches the row, so the new
        count is stored and the next assertion is measured against it."""
        self._enroll(5)
        conn = tenancy.control_connect()
        try:
            challenge_id = passkeys.login_options(conn, RP_ID)["challenge_id"]
            with mock.patch.object(passkeys, "verify_authentication_response",
                                   return_value=_Asserted(6)):
                r = passkeys.login_verify(conn, challenge_id,
                                          self._credential(), RP_ID, ORIGIN)
        finally:
            conn.close()
        self.assertEqual(r["user_id"], self.uid)
        self.assertEqual(self._stored_count(), 6)


if __name__ == "__main__":
    unittest.main()
