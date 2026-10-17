"""The plan's Savings goal is found by its name trimmed and lowercased —
the way the web and mobile clients find it and the goal editor refuses a
duplicate. A goal typed " savings " is that goal: the Month lens'
saved_month, the Year lens' saved_ytd and Today's Savings / Investment row
must read it exactly as they read "Savings", never fall back to "no goal".
"""

import unittest

from oikonome.engine.savings import is_plan_savings_goal
from oikonome.web import lenses, report, todayview

from .util import TODAY, add_txn, make_db, write_config


class PlanSavingsGoalNameTests(unittest.TestCase):
    def _figures(self, name):
        conn = make_db()
        try:
            write_config(conn, savings_goals=[
                {"name": name, "monthly_plan": 500.0, "account_id": "sav",
                 "tokens": [], "start_balance": 0}])
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES ('sav','it1','Test Savings',"
                "'depository','savings',0) "
                "ON CONFLICT (tenant_id, id) DO NOTHING")
            add_txn(conn, TODAY.replace(day=2), -250.0, "TO SAVINGS",
                    account="sav", primary="TRANSFER_IN")
            m = lenses.month_summary(conn, TODAY.year, TODAY.month,
                                     today=TODAY)
            y = lenses.year_summary(conn, TODAY.year, TODAY)
            rows = todayview._plan_rows(report.gather(conn, TODAY))
            row = next((r for r in rows
                        if r["label"] == "Savings / Investment"), None)
            return m["saved_month"], y["saved_ytd"], row
        finally:
            conn.close()

    def test_the_predicate_trims_and_ignores_case(self):
        for name in ("Savings", "savings", " SAVINGS ", "SaViNgS"):
            self.assertTrue(is_plan_savings_goal({"name": name}), name)
        for name in ("Savings fund", "", None):
            self.assertFalse(is_plan_savings_goal({"name": name}), name)

    def test_a_lowercase_savings_goal_drives_the_lenses_and_today(self):
        month, ytd, row = self._figures(" savings ")
        self.assertEqual(month, 250.0)
        self.assertIsNotNone(ytd)
        self.assertIsNotNone(row)
        self.assertEqual(row["actual"], 250.0)
        self.assertEqual(row["budget"], 500.0)
        self.assertEqual((month, ytd, row), self._figures("Savings"))


if __name__ == "__main__":
    unittest.main()
