"""Settings → Email must list everyone who actually receives the mail.

The card's own promise is "who receives the scheduled mail". With NO
recipient list configured, `worker._recipients_raw` falls back to every
verified user on the tenant, so the card lists them all — the owner alone
would tell a three-person household "you" while all three get the balances
and the verdict every morning.
"""

import pathlib
import unittest

import oikonome


class RecipientStatusTests(unittest.TestCase):

    def setUp(self):
        self.src = (pathlib.Path(oikonome.__file__).parent / "web"
                    / "api.py").read_text()

    def test_the_no_list_fallback_is_modelled(self):
        self.assertIn("implied = ([m[\"email\"] for m in members.values()",
                      self.src)
        self.assertIn("+ implied", self.src)

    def test_a_member_row_is_not_labelled_uninvited(self):
        """They were never invited and never need to be, so an
        "uninvited" badge on their row would be a lie."""
        self.assertIn('if (is_owner or (not configured and m))', self.src)

    def test_each_row_says_why_it_is_there(self):
        self.assertIn('"via": ("owner" if is_owner', self.src)

    def test_a_configured_list_does_not_gain_phantom_members(self):
        """The fallback is only the empty-list case: once a household names
        its recipients, everyone else stops receiving and must stop being
        listed."""
        self.assertIn("if not configured else []", self.src)


class RecipientStatusSpaTests(unittest.TestCase):

    def setUp(self):
        p = (pathlib.Path(oikonome.__file__).parent.parent.parent
             / "webapp" / "src")
        try:
            self.spa = (p / "pages" / "Settings.tsx").read_text()
            self.client = (p / "api" / "client.ts").read_text()
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_reason_reaches_the_client(self):
        self.assertIn('via?: "owner" | "member" | "invited"', self.client)

    def test_a_member_row_says_what_it_is(self):
        self.assertIn('status.via === "member"', self.spa)
        self.assertIn("household member", self.spa)
