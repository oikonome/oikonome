"""The budget planner's "where each dollar goes" shares follow the
server's rule. They are worked out on the client, because they echo
amounts still being typed, by plannerShares (web moneymath.ts, phone
lib/pure.ts); the server's reporting.shown_shares decides the same
figures, and budget.plan_line carries them for the saved plan. One case
table (mobile/scripts/share-cases.json) runs through all three: here
through the server, and through both twins in test_client_money_twins."""

import json
import pathlib
import random
import unittest

import oikonome
from oikonome.engine import budget
from oikonome.engine.reporting import shown_shares

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_CASES = _APP / "mobile" / "scripts" / "share-cases.json"


class ShareCaseTableTests(unittest.TestCase):
    def test_the_server_decides_every_case_as_the_table_says(self):
        cases = json.loads(_CASES.read_text())["cases"]
        self.assertGreaterEqual(len(cases), 8)
        for c in cases:
            with self.subTest(case=c["name"]):
                self.assertEqual(shown_shares(c["parts"], c["base"]),
                                 c["shares"])


class PlanLineSharesTests(unittest.TestCase):
    def test_the_legs_share_the_income_adding_to_100(self):
        pl = budget.plan_line(4000, 2000, 990, 510, 500)
        self.assertEqual(pl["shares"], {"bills": 50, "to_spend": 25,
                                        "saved": 13, "excess": 12})
        # the planner, nothing typed: out (bills and to spend), save and
        # the excess as the table's "out, save and excess" case reads them
        self.assertEqual(shown_shares([2990, 510, 500], 4000), [75, 13, 12])

    def test_three_equal_legs_read_34_33_33(self):
        pl = budget.plan_line(1500, 500, 500, 0, 500)
        self.assertEqual(pl["shares"], {"bills": 34, "to_spend": 33,
                                        "saved": None, "excess": 33})
        self.assertIsNone(pl["saved"])

    def test_a_plan_past_its_income_has_no_excess_share(self):
        pl = budget.plan_line(3000, 2000, 1500, 0, -500)
        self.assertIsNone(pl["shares"]["excess"])
        self.assertEqual(pl["shares"]["bills"] + pl["shares"]["to_spend"],
                         117)

    def test_the_shares_add_to_100_whenever_the_legs_are_the_income(self):
        rnd = random.Random(7)
        for _ in range(300):
            legs = [round(rnd.uniform(0, 3000), 2) for _ in range(3)]
            income = round(sum(legs) + rnd.uniform(0.01, 2000), 2)
            excess = round(income - sum(legs), 2)
            pl = budget.plan_line(income, *legs, excess)
            with self.subTest(legs=legs, income=income):
                self.assertEqual(sum(v for v in pl["shares"].values()
                                     if v is not None), 100)


class PlannerScreensTests(unittest.TestCase):
    def test_neither_planner_divides_a_share_on_its_own(self):
        for rel in ("webapp/src/components/BudgetPlanner.tsx",
                    "mobile/src/app/budget.tsx"):
            src = (_APP / rel).read_text()
            with self.subTest(screen=rel):
                self.assertIn("plannerShares(", src)
                self.assertNotIn("Math.round((100 * v) / inc)", src)


if __name__ == "__main__":
    unittest.main()
