"""A bill payment that crosses a month boundary moves cash, not the plan.

Two halves of one rule in the month verdict:

- A bill due the 30th that posts a few days LATE, on the 3rd, belongs to
  last month's occurrence. It is this month's fixed spend (the cash left
  here) and never a variable purchase — left unmatched it would sit in
  "everything else" and read as overspending.
- An EARLY autopay of next month's bill raises this month's fixed bar (cash
  basis), but the dynamic variable budget is sized from this month's own
  bill schedule. Sized from the cash instead, one early rent payment would
  shrink food/other to nothing and flip UNDER to OVER with no change in
  what the household spent, while the month that owes the rent gets
  nothing back.
"""
import datetime as dt
import unittest

from oikonome.engine import budget

from .util import add_bill, add_txn, make_db, write_config


class LateBillPaymentIsFixedSpend(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=1000, other_monthly=1000)
        add_bill(self.conn, "City Water", 150.0,
                 next_due=dt.date(2026, 7, 30),
                 last_seen=dt.date(2026, 6, 30))
        # June's bill, three days late
        add_txn(self.conn, dt.date(2026, 7, 3), 150.0, "CITY WATER",
                primary="GENERAL_SERVICES", account="chk")
        add_txn(self.conn, dt.date(2026, 7, 6), 40.0, "HARDWARE BARN",
                primary="GENERAL_MERCHANDISE", account="chk")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_a_late_payment_of_last_months_bill_is_not_variable_spend(self):
        st = budget.month_status(self.conn, dt.date(2026, 7, 15))
        b = st["buckets"]
        self.assertAlmostEqual(b["other"]["actual"], 40.0)
        self.assertAlmostEqual(b["fixed"]["actual"], 150.0)

    def test_this_months_own_occurrence_stays_owed_and_is_not_overdue(self):
        """The late payment pays June, not July: July's occurrence is still
        this month's plan, and it is not overdue before its day."""
        st = budget.month_status(self.conn, dt.date(2026, 7, 15))
        self.assertEqual(st["overdue_unpaid"], [])
        # cash basis: June's late payment + July's own planned occurrence
        self.assertAlmostEqual(st["buckets"]["fixed"]["month_budget"], 300.0)
        # the forecast agrees July is still owed
        cfg = budget.load_config(self.conn)
        up = budget.upcoming_bill_occurrences(
            self.conn, cfg, dt.date(2026, 7, 15), dt.date(2026, 8, 5))
        self.assertEqual([(o["payee"], o["due"]) for o in up],
                         [("City Water", dt.date(2026, 7, 30))])

    def test_an_unpaid_last_month_occurrence_is_never_this_months_problem(self):
        """Without the late payment, June's occurrence is only a decoy: it
        must not become July's expectation or an overdue line."""
        self.conn.execute("DELETE FROM transactions WHERE name = 'CITY WATER'")
        st = budget.month_status(self.conn, dt.date(2026, 7, 15))
        self.assertEqual(st["overdue_unpaid"], [])
        self.assertAlmostEqual(st["buckets"]["fixed"]["month_budget"], 150.0)
        self.assertAlmostEqual(st["buckets"]["fixed"]["expected"], 0.0)

    def test_an_on_time_payment_last_month_stays_last_months(self):
        """Paid on its due day in June: neither this month's actual nor
        this month's plan."""
        self.conn.execute("UPDATE transactions SET date = %s "
                          "WHERE name = 'CITY WATER'", (dt.date(2026, 6, 30),))
        st = budget.month_status(self.conn, dt.date(2026, 7, 15))
        self.assertAlmostEqual(st["buckets"]["fixed"]["actual"], 0.0)
        self.assertAlmostEqual(st["buckets"]["fixed"]["month_budget"], 150.0)
        self.assertAlmostEqual(st["buckets"]["other"]["actual"], 40.0)


class EarlyAutopayLeavesTheVariableBudgetAlone(unittest.TestCase):
    TODAY = dt.date(2026, 7, 29)

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=500, other_monthly=500,
                     dynamic_variable_budget=True,
                     budgeted_income_monthly=3000.0)
        add_bill(self.conn, "Rent", 2000.0, next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        add_txn(self.conn, dt.date(2026, 7, 1), 2000.0, "RENT",
                account="chk")
        add_txn(self.conn, dt.date(2026, 7, 10), 300.0, "HARDWARE BARN",
                primary="GENERAL_MERCHANDISE", account="chk")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_next_months_rent_paid_early_does_not_shrink_this_months_budget(self):
        before = budget.month_status(self.conn, self.TODAY)
        add_txn(self.conn, dt.date(2026, 7, 28), 2000.0, "RENT",
                account="chk")
        self.conn.commit()
        after = budget.month_status(self.conn, self.TODAY)
        for name in ("food", "other"):
            self.assertAlmostEqual(after["buckets"][name]["month_budget"],
                                   before["buckets"][name]["month_budget"])
        self.assertAlmostEqual(after["buckets"]["other"]["month_budget"], 500.0)
        self.assertEqual(after["verdict"], before["verdict"])
        # the fixed bar still carries the cash that left this month
        self.assertAlmostEqual(after["buckets"]["fixed"]["month_budget"],
                               4000.0)

    def test_the_month_that_owes_the_rent_still_sizes_its_budget_by_it(self):
        """August's rent was paid in July, but August's schedule still owes
        it: August's variable budget is sized the same as any month's."""
        add_txn(self.conn, dt.date(2026, 7, 28), 2000.0, "RENT",
                account="chk")
        write_config(self.conn, food_monthly=500, other_monthly=500,
                     dynamic_variable_budget=True,
                     budgeted_income_monthly=2500.0)
        st = budget.month_status(self.conn, dt.date(2026, 8, 15))
        # 2500 income − 2000 scheduled rent = 500 for 1000 of budgets
        self.assertAlmostEqual(st["variable_scale"]["factor"], 0.5)
        self.assertAlmostEqual(st["buckets"]["other"]["month_budget"], 250.0)


if __name__ == "__main__":
    unittest.main()
