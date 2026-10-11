"""The monthly bill load is the schedule's cost evened over its cycle.

The Bills headline ('bills / mo'), the month plan's avg_bills (and with it
plan_surplus, which sweep-mode savings goals size their transfer from) and
the planner's avg_bills_monthly all quote one figure. It must be the bill's
standing monthly cost, not what happens to fall due in this calendar year
after the history floor: a hand-added bill has no payment history, so every
occurrence before its first due date is floored, and counting only the rest
reads a $2,000 rent due in November as $333/mo. A bill on a two-year cycle
falls due in one calendar year of two, so a per-calendar-year count reads it
as $0/mo one year and twice its cost the next.
"""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import TODAY, _ensure_db, add_bill, make_db, write_config


class MonthlyBillLoadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _all_three(self):
        return (budget.recurring_load(self.conn, TODAY)["bills_monthly"],
                budget.month_status(self.conn, TODAY)["avg_bills"],
                budget.suggest_budgets(self.conn, TODAY)["avg_bills_monthly"])

    def test_a_new_bill_with_no_history_counts_its_full_monthly_cost(self):
        # no last_seen: the series starts at its first due date
        add_bill(self.conn, "Rent", 2000.0,
                 next_due=dt.date(TODAY.year, 11, 1), source="manual")
        for figure in self._all_three():
            self.assertAlmostEqual(figure, 2000.0, places=2)

    def test_a_bill_added_in_december_due_in_january_still_costs_its_rate(self):
        add_bill(self.conn, "Gym", 60.0,
                 next_due=dt.date(TODAY.year + 1, 1, 5), source="manual")
        for figure in self._all_three():
            self.assertAlmostEqual(figure, 60.0, places=2)

    def test_a_biennial_bill_is_evened_over_two_years(self):
        add_bill(self.conn, "Passport Renewal Fund", 480.0,
                 frequency="YEARLY", interval=2,
                 next_due=dt.date(TODAY.year + 1, 3, 10),
                 last_seen=dt.date(TODAY.year - 1, 3, 10))
        for figure in self._all_three():
            self.assertAlmostEqual(figure, 20.0, places=2)
        # and the same in the year it falls due
        nxt = TODAY.replace(year=TODAY.year + 1)
        self.assertAlmostEqual(
            budget.recurring_load(self.conn, nxt)["bills_monthly"], 20.0,
            places=2)

    def test_an_eighteen_month_cycle_is_evened_over_its_cycle(self):
        add_bill(self.conn, "Water Heater Service", 360.0,
                 frequency="MONTHLY", interval=18,
                 next_due=dt.date(TODAY.year, 9, 1),
                 last_seen=dt.date(TODAY.year - 2, 3, 1))
        hero, plan, planner = self._all_three()
        for figure in (hero, plan, planner):
            self.assertAlmostEqual(figure, 20.0, places=2)

    def test_new_income_with_no_history_counts_its_full_rate(self):
        add_bill(self.conn, "Northwind Payroll", 3000.0, income=True,
                 next_due=dt.date(TODAY.year, 12, 1), source="manual")
        self.assertAlmostEqual(
            budget.recurring_load(self.conn, TODAY)["income_monthly"],
            3000.0, places=2)


if __name__ == "__main__":
    unittest.main()
