"""Decoy credential lengths must carry no information about the account.

`POST /api/login/passkey/options` pads every allow-list to a fixed width
with decoys, but a decoy's BYTE LENGTH is a channel of its own: credential
ids vary by authenticator vendor (~16 to 90+ bytes), so decoys sized off
the account's first real credential made any different-length real key the
unique outlier in a mixed-authenticator account, and gave credential-less
addresses a uniform four-of-a-kind that no multi-vendor account would
produce. Decoy lengths must be a function of the email seed alone —
identical whatever is (or isn't) enrolled — so real credentials hide among
plausibly varied lengths instead of defining them.
"""

import unittest
import uuid

from oikonome.auth import passkeys
from oikonome.db import tenancy

from .util import _ensure_db

RP_ID = "localhost"


class DecoyLengthIndependenceTests(unittest.TestCase):
    """Pure-function checks on the padding itself — no database."""

    def _decoys_of(self, padded, real):
        return [c for c in padded if c not in real]

    def test_decoy_bytes_do_not_depend_on_the_real_credentials(self):
        """The same email must yield byte-identical decoys whether its one
        real credential is a compact platform id or a wide security-key
        handle — otherwise the decoys mirror the reals and any real key
        whose length differs from the first stands out."""
        email = "same-address@example.dev"
        short_real = [b"\x01" * 16]
        wide_real = [b"\x02" * 90]
        decoys_short = self._decoys_of(
            passkeys._pad_allow(email, short_real), short_real)
        decoys_wide = self._decoys_of(
            passkeys._pad_allow(email, wide_real), wide_real)
        self.assertEqual(decoys_short, decoys_wide,
                         "decoys changed with the real credential — their "
                         "lengths are leaking what the account has enrolled")

    def test_decoy_lengths_vary_within_and_across_addresses(self):
        """A credential-less address must show the same plausibly mixed
        length family as a multi-vendor account — uniform-length decoy
        sets were themselves a tell that nothing real was enrolled."""
        all_lengths = set()
        some_address_is_mixed = False
        for i in range(30):
            padded = passkeys._pad_allow(f"empty-{i}@example.dev", [])
            lengths = [len(c) for c in padded]
            self.assertEqual(passkeys._ALLOW_WIDTH, len(padded))
            for n in lengths:
                self.assertIn(n, passkeys._DECOY_SIZES)
            all_lengths.update(lengths)
            if len(set(lengths)) > 1:
                some_address_is_mixed = True
        self.assertGreater(len(all_lengths), 1,
                           "every decoy everywhere has one length — the "
                           "distribution can't hide mixed-vendor accounts")
        self.assertTrue(some_address_is_mixed,
                        "no address shows mixed decoy lengths — a "
                        "heterogeneous real account is still an outlier")

    def test_padding_is_deterministic_and_keeps_every_real_credential(self):
        """Repeat queries must be byte-identical (diffing two probes must
        not re-open the oracle) and padding must never drop a real key."""
        email = "repeat@example.dev"
        real = [b"\x03" * 20, b"\x04" * 64]
        first = passkeys._pad_allow(email, real)
        second = passkeys._pad_allow(email, real)
        self.assertEqual(first, second)
        for cred in real:
            self.assertIn(cred, first)


class LoginOptionsLengthShapeTests(unittest.TestCase):
    """The same properties observed through the real endpoint path."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect()

    def tearDown(self):
        self.admin.close()

    def test_nonexistent_email_gets_varied_length_deterministic_decoys(self):
        """A stranger's response must look like any account's: full width,
        lengths drawn from the same family, and identical on re-query."""
        seen_lengths = set()
        for i in range(10):
            email = f"ghost-{uuid.uuid4().hex[:10]}@example.dev"
            first = [c["id"] for c in passkeys.login_options(
                self.admin, RP_ID, email)["options"]["allowCredentials"]]
            second = [c["id"] for c in passkeys.login_options(
                self.admin, RP_ID, email)["options"]["allowCredentials"]]
            self.assertEqual(first, second)
            self.assertEqual(passkeys._ALLOW_WIDTH, len(first))
            seen_lengths.update(
                len(passkeys.base64url_to_bytes(c)) for c in first)
        self.assertLessEqual(seen_lengths, set(passkeys._DECOY_SIZES))
        self.assertGreater(len(seen_lengths), 1,
                           "stranger responses are uniform-length — "
                           "distinguishable from mixed-vendor accounts")


if __name__ == "__main__":
    unittest.main()
