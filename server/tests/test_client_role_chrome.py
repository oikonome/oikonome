"""Neither client offers a member a control the server will refuse.

The role policy is enforced on the server and RENDERED by two clients, and
the rendering is where it can quietly go wrong: `canEdit` on an owner-only
control shows a member a button that can only 403, and `isOwner` on an
editing control hides the app from the person the member role exists for.
Both are invisible until somebody in that role tries it.

Source-shaped, like the other client-shape tests here, because the thing
being protected is a decision in the markup — which question a control
asks — not a behaviour a request can probe. Kept to the controls the
product actually promises are the owner's: connections, exports and
restores, the household roster.
"""

import pathlib
import unittest

import oikonome

ROOT = pathlib.Path(oikonome.__file__).parent.parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text()


class WebRoleHelperTests(unittest.TestCase):

    def setUp(self):
        try:
            self.src = _read("webapp/src/role.ts")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_two_questions_are_actually_different(self):
        """`canEdit` and `isOwner` must not collapse into one another —
        that is exactly the state the member role came out of."""
        self.assertIn('me.data.role !== "viewer"', self.src)
        self.assertIn('me.data.role === "owner"', self.src)

    def test_both_fail_closed_while_me_is_in_flight(self):
        """Unknown is not privileged. Every helper reads through
        `me?.data ? … : false`, never an optional chain that answers TRUE
        before the server has said who anybody is."""
        self.assertNotIn('me?.data?.role !==', self.src)
        for line in self.src.splitlines():
            if line.strip().startswith("return me?.data"):
                self.assertTrue(line.rstrip().endswith(": false;"), line)


class WebOwnerChromeTests(unittest.TestCase):

    def setUp(self):
        try:
            self.settings = _read("webapp/src/pages/Settings.tsx")
            self.accounts = _read("webapp/src/pages/Accounts.tsx")
            self.manage = _read("webapp/src/components/ManageAccounts.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_export_card_is_owner_chrome(self):
        self.assertIn("{owner && (\n      <Show on={h.data}>\n"
                      "      <DataExportCard", self.settings)

    def test_the_roster_offers_only_the_roles_an_owner_may_hand_out(self):
        self.assertIn("assignable_roles", self.settings)
        self.assertIn("memberSetRole", self.settings)

    def test_the_accounts_page_asks_both_questions(self):
        """Editing an account is a member's; the connection is not."""
        self.assertIn("const mayEdit = canEdit(me);", self.accounts)
        self.assertIn("const owner = isOwner(me);", self.accounts)
        # the sync control is edit chrome…
        self.assertIn("&& mayEdit && !reaped", self.accounts)
        # …and the repair/relink control is not
        self.assertIn("const needsFix = owner &&", self.accounts)

    def test_account_management_is_owner_only(self):
        self.assertIn("const owner = isOwner(me);", self.manage)


class MobileRoleChromeTests(unittest.TestCase):

    def setUp(self):
        try:
            self.helper = _read("mobile/src/lib/viewer.ts")
            self.accounts = _read("mobile/src/app/(tabs)/accounts.tsx")
            self.security = _read("mobile/src/app/security.tsx")
        except FileNotFoundError:
            self.skipTest("mobile/ not present")

    def test_the_app_can_ask_both_questions_too(self):
        self.assertIn("export function useOwner()", self.helper)
        self.assertIn('me.data.role === "owner"', self.helper)
        self.assertIn('me.data.role === "viewer"', self.helper)

    def test_connecting_a_bank_is_owner_chrome(self):
        self.assertIn("const owner = useOwner();", self.accounts)
        # the header's Connect door is owner-only; Add account beside it
        # is for any editor (the two-button header since the ledger
        # filters rebuild)
        i = self.accounts.index('"＋ Connect account ↗"')
        gate = self.accounts.rfind("{owner && (", 0, i)
        self.assertGreater(gate, 0)
        self.assertNotIn("{!viewer && (", self.accounts[gate:i])
        self.assertLess(i, self.accounts.index("＋ Add account"))

    def test_the_roster_screen_keys_the_household_on_the_owner(self):
        self.assertIn("const owner = useOwner();", self.security)
        self.assertIn("memberSetRole", self.security)


class BusinessWizardBankDoorTests(unittest.TestCase):
    """The business wizard's account step offers a bank link, and that door
    is the account owner's on both clients.

    A member may run the wizard — creating the entity and assigning
    accounts is editing chrome — but the Plaid link endpoint refuses
    anyone else, so the button on this step is drawn only for the owner
    and a plain note takes its place. The manual "add account by hand"
    fallback beside it stays for everyone, which is what makes hiding the
    link button an honest answer rather than a dead end.
    """

    def setUp(self):
        try:
            self.web = _read("webapp/src/components/BusinessWizard.tsx")
            self.app = _read("mobile/src/app/business-wizard.tsx")
        except FileNotFoundError:
            self.skipTest("clients not present")

    def test_web_asks_the_owner_question_for_the_bank_link(self):
        self.assertIn("const owner = isOwner(me);", self.web)
        self.assertIn("{owner ? (", self.web)
        # the link button lives inside that branch, the manual form does not
        self.assertIn('onClick={() => connect.mutate()}', self.web)
        self.assertIn("account owner's", self.web)
        self.assertIn("onClick={() => addAcct.mutate()}", self.web)

    def test_mobile_asks_the_owner_question_for_the_bank_link(self):
        self.assertIn("const owner = useOwner();", self.app)
        self.assertIn("{owner ? (", self.app)
        self.assertIn("onPress={() => connect.mutate()}", self.app)
        self.assertIn("owner&apos;s", self.app)
        self.assertIn("onPress={() => addAcct.mutate()}", self.app)


class BusinessWizardNameGuardTests(unittest.TestCase):
    """The wizard never offers a Create button that can only fail.

    The name is collected on the first question and the server rejects an
    entity without one, so the Create button on the second question is
    disabled while the name is blank — and, because the flow can reopen on
    that step, there is a Back control to reach the field again. A control
    that is drawn, is pressable, and answers with a 400 is the shape this
    guards against.
    """

    def setUp(self):
        try:
            self.web = _read("webapp/src/components/BusinessWizard.tsx")
            self.app = _read("mobile/src/app/business-wizard.tsx")
        except FileNotFoundError:
            self.skipTest("clients not present")

    def test_create_is_refused_while_the_name_is_blank(self):
        self.assertIn("disabled={busy || !name.trim()}", self.web)
        self.assertIn("disabled={busy || !name.trim()}", self.app)

    def test_the_second_question_can_get_back_to_the_first(self):
        self.assertIn(">Back</button>", self.web)
        self.assertIn("Back\n", self.app)

    def test_neither_question_is_marked_before_the_entity_exists(self):
        """Marking a step done on Continue would claim the answers were
        saved while they are still only in component state — the resume
        point would then land past the field that collects them."""
        for src in (self.web, self.app):
            self.assertNotIn('go(1, "what")', src)
            self.assertIn('mark.mutate({ what: "done", when: "done" })', src)


if __name__ == "__main__":
    unittest.main()
