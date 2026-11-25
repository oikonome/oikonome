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
from oikonome.web.schemas.business import BalanceSheet, Capital, EstimatedTax

from .generated_types import aliases, ts_field

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = (_APP / "webapp/src/pages/Business.tsx").read_text()
_MOB = (_APP / "mobile/src/app/business.tsx").read_text()


class HouseholdMemberScreens(unittest.TestCase):

    def test_the_set_aside_uses_the_households_own_footing(self):
        # the card's figures are selfemploy.tax_set_aside's, which reads
        # the estimate's covered figures; both clients print set_aside
        import inspect

        from oikonome.engine import selfemploy
        src = inspect.getsource(selfemploy.tax_set_aside)
        self.assertIn("covered_profit", src)
        self.assertIn("covered_tax_paid", src)
        for name, src in (("web", _WEB), ("mobile", _MOB)):
            with self.subTest(name):
                self.assertIn("set_aside", src)
        # the server documents both figures and the reason (so the hook
        # proves each reply carries them), and both clients compile against
        # the generated EstimatedTax that holds them
        for f in ("covered_profit", "covered_tax_paid",
                  "household_unavailable"):
            self.assertIn(f, EstimatedTax.model_fields)
        for name in ("web", "mobile"):
            with self.subTest(name):
                self.assertTrue(aliases(name, "EstimatedTax", "EstimatedTax"))
                self.assertEqual(ts_field(name, "EstimatedTax",
                                          "covered_profit"), "number")
                self.assertIn("s_corp", ts_field(name, "EstimatedTax",
                                                 "household_unavailable"))

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
        # the capital account and the balance sheet both carry the share
        for model in (Capital, BalanceSheet):
            self.assertIn("household_capital", model.model_fields)
        for name in ("web", "mobile"):
            with self.subTest(name):
                for shape in ("Capital", "BalanceSheet"):
                    self.assertEqual(ts_field(name, shape, "household_capital"),
                                     "number | null")
                self.assertTrue(aliases(name, "Capital", "Capital"))
        self.assertTrue(aliases("web", "BalanceSheet", "BalanceSheet"))

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
