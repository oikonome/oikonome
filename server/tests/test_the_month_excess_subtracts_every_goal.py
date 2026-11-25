"""The month's excess is one figure: income − bills − planned spending −
the monthly plan of EVERY savings goal (budget.month_status →
plan_surplus). The planners show it and list each goal's plan above it,
so the status names the goals that figure subtracts (`plan_goals`), and
the plan's Savings row carries the same total the plan line calls
"saved" — a second goal must not leave the row at the Savings goal's plan
while the line beside it subtracts both. A sweep goal takes from the
excess only when the month provides it, so it is in neither. Figures are
invented."""

import unittest

from oikonome.engine import budget
from oikonome.web import report, todayview

from .util import TODAY, add_bill, make_db, write_config


def _goal(name, plan, **kw):
    g = {"name": name, "target": 0, "monthly_plan": plan, "tokens": [],
         "start_balance": 0}
    g.update(kw)
    return g


class MonthExcessEveryGoalTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('sav','it1','Home Savings',"
            "'depository','savings',0)")
        add_bill(self.conn, "ROOFTOP RENT", 1500.0,
                 next_due=TODAY.replace(day=1))
        write_config(self.conn, budgeted_income_monthly=6000,
                     food_monthly=800, other_monthly=700,
                     savings_goals=[
                         _goal("Savings", 300, account_id="sav"),
                         _goal("Trip fund", 200, account_id="sav"),
                         _goal("Year-end sweep", 100, mode="sweep")])

    def tearDown(self):
        self.conn.close()

    def test_the_status_names_every_goal_the_excess_subtracts(self):
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(
            [(g["name"], g["monthly_plan"]) for g in st["plan_goals"]],
            [("Savings", 300.0), ("Trip fund", 200.0)])
        self.assertAlmostEqual(
            st["savings_plan"],
            sum(g["monthly_plan"] for g in st["plan_goals"]), places=2)
        self.assertAlmostEqual(
            st["plan_surplus"],
            st["income"] - st["avg_bills"] - st["plan_variable"] - 500.0,
            places=2)

    def test_the_savings_row_carries_the_plan_lines_saved_figure(self):
        st = report.gather(self.conn, TODAY)
        row = next(r for r in todayview._plan_rows(st)
                   if r["label"] == "Savings / Investment")
        self.assertAlmostEqual(row["budget"], st["savings_plan"], places=2)
        self.assertAlmostEqual(row["budget"], 500.0, places=2)
        self.assertEqual(row["caption"], "transferred vs plan")

    def test_a_row_showing_the_three_month_average_says_so(self):
        # a status without this month's figure shows the trailing average;
        # the caption must not call that this month's transfers
        st = report.gather(self.conn, TODAY)
        for g in st["savings_goals"]:
            g.pop("saved_this_month", None)
        row = next(r for r in todayview._plan_rows(st)
                   if r["label"] == "Savings / Investment")
        self.assertEqual(row["caption"], "avg / mo, last 3 months")

    def test_the_bills_row_says_it_is_not_in_the_verdict(self):
        # one caption for web, phone and email: what posted, what awaits,
        # and that bills are planned but never judged
        st = report.gather(self.conn, TODAY)
        row = next(r for r in todayview._plan_rows(st)
                   if r["label"] == "Bills")
        self.assertTrue(row["caption"].startswith("posted $"))
        self.assertTrue(row["caption"].endswith(
            " · not counted in the verdict"))

    def test_the_variable_bar_is_the_sum_of_its_rows(self):
        st = report.gather(self.conn, TODAY)
        rows = todayview._plan_rows(st)
        total = todayview.plan_total(rows)
        judged = [r for r in rows if r["judged"]]
        self.assertAlmostEqual(total["budget"],
                               sum(r["budget"] for r in judged), places=2)
        self.assertAlmostEqual(total["actual"],
                               sum(r["actual"] for r in judged), places=2)


if __name__ == "__main__":
    unittest.main()
