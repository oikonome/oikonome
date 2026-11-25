"""The second-factor gate keeps the ways out of the account on screen.

The server lets a session with no enrolled factor erase its household (the
owner) or remove its own login (anyone else), because enrolment is not owed
by someone who wants to leave. The gate that stands in front of that
session must therefore offer the same two doors, each to the role the
server serves it to and to nobody else: Delete account for the owner (the
erasure door refuses anyone else), Leave household for a member or viewer
(the leave door refuses the owner, whose departure would orphan the
household). A role that is not known yet shows neither, rather than a
button that is refused when pressed.

Source-shaped, like the other client-shape tests: what is protected is the
question each control asks in the markup.
"""

import pathlib
import re
import unittest

import oikonome

ROOT = pathlib.Path(oikonome.__file__).parent.parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text()


class WebAccountExitTests(unittest.TestCase):

    def setUp(self):
        try:
            self.exits = _read("webapp/src/components/AccountExits.tsx")
            self.gate = _read("webapp/src/components/TwoFactorGate.tsx")
        except FileNotFoundError as e:
            self.fail(f"missing client file: {e}")

    def test_delete_is_offered_to_the_owner_alone(self):
        self.assertIn('const owner = role === "owner";', self.exits)
        self.assertIn('const other = !!role && role !== "owner";',
                      self.exits)
        self.assertRegex(self.exits,
                         r"\{owner \? <DeleteAccountCard owner")
        # the card refuses a non-owner on its own too
        self.assertIn("if (!owner) return null;", self.exits)

    def test_leave_is_offered_to_a_known_non_owner_alone(self):
        self.assertRegex(self.exits, r": <LeaveHouseholdCard")
        self.assertIn("if (!owner && !other) return null;", self.exits)
        self.assertIn('{owner ? "Delete account" : "Leave household"}',
                      self.exits)

    def test_the_cards_keep_the_settings_confirmations(self):
        self.assertIn('confirm !== "delete everything"', self.exits)
        self.assertIn('confirm !== "delete my login"', self.exits)
        self.assertIn("api.accountDelete()", self.exits)
        self.assertIn("api.accountLeave()", self.exits)

    def test_the_gate_offers_the_exit_for_its_own_role(self):
        self.assertIn('import { AccountExit } from "./AccountExits";',
                      self.gate)
        self.assertRegex(self.gate, r"<AccountExit role=\{me\.data\?\.role\}")


class MobileAccountExitTests(unittest.TestCase):

    def setUp(self):
        try:
            self.exits = _read("mobile/src/components/account-exits.tsx")
            self.layout = _read("mobile/src/app/(tabs)/_layout.tsx")
            self.security = _read("mobile/src/app/security.tsx")
        except FileNotFoundError as e:
            self.fail(f"missing client file: {e}")

    def test_the_app_asks_the_same_two_questions(self):
        self.assertIn('const owner = role === "owner";', self.exits)
        self.assertIn('const other = !!role && role !== "owner";',
                      self.exits)
        self.assertIn("if (!owner && !other) return null;", self.exits)
        self.assertRegex(self.exits, r"\(owner \? <DeleteCard onDeleted")
        self.assertRegex(self.exits, r": <LeaveCard")
        self.assertIn('{owner ? "Delete account" : "Leave household"}',
                      self.exits)

    def test_the_lockout_offers_the_exit_for_its_own_role(self):
        i = self.layout.index("if (me.data?.needs_2fa) {")
        block = self.layout[i:self.layout.index("for (const l of ext.lockouts)", i)]
        self.assertRegex(block, r"<AccountExit role=\{me\.data\?\.role\}")
        self.assertIn("onDone={signOut}", block)

    def test_security_uses_the_one_copy_of_the_cards(self):
        self.assertNotIn("function DeleteCard(", self.security)
        self.assertNotIn("function LeaveCard(", self.security)
        self.assertTrue(re.search(r"import \{[^}]*DeleteCard[^}]*\} from "
                                  r"\"\.\./components/account-exits\"",
                                  self.security))


if __name__ == "__main__":
    unittest.main()
