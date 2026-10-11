"""A savings goal counts the money in the account it names, whoever owns it.

The household-only predicate keeps business-entity money out of personal
figures. Applied to a goal whose chosen account belongs to a business, it
drops every row in that account: the goal reads $0 saved and off pace while
the money sits there. A goal on a business account counts that business's
money — its account's transfers, and its own checking as the funding leg —
and a household goal still never counts the business's."""

import datetime as dt
import unittest

from oikonome.db import tenancy
from oikonome.engine import budget, entities, savings

from .util import TODAY, add_txn, make_db, write_config

PLAN = 500.0


def _goal(name, account, **over):
    g = {"name": name, "target": 10000, "monthly_plan": PLAN,
         "account_id": account, "tokens": [], "start_balance": 0,
         "mode": "sweep"}
    g.update(over)
    return g


class BusinessAccountGoalTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        ent = entities.create_entity(self.conn, name="Acme LLC",
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
        for i in range(4):
            day = TODAY - dt.timedelta(days=5 + 7 * i)
            add_txn(self.conn, day, PLAN, "TRANSFER TO BIZ SAVINGS",
                    primary="TRANSFER_OUT", account="bizchk")
            add_txn(self.conn, day, -PLAN, "TRANSFER TO BIZ SAVINGS",
                    primary="TRANSFER_IN", account="bizsav")

    def tearDown(self):
        self.conn.close()

    def _progress(self, goal):
        write_config(self.conn, budgeted_income_monthly=8000,
                     savings_goals=[goal])
        cfg = budget.load_config(self.conn)
        return cfg, {p["name"]: p for p in savings.progress(
            self.conn, cfg, TODAY)}[goal["name"]]

    def test_a_goal_on_a_business_account_reads_what_was_saved_there(self):
        _, p = self._progress(_goal("Equipment fund", "bizsav"))
        self.assertEqual(p["saved"], 4 * PLAN)

    def test_the_business_checking_leg_funds_a_business_goal(self):
        goal = _goal("Equipment fund", "bizsav")
        cfg, _ = self._progress(goal)
        rows = savings._checking_side_rows(self.conn, goal, PLAN)
        self.assertEqual(len(rows), 4)

    def test_a_household_goal_never_counts_the_business_transfers(self):
        goal = _goal("Rainy day", "sav")
        _, p = self._progress(goal)
        self.assertEqual(p["saved"], 0)
        self.assertEqual(savings._checking_side_rows(self.conn, goal, PLAN),
                         [])


if __name__ == "__main__":
    unittest.main()
