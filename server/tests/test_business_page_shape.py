"""Business Overview: every money aggregate excludes shadow-linked rows.

An account linked through two aggregators leaves two rows sharing one link
group, and both can carry entity_id, so summing every matching row counts one
real balance twice. The "Cash in the business" tile did exactly that — one
$10,000 checking account linked via two aggregators read as $20,000 — while
TaxSetAside in the same file already applied the shadow-exclusion rule.
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
        cash = re.search(r"const cash = \(accts.*?reduce\(", self.src, re.S)
        self.assertIsNotNone(cash, "Overview cash aggregation not found")
        self.assertIn("!a.link || a.link.primary", cash.group(0))

    def test_account_count_excludes_shadow_linked_rows(self):
        # The tile's "N account(s)" sub-label counts real accounts, not rows —
        # a doubly-linked account is still one account.
        label = re.search(r"[^\n]*account\(s\)", self.src)
        self.assertIsNotNone(label, "account(s) sub-label not found")
        tile = self.src[max(0, label.start() - 400):label.end()]
        self.assertIn("!a.link || a.link.primary", tile)

    def test_tax_set_aside_keeps_its_shadow_filter(self):
        # The rule the Overview tile was fixed to match must itself stay.
        mine = re.search(r"const mine =.*?;", self.src, re.S)
        self.assertIsNotNone(mine)
        self.assertIn("!a.link || a.link.primary", mine.group(0))
