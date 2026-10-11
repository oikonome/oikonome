"""Both clients show the household member's share on its own footing.

Shape checks over the web and mobile sources, because the screens have
no runtime harness here:

* the set-aside card divides the household's estimate by the profit and
  payments the server gives on the same footing (covered_profit /
  covered_tax_paid), never the whole business's — a 50% member would see
  half their real rate and every partner's payments off their bill;
* the "mark your member" hint follows the server's reason
  (household_unavailable), so it never asks for a mark that changes
  nothing (an S-corp, shares that do not make 100%);
* the household's share of the capital account is rendered where the
  capital account is, as the guide promises;
* adding a member on the phone resets the household switch, or the next
  member added quietly takes the mark.
"""

import pathlib
import re
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = (_APP / "webapp/src/pages/Business.tsx").read_text()
_WEB_T = (_APP / "webapp/src/api/client.ts").read_text()
_MOB = (_APP / "mobile/src/app/business.tsx").read_text()
_MOB_T = (_APP / "mobile/src/lib/api.ts").read_text()


class HouseholdMemberScreens(unittest.TestCase):

    def test_the_set_aside_uses_the_households_own_footing(self):
        for name, src in (("web", _WEB), ("mobile", _MOB)):
            with self.subTest(name):
                self.assertIn("covered_profit", src)
                self.assertIn("covered_tax_paid", src)
        for name, src in (("web", _WEB_T), ("mobile", _MOB_T)):
            with self.subTest(name):
                self.assertIn("covered_profit?: number", src)
                self.assertIn("household_unavailable?:", src)

    def test_the_hint_follows_the_servers_reason(self):
        for name, src in (("web", _WEB), ("mobile", _MOB)):
            with self.subTest(name):
                self.assertIn("bizHouseholdHint(e", src)
                self.assertIn('why === "s_corp"', src)
                self.assertIn('why === "shares_incomplete"', src)

    def test_the_households_capital_share_is_shown(self):
        for name, src in (("web", _WEB), ("mobile", _MOB)):
            with self.subTest(name):
                self.assertIn("cap.household_capital", src)
                self.assertRegex(src, r"(b|bs\.data)\.household_capital")
        for name, src in (("web", _WEB_T), ("mobile", _MOB_T)):
            with self.subTest(name):
                self.assertGreaterEqual(
                    src.count("household_capital?: number | null"), 2)

    def test_adding_a_member_on_the_phone_resets_the_household_switch(self):
        i = _MOB.index("const addMember = useMutation(")
        body = _MOB[i:_MOB.index("onError:", i)]
        success = body[body.index("onSuccess:"):]
        self.assertIn("setMemberHousehold(false)", success)
        # and the estimate the mark moves is refetched
        self.assertTrue(re.search(r"bizMembersChanged\(qc, entity\.id\)",
                                  success))


if __name__ == "__main__":
    unittest.main()
