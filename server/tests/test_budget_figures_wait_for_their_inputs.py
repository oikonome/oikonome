"""The budget planners never show or save a figure worked out from a read
that has not answered.

The planner's Bills comes from the Today payload, the wizard's from the
budget suggestion, and the Savings plan is income less Bills less
spending. A read that is still loading, or failed, is not $0 of bills: read
as one, the Savings plan comes out too high and Save stores it. So while
an input is unknown the planners print the unknown mark, show no worked-out
figure, and hold Save with the shared reason (plannerWaits, pinned by the
case table in mobile/scripts/money-cases.mjs). A first run also waits for
the suggestion's answer rather than filling the plan in from $0.
"""

import pathlib
import re
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = _APP / "webapp" / "src"
_MOB = _APP / "mobile" / "src"


def _src(test: unittest.TestCase, p: pathlib.Path) -> str:
    if not p.exists():
        test.skipTest(f"{p} not present")
    return p.read_text()


def _fn(src: str, head: str) -> str:
    body = src[src.index(head):]
    return body[:body.index("\n}\n")]


class BudgetFiguresWaitForTheirInputs(unittest.TestCase):
    def test_the_planners_wait_for_the_bills_and_the_first_run_history(self):
        for p, head in ((_WEB / "components" / "BudgetPlanner.tsx",
                         "export default function BudgetPlanner("),
                        (_MOB / "app" / "budget.tsx",
                         "export function Planner(")):
            body = _fn(_src(self, p), head)
            self.assertNotRegex(body, r"avg_bills\s*\?\?\s*0", p)
            self.assertNotRegex(body, r"avg_bills_count\s*\?\?\s*0", p)
            self.assertIn("plannerWaits(inputPhase(now),", body, p)
            # the auto savings figure is unknown while an input is
            self.assertRegex(body, r"const savingsAuto = waits \? null", p)
            # a first run waits for the suggestion's answer, not only for
            # it to stop loading
            self.assertNotIn("sug.isPending", body, p)
            self.assertIn("!sug.data", body, p)
            # nothing is saved from a missing input; Save also holds while
            # an untouched savings box waits for its suggestion
            self.assertIn("const holds = waits ?? savingsWaits;", body, p)
            save = body[body.index("const save = useMutation("):]
            self.assertRegex(save[:save.index("const cur =")],
                             r"if \(holds\) throw new Error\(holds\);", p)
            self.assertGreaterEqual(body.count("save.isPending || !!holds"),
                                    1, p)
            self.assertIn("UNKNOWN_FIGURE", body, p)

    def test_the_wizard_budgets_wait_for_the_suggested_bills(self):
        for p, head in ((_WEB / "components" / "ManualBudgets.tsx",
                         "export default function ManualBudgets("),
                        (_MOB / "components" / "wizard-budgets.tsx",
                         "export function ManualBudgets(")):
            body = _fn(_src(self, p), head)
            self.assertNotRegex(body, r"avg_bills_monthly\s*\?\?\s*0", p)
            self.assertIn('plannerWaits(inputPhase(sug), "ready")', body, p)
            self.assertIn("save.isPending || !!waits", body, p)
            self.assertIn("bills === null ? UNKNOWN_FIGURE", body, p)
        web = _src(self, _WEB / "components" / "ManualBudgets.tsx")
        self.assertIn("if (bills === null) throw new Error(", web)
        logic = _fn(_src(self, _MOB / "components" / "wizard-logic.ts"),
                    "export function manualBudgetsBody(")
        self.assertIn("bills: number | null", logic)
        self.assertIn("if (bills === null) throw new Error(PLANNER_BILLS_FAILED);",
                      logic)

    def test_the_figures_wait_block_is_the_same_text_on_both(self):
        head = "// ---- figures that wait for their inputs: twin block ----"
        tail = "// ---- end of the figures-wait twin block ----"
        blocks = []
        for p in (_WEB / "moneymath.ts", _MOB / "lib" / "pure.ts"):
            src = _src(self, p)
            self.assertEqual(src.count(head), 1, p)
            blocks.append(src[src.index(head):src.index(tail)])
        self.assertEqual(blocks[0], blocks[1])
        # the web's money(null) prints the same one mark
        client = _src(self, _WEB / "api" / "client.ts")
        self.assertRegex(client, re.escape(
            "if (v === null || v === undefined) return UNKNOWN_FIGURE;"))


if __name__ == "__main__":
    unittest.main()
