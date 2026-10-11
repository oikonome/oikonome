"""A savings goal on a business's account is the business's commitment.

The business pays the transfer out of its own checking, and every
household figure (the plan surplus, the cash forecast, the sweep line)
starts from household-only balances. Charging such a goal's plan to them
understated the household surplus and walked money out of household cash
that never leaves it; a sweep goal's own business-side transfer read as
the household having swept. With the combined view on, the household
figures include the businesses, and so do their goals. Figures are
invented."""

import unittest

from oikonome.db import tenancy
from oikonome.engine import budget, entities, forecast, savings

from .util import TODAY, add_txn, make_db, write_config

PLAN = 500.0


def _goal(name, account, mode="monthly"):
    return {"name": name, "target": 10000, "monthly_plan": PLAN,
            "account_id": account, "tokens": [], "start_balance": 0,
            "mode": mode}


class BusinessGoalHouseholdPlanTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        ent = entities.create_entity(self.conn, name="Fixture Works LLC",
                                     structure="sole_prop")
        for aid, name, sub in (("bizsav", "Biz Savings", "savings"),
                               ("bizchk", "Biz Checking", "checking"),
                               ("sav", "Home Savings", "savings")):
            self.conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES (%s,'it1',%s,'depository',%s,0)",
                (aid, name, sub))
        entities.assign_account(self.conn, "bizsav", ent["id"])
        entities.assign_account(self.conn, "bizchk", ent["id"])
        tenancy.set_entity_scope(self.conn)

    def tearDown(self):
        self.conn.close()

    def _config(self, goals, **over):
        write_config(self.conn, budgeted_income_monthly=8000,
                     savings_goals=goals, **over)
        tenancy.set_entity_scope(self.conn)

    def _surplus(self):
        return budget.month_status(self.conn, TODAY)

    def test_a_business_goal_is_not_taken_from_the_household_surplus(self):
        self._config([])
        base = self._surplus()["plan_surplus"]
        self._config([_goal("Equipment fund", "bizsav")])
        st = self._surplus()
        self.assertEqual(st["savings_plan"], 0)
        self.assertAlmostEqual(st["plan_surplus"], base, places=2)
        # a household goal still is
        self._config([_goal("Equipment fund", "bizsav"),
                      _goal("Rainy day", "sav")])
        st = self._surplus()
        self.assertEqual(st["savings_plan"], PLAN)
        self.assertAlmostEqual(st["plan_surplus"], base - PLAN, places=2)

    def test_the_forecast_does_not_walk_a_business_goal_out_of_household_cash(self):
        self._config([_goal("Equipment fund", "bizsav"),
                      _goal("Rainy day", "sav")])
        names = {e[2] for e in forecast.build(self.conn, days=90)["events"]}
        self.assertIn("Rainy day (savings plan)", names)
        self.assertNotIn("Equipment fund (savings plan)", names)

    def test_a_business_sweep_is_not_the_household_sweeping(self):
        day = TODAY.replace(day=1)
        add_txn(self.conn, day, PLAN, "TRANSFER TO BIZ SAVINGS",
                primary="TRANSFER_OUT", account="bizchk")
        add_txn(self.conn, day, -PLAN, "TRANSFER TO BIZ SAVINGS",
                primary="TRANSFER_IN", account="bizsav")
        self._config([_goal("Equipment fund", "bizsav", mode="sweep")])
        st = self._surplus()
        self.assertEqual(st["sweep_plan"], 0)
        self.assertEqual(st["sweep_posted"], 0)

    def test_the_combined_view_counts_a_business_goal(self):
        self._config([_goal("Equipment fund", "bizsav")],
                     combine_entities=True)
        self.assertEqual(self._surplus()["savings_plan"], PLAN)
        cfg = budget.load_config(self.conn)
        self.assertEqual(
            [g["name"] for g in savings.household_goals(self.conn, cfg)],
            ["Equipment fund"])


if __name__ == "__main__":
    unittest.main()
