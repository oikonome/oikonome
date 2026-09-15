"""Merchants: one list, a panel beside it, two actions with different names.

The page had grown by accretion: three cards stacked above the list, the
proposer's evidence and raw bank strings in the open, every source name
printed inline, and one control that renamed OR merged depending on what
was typed — a case or whitespace slip forked a merchant instead of merging
it. Now a row is a logo, a name and one line of facts; the rest opens
beside the list; Merge picks an EXISTING merchant from a search; the
offers are a queue with the evidence behind a click. Both clients.
"""

import pathlib
import unittest

import oikonome

_ROOT = pathlib.Path(oikonome.__file__).parent.parent.parent


def _src(*rel: str) -> str:
    return _ROOT.joinpath(*rel).read_text()


class WebMerchantsShape(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("webapp", "src", "pages", "Merchants.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_offers_are_a_queue_with_evidence_behind_a_click(self):
        self.assertIn("Needs a look", self.src)
        self.assertIn('"why?"', self.src)
        self.assertIn("Merge all", self.src)

    def test_merge_picks_an_existing_merchant_and_rename_is_its_own_action(self):
        self.assertIn("Merge into…", self.src)
        self.assertIn(">Rename<", self.src)
        # the picker searches the catalog, so the target always exists
        self.assertIn('api.merchantCatalog(pickQ, "count")', self.src)
        self.assertNotIn("<datalist", self.src)

    def test_the_list_has_the_four_orders_and_the_three_filters(self):
        for word in ("biggest spend", "recently seen", "one-off", "unnamed", "seen this month"):
            self.assertIn(word, self.src)

    def test_a_row_opens_a_panel_beside_the_list(self):
        self.assertIn('className="side-panel"', self.src)
        self.assertIn("Recent changes", self.src)
        self.assertIn('<details className="card fold"', self.src)


class MobileMerchantsShape(unittest.TestCase):
    def setUp(self):
        try:
            self.list = _src("mobile", "src", "app", "merchants.tsx")
            self.screen = _src("mobile", "src", "app", "merchant.tsx")
            self.pure = _src("mobile", "src", "lib", "pure.ts")
            self.layout = _src("mobile", "src", "app", "_layout.tsx")
        except FileNotFoundError:
            self.skipTest("mobile/ not present")

    def test_the_list_mirrors_the_web(self):
        for word in ("Needs a look", "why?", "Merge all", "Recent changes",
                     "MERCHANT_ORDERS", "MERCHANT_FILTERS", "sortMerchants"):
            self.assertIn(word, self.list)
        # a row opens the merchant's own screen, which the stack knows
        self.assertIn('pathname: "/merchant"', self.list)
        self.assertIn('name="merchant"', self.layout)

    def test_the_screen_has_the_two_actions(self):
        self.assertIn("Merge into…", self.screen)
        self.assertIn("Rename", self.screen)
        self.assertIn('merchantCatalog(pickQ, "count")', self.screen)

    def test_the_orders_and_filters_are_the_webs(self):
        web = _src("webapp", "src", "pages", "Merchants.tsx")
        for word in ("biggest spend", "recently seen", "one-off", "unnamed", "seen this month"):
            self.assertIn(word, self.pure)
            self.assertIn(word, web)


if __name__ == "__main__":
    unittest.main()
