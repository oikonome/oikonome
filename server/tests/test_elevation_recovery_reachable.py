"""A lost authenticator must not strand anybody in either client.

Elevation guards the doors that end an account or take its data out: the
full export and the account delete. The elevate endpoint has always taken
a recovery code beside the password on a TOTP account — the same stand-in
the login screen offers when the authenticator is gone — but for a long
while neither client could send that shape. The recovery form was
reachable ONLY from the passkey path, so an account with a password and
TOTP rendered a password field, a six-digit field capped at six
characters, and no way out: a 24-character recovery code could not even
be typed into it, let alone sent.

Source-shaped, like the other client-shape tests here, because what is
being protected is a decision in the markup — which proofs a form can
collect and where the person can get to it from — not a behaviour a
request can probe. The server half of the same contract is pinned by
test_elevation.py; this file pins that the clients can reach it.
"""

import pathlib
import re
import unittest

import oikonome

ROOT = pathlib.Path(oikonome.__file__).parent.parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text()


class WebElevationSheetTests(unittest.TestCase):

    def setUp(self):
        try:
            self.src = _read("webapp/src/elevation.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def _recovery_branch(self) -> str:
        """The recovery form's own markup, comments stripped, so a cap or a
        shape found here belongs to the recovery field and not to the
        authenticator one — or to prose about it."""
        start = self.src.index('alt === "recovery" ? (')
        branch = self.src[start:self.src.index("\n        ) : (", start)]
        return re.sub(r"\{/\*.*?\*/\}", "", branch, flags=re.S)

    def test_the_recovery_form_does_not_belong_to_the_passkey_path(self):
        self.assertNotIn('passkey && alt === "recovery"', self.src)
        self.assertIn('alt === "recovery" ? (', self.src)

    def test_a_password_account_can_reach_it_when_its_authenticator_is_lost(
            self):
        """The password form is the branch a password+TOTP account always
        lands on. It must name the way past a missing authenticator."""
        self.assertIn("Lost your authenticator?", self.src)
        self.assertIn('setCode(""); setAlt("recovery")', self.src)

    def test_the_recovery_form_sends_the_shape_the_server_documents(self):
        """A recovery code stands in for ONE factor: an account that has a
        password sends both. A bare code there is refused
        (password_required), so a form that could only send one was a door
        that could never open."""
        branch = self._recovery_branch()
        self.assertIn("submitRecovery", branch)
        self.assertIn("password: pw, recovery_code:", self.src)
        self.assertIn("recovery_code: recovery.trim()", self.src)

    def test_the_recovery_field_can_hold_a_recovery_code(self):
        """Recovery codes are five dash-separated groups — 24 characters.
        The authenticator field's maxLength of six made one untypeable."""
        self.assertNotIn("maxLength", self._recovery_branch())

    def test_a_passkey_account_with_a_password_can_still_choose_recovery(self):
        """Having a password must not remove the recovery route: somebody
        who lost the key AND the authenticator has nothing else left."""
        # the two ways out are independent conditions, not an either/or:
        # the password link on holding a password, the recovery link on
        # having no password OR having an authenticator to lose
        self.assertIn("{hasPassword && (", self.src)
        self.assertIn("(!hasPassword || info.totp)", self.src)


class MobileElevationSheetTests(unittest.TestCase):

    def setUp(self):
        try:
            self.modal = _read("mobile/src/components/elevation-modal.tsx")
            self.pure = _read("mobile/src/lib/pure.ts")
        except FileNotFoundError:
            self.skipTest("mobile/ not present")

    def test_the_password_form_offers_the_recovery_route(self):
        """setForm("recovery") used to be reachable only from the passkey
        path's failure, and the only link between the two forms pointed
        the other way."""
        self.assertIn('form === "password" && status?.totp', self.modal)
        self.assertIn('setForm("recovery")', self.modal)

    def test_the_recovery_form_collects_the_password_it_has_to_send(self):
        self.assertIn("passwordToo && (", self.modal)
        self.assertIn("secureTextEntry", self.modal)

    def test_the_proof_shape_is_one_decision_the_screen_defers_to(self):
        """Button state and submitted body come from the same call, so a
        form cannot be enabled for a proof it will not send."""
        self.assertIn("elevationProof", self.modal)
        self.assertIn("password: pw, recovery_code: code", self.pure)


if __name__ == "__main__":
    unittest.main()
