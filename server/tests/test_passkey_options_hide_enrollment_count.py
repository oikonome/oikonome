"""Passkey login options must not tell an attacker who has an account.

`POST /api/login/passkey/options` takes a candidate email from anyone, so
its response is an oracle unless every email produces the same-shaped
answer. An address with no account (or no passkey) is answered with a
synthetic decoy credential for exactly that reason — but the shape has to
match for EVERY enrollment count, not just zero, or the array length
itself becomes the oracle: an account with two keys could be picked out
of a candidate list because the decoy branch could only ever produce one.
"""

import unittest
import uuid

from oikonome.auth import passkeys
from oikonome.db import tenancy

from .util import _ensure_db

RP_ID = "localhost"


class LoginOptionsShapeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect()

    def tearDown(self):
        self.admin.close()

    def _account(self, passkey_count: int) -> str:
        """An account with exactly `passkey_count` enrolled credentials."""
        email = f"enum-{uuid.uuid4().hex[:10]}@example.dev"
        tid = tenancy.create_tenant(self.admin, email)
        uid = self.admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s,%s,'x') RETURNING id", (tid, email)).fetchone()["id"]
        for i in range(passkey_count):
            self.admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s,%s,%s,0)",
                (uid, passkeys._b64u(uuid.uuid4().bytes + bytes([i])),
                 passkeys._b64u(b"pub" + bytes([i]))))
        return email

    def _allow_len(self, email: str) -> int:
        opts = passkeys.login_options(self.admin, RP_ID, email)
        return len(opts["options"].get("allowCredentials") or [])

    def test_enrollment_count_is_not_visible_in_the_options(self):
        """The response for a stranger, for a one-key account and for a
        multi-key account must be indistinguishable by shape."""
        stranger = f"nobody-{uuid.uuid4().hex[:10]}@example.dev"
        lengths = {
            "stranger": self._allow_len(stranger),
            "no passkey": self._allow_len(self._account(0)),
            "one passkey": self._allow_len(self._account(1)),
            "two passkeys": self._allow_len(self._account(2)),
            "three passkeys": self._allow_len(self._account(3)),
        }
        self.assertEqual(
            1, len(set(lengths.values())),
            "allowCredentials length reveals how many passkeys an address "
            f"has: {lengths}")

    def test_every_real_credential_is_still_offered(self):
        """Padding must not cost a user a key: an authenticator can only
        answer with a credential the server actually listed."""
        email = self._account(3)
        real = {r["credential_id"] for r in self.admin.execute(
            "SELECT p.credential_id FROM passkeys p JOIN users u "
            "ON u.id = p.user_id WHERE u.email = %s", (email,)).fetchall()}
        offered = {c["id"] for c in
                   passkeys.login_options(self.admin, RP_ID,
                                          email)["options"]["allowCredentials"]}
        self.assertTrue(real <= offered,
                        "a registered credential was dropped from the "
                        "allow-list — that key can no longer sign in")

    def test_the_answer_for_one_address_does_not_change_between_calls(self):
        """Two probes of the same address must not differ, or diffing the
        responses re-opens the oracle the decoys close."""
        email = f"nobody-{uuid.uuid4().hex[:10]}@example.dev"
        first = [c["id"] for c in passkeys.login_options(
            self.admin, RP_ID, email)["options"]["allowCredentials"]]
        second = [c["id"] for c in passkeys.login_options(
            self.admin, RP_ID, email)["options"]["allowCredentials"]]
        self.assertEqual(first, second)

    def test_decoys_differ_between_addresses(self):
        """A decoy shared by every stranger would be its own tell."""
        a = [c["id"] for c in passkeys.login_options(
            self.admin, RP_ID,
            f"a-{uuid.uuid4().hex[:10]}@example.dev")["options"]
            ["allowCredentials"]]
        b = [c["id"] for c in passkeys.login_options(
            self.admin, RP_ID,
            f"b-{uuid.uuid4().hex[:10]}@example.dev")["options"]
            ["allowCredentials"]]
        self.assertEqual(set(), set(a) & set(b))


if __name__ == "__main__":
    unittest.main()
