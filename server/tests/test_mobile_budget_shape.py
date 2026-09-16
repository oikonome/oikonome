"""Mobile Budget and bill history: drafts survive, money math is guarded.

Three invariants with quiet failure modes. Every Budget card holding an
unsaved draft must be in the sibling dirty-guard — a card outside it is
silently remounted when a neighbour saves, and the draft evaporates.
The bill-history transaction list must stay bounded — the server returns
every match over 24 months and the plain ScrollView mounts each row
synchronously, so an uncapped list janks or crashes on a frequent
merchant. And the pure money/merge logic (banker's rounding that keeps
whole-dollar figures in lock-step with the server and email; the
savings-goals merge that is the only lost-update guard for the array)
must keep its node-runnable unit tests, because mobile has no JS test
framework and nothing else executes that logic.
"""

import pathlib
import shutil
import subprocess
import unittest

import oikonome

_MOBILE = (pathlib.Path(oikonome.__file__).parent.parent.parent / "mobile")


def _src(rel: str) -> str:
    return (_MOBILE / "src" / rel).read_text()


class MobileBudgetShapeTests(unittest.TestCase):
    def _read(self, rel: str) -> str:
        try:
            return _src(rel)
        except FileNotFoundError:
            self.skipTest("mobile/ not present")

    def test_every_budget_card_is_in_the_sibling_dirty_guard(self):
        src = self._read("app/budget.tsx")
        # the remount loop iterates p/i/g/b — each of those cards must
        # also report dirtiness, or its key stays "clean" and a sibling
        # save wipes its unsaved draft
        for key in ("p", "i", "g", "b"):
            self.assertIn(f'onDirty={{markDirty("{key}")}}', src,
                          f'card "{key}" can be remounted mid-edit')

    def test_income_scenarios_edits_mark_the_card_dirty(self):
        src = self._read("app/budget.tsx")
        scenarios = src[src.index("function IncomeScenarios"):
                        src.index("// ---- savings goals ----")]
        self.assertIn("onDirty?.(true)", scenarios)

    def test_bill_history_txn_list_is_bounded_and_says_so(self):
        # bounded RENDER, honest about the remainder: pages of rows
        # seeded at 60 with an explicit show-more, never an unbounded
        # synchronous mount of a category-pool envelope's whole ledger
        src = self._read("app/bill-history.tsx")
        self.assertIn("d.txns.slice(0, shownTxns)", src)
        self.assertIn("useState(60)", src)
        self.assertIn("show more", src)

    def test_whole_dollar_money_routes_through_bankers_rounding(self):
        pure = self._read("lib/pure.ts")
        self.assertIn("const r = pyround(n)", pure)
        self.assertNotIn("Math.round", pure)
        # theme.ts must re-export the tested implementation, not keep a
        # second private copy that drifts
        theme = self._read("lib/theme.ts")
        self.assertIn('export { money } from "./pure"', theme)

    def test_savings_goals_save_uses_the_extracted_merge(self):
        src = self._read("app/budget.tsx")
        self.assertIn("mergeSavingsGoals(rows, cur.savings_goals ?? []", src)

    def test_pure_logic_unit_tests_exist_and_pass_under_node(self):
        if not _MOBILE.exists():
            self.skipTest("mobile/ not present")
        script = _MOBILE / "scripts" / "pure-logic-tests.mjs"
        self.assertTrue(script.exists(),
                        "the pure-logic tests are the only executable "
                        "coverage of pyround/money/mergeSavingsGoals")
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        r = subprocess.run(
            [node, "--experimental-strip-types", str(script)],
            capture_output=True, text=True, cwd=str(_MOBILE), timeout=120)
        if r.returncode != 0 and "bad option" in (r.stderr or ""):
            self.skipTest("node too old for --experimental-strip-types")
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)


if __name__ == "__main__":
    unittest.main()
