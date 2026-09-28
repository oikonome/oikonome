"""A transaction's "mark reimbursed…" must land on a page that can pair it.

The Reimburse page lists only charges flagged as awaiting reimbursement,
and "match…" (the only way to see candidate deposits) lives on those rows.
So the ledger's "↔ mark reimbursed…" has to flag an unflagged charge before
it navigates, and name the charge so the page opens its candidates;
navigating bare lands on "Nothing waiting" with no way to pair the charge
the person just picked.

Source-shaped, like the other client-shape tests here: the decision being
protected is what the client does before it navigates, not a request a
server test can probe.
"""

import pathlib
import re
import unittest

import oikonome

ROOT = pathlib.Path(oikonome.__file__).parent.parent.parent


def _read(rel: str) -> str:
    return (ROOT / rel).read_text()


class MarkReimbursedTests(unittest.TestCase):

    def setUp(self):
        try:
            self.table = _read("webapp/src/components/TxnTable.tsx")
            self.page = _read("webapp/src/pages/Reimburse.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def _branch(self) -> str:
        start = self.table.index('if (category === "__reimburse__")')
        end = self.table.index("return;\n    }", start)
        return self.table[start:end]

    def test_an_unflagged_charge_is_flagged_before_navigating(self):
        """Without the flag the page's list does not hold the charge."""
        branch = self._branch()
        self.assertIn('category: "__flag__"', branch)
        self.assertIn("reimb_flag", branch)

    def test_the_charge_is_named_to_the_page(self):
        """The page is told which charge to open, and opens it."""
        self.assertRegex(self._branch(), r"/reimburse\?match=")
        self.assertIn('params.get("match")', self.page)
        self.assertTrue(re.search(
            r"useState<string \| null>\(\(\) => params\.get\(\"match\"\)\)",
            self.page))


if __name__ == "__main__":
    unittest.main()
