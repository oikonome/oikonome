"""Business Overview: every money aggregate excludes shadow-linked rows.

An account linked through two aggregators leaves two rows sharing one link
group, and both can carry entity_id, so summing every matching row counts one
real balance twice. The "Cash in the business" tile must apply the same
shadow-exclusion rule as TaxSetAside in the same file; otherwise one checking
account linked via two aggregators reads as double its balance.
"""

import pathlib
import re
import unittest

import oikonome


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


class BusinessShapeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Business.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_cash_tile_excludes_shadow_linked_rows(self):
        # The Overview cash sum must carry the same shadow filter TaxSetAside
        # uses: only the primary row of a link group contributes its balance.
        # The rule lives in one place (bizmath.countsInTotals, shared with
        # the phone) and the tile's account list goes through it.
        rule = re.search(r"export const countsInTotals =.*?\n\n", _src("bizmath.ts"), re.S)
        self.assertIsNotNone(rule, "countsInTotals not found in bizmath.ts")
        self.assertIn("!a.link || a.link.primary", rule.group(0))
        counted = re.search(r"const counted = \(accts.*?;", self.src, re.S)
        self.assertIsNotNone(counted, "Overview cash account list not found")
        self.assertIn("countsInTotals", counted.group(0))

    def test_account_count_excludes_shadow_linked_rows(self):
        # The tile's "N account(s)" sub-label counts real accounts, not rows —
        # a doubly-linked account is still one account.
        label = re.search(r"[^\n]*account\(s\)", self.src)
        self.assertIsNotNone(label, "account(s) sub-label not found")
        self.assertIn("counted.length", label.group(0))

    def test_tax_set_aside_keeps_its_shadow_filter(self):
        # The rule the Overview tile matches must itself stay.
        mine = re.search(r"const mine =.*?countsInTotals\);", self.src, re.S)
        self.assertIsNotNone(mine, "TaxSetAside account list must go through countsInTotals")

    def test_form_dates_default_to_the_local_day(self):
        """toISOString() is the UTC day: west of Greenwich an evening trip
        or owner contribution defaulted to tomorrow — on Dec 31, into the
        next tax year."""
        self.assertNotIn("toISOString().slice(0, 10)", self.src)
        self.assertIn("date: localYmd(new Date())", self.src)



class BusinessToastShapeTests(unittest.TestCase):
    def test_toasts_go_through_the_shared_helper(self):
        """client.ts toast() owns the event contract (plain text, or the
        {text, to} link form); hand-built dispatches drift from it."""
        try:
            src = _src("pages/Business.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")
        self.assertNotIn('"oiko-toast"', src)
        self.assertNotIn("const toast =", src)
        self.assertIn("const toastErr = (e: unknown) => toast(errText(e))", src)
