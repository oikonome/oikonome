"""The setup wizard prints the server's answers, not its own. Three places
it worked one out and was wrong: the budget step
counted a plan as saved by its own step mark (cleared by a re-run) — and
then by any stored budget, the server's own first-run seed included — the
Done step promised the first email from the schedule's on flag with
nobody receiving it, and the self-host source table ticked a link the
bank feed had removed and replaced."""

import pathlib
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


class WizardReadsTheServerTests(unittest.TestCase):
    def test_the_budget_step_counts_a_plan_the_server_says_was_saved(self):
        # planSaved reads budgets_saved_by_user: budgets_set also holds for
        # the first-run seed the server stores on its own
        planner = _read("webapp/src/components/BudgetPlanner.tsx")
        self.assertIn("    : planSaved(od);", planner)
        self.assertNotIn('wizard_steps?.budgets === "done"', planner)
        self.assertIn("data.budgets_saved_by_user ?? data.budgets_set",
                      _read("webapp/src/checklist.ts"))
        phone = _read("mobile/src/app/welcome.tsx")
        self.assertIn("wizard={{ saved: plannerSaved || planSaved(ob.data) }}",
                      phone)
        self.assertIn("ob.budgets_saved_by_user ?? ob.budgets_set",
                      _read("mobile/src/components/wizard-logic.ts"))

    def test_done_prints_who_the_daily_message_reaches(self):
        w = _read("webapp/src/pages/Welcome.tsx")
        self.assertIn('["Email & push", reachLine(s.data?.email_reach?.daily)',
                      w)
        self.assertIn("const first = firstMessage(st.data?.email_reach?.daily);",
                      w)
        phone = _read("mobile/src/app/welcome.tsx")
        self.assertIn("finishBlurb(sv?.email_reach?.daily,", phone)
        self.assertIn('["Email & push", reachLine(sv?.email_reach?.daily)',
                      phone)

    def test_the_source_table_ticks_only_live_links(self):
        hub = _read("webapp/src/components/ConnectHub.tsx")
        grid = hub.split("{ROWS.map((r) => {")[1].split("</tr>")[0]
        self.assertIn("const linked = listed.filter((c) => !c.replaced);", grid)
        self.assertIn("c.removed_text", grid)
        self.assertIn("c.needs_attention", grid)
        self.assertNotIn('startsWith("error")', grid)


if __name__ == "__main__":
    unittest.main()
