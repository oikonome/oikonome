"""The Bills headline, the month snapshot and the planner count the same
bills the month plan does.

A hand-tracked card-payment bill leaves the plan while live card debt
exists: the card machinery already pays that debt, so the plan would
reserve it twice. The Bills page headline (recurring_load, which the
month snapshot stores) and the planner's bill prefill (suggest_budgets)
must leave it out too, or the Bills page reads a bigger monthly load than
Budget and Today for the identical schedule, and a plan balanced against
the bigger figure shows a standing surplus that does not exist.
"""
import datetime as dt
import unittest

from oikonome.engine import budget

from .util import TODAY, add_bill, make_db, write_config


class CardPayBillTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)       # the fixture card carries live debt
        add_bill(self.conn, "Hillside Rent", 1000.0,
                 next_due=TODAY + dt.timedelta(days=3),
                 last_seen=TODAY - dt.timedelta(days=27))
        add_bill(self.conn, "CHASE CREDIT CRD AUTOPAY", 1200.0,
                 next_due=TODAY + dt.timedelta(days=4),
                 last_seen=TODAY - dt.timedelta(days=26))

    def tearDown(self):
        self.conn.close()

    def test_every_reader_counts_the_plans_bill_set(self):
        plan = budget.month_status(self.conn, TODAY)["avg_bills"]
        load = budget.recurring_load(self.conn, TODAY)
        self.assertAlmostEqual(load["bills_monthly"], plan, places=2)
        self.assertAlmostEqual(load["bills_monthly"], 1000.0, places=2)
        # the week ahead holds the rent, not the card payment the card
        # payoff already reserves
        self.assertAlmostEqual(load["week_total"], 1000.0, places=2)
        sug = budget.suggest_budgets(self.conn, TODAY)
        self.assertAlmostEqual(sug["avg_bills_monthly"], plan, places=2)

    def test_without_card_debt_the_bill_is_counted_everywhere(self):
        self.conn.execute("UPDATE accounts SET balance_current = 0 "
                          "WHERE type = 'credit'")
        plan = budget.month_status(self.conn, TODAY)["avg_bills"]
        self.assertAlmostEqual(plan, 2200.0, places=2)
        self.assertAlmostEqual(
            budget.recurring_load(self.conn, TODAY)["bills_monthly"], plan,
            places=2)


if __name__ == "__main__":
    unittest.main()
