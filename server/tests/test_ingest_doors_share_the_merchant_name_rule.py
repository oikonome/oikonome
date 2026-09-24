"""Every door that re-writes a transaction's merchant name uses one rule.

A row whose aggregator name was set aside has merchant_name NULL and the
name in merchant_name_set_aside. An upsert that wrote
`merchant_name = EXCLUDED.merchant_name` by hand would hand the name back
on the next sync while leaving it in the set-aside column too — the row
would answer to a name the ledger had judged a misreading, under a merchant
that name does not point at. The rule lives in
sync.base.MERCHANT_NAME_ON_CONFLICT; this test fails when a module spells
the assignment itself instead of using it.
"""

import pathlib
import re
import unittest

from oikonome.sync import base

ROOT = pathlib.Path(base.__file__).resolve().parents[1]
BY_HAND = re.compile(r"merchant_name\s*=\s*EXCLUDED\.merchant_name", re.I)


class IngestDoorsShareTheMerchantNameRuleTests(unittest.TestCase):
    def test_no_module_assigns_the_feeds_merchant_name_by_hand(self):
        offenders = [str(p.relative_to(ROOT)) for p in ROOT.rglob("*.py")
                     if BY_HAND.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(offenders, [])

    def test_the_rule_moves_all_three_columns_together(self):
        rule = base.MERCHANT_NAME_ON_CONFLICT
        for column in ("merchant_name =", "merchant_name_set_aside =",
                       "merchant_id ="):
            self.assertIn(column, rule)


if __name__ == "__main__":
    unittest.main()
