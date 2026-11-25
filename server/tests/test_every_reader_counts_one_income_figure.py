"""The household's monthly income is one figure, read one way.

The Bills headline, the budget planner's suggested income and the
forecast's standing surplus all quote "what the income series bring in a
month". Each used to build its own series set: the planner kept series the
household had switched off, the headline and planner counted deposits into
excluded accounts the forecast dropped. A switched-off pension that has not
started then read as income in the plan and not on the Bills page.

A daily or weekly cadence never lines up with twelve calendar months: a
twelve-month window that opens on a payday holds 27 biweekly checks, so the
deposits' average must not read 27/26 of the cycle's rate.
"""

import datetime as dt
import unittest

from oikonome.engine import budget, forecast

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 15)


class OneIncomeFigure(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _forecast_income(self):
        """The income the forecast's standing surplus swaps in: a status
        with plan_surplus 0 and income 0 returns exactly that figure."""
        st = {"plan_surplus": 0.0, "income": 0.0, "income_received": 1.0,
              "income_expected": None}
        return forecast.standing_surplus(self.conn, st, TODAY)

    def test_a_switched_off_series_is_out_of_every_reader(self):
        write_config(self.conn, disabled_bills=["Harbor Pension"])
        # an active monthly payroll with a year of deposits
        d = dt.date(2025, 5, 1)
        while d < TODAY:
            add_txn(self.conn, d, -4000.0, "LANTERN WORKS PAYROLL",
                    primary="INCOME", account="chk")
            d = (d + dt.timedelta(days=32)).replace(day=1)
        add_bill(self.conn, "Lantern Works Payroll", 4000.0, income=True,
                 next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        # a pension that has not started, switched off: a schedule, no
        # deposits
        add_bill(self.conn, "Harbor Pension", 1500.0, income=True,
                 next_due=dt.date(2026, 9, 1))
        head = budget.recurring_load(self.conn, TODAY)["income_monthly"]
        plan = budget.suggest_budgets(
            self.conn, TODAY)["suggestions"]["income_monthly"]
        self.assertAlmostEqual(head, 4000.0, places=2)
        self.assertAlmostEqual(plan, head, places=2)
        self.assertAlmostEqual(self._forecast_income(), head, places=2)

    def test_deposits_into_an_excluded_account_count_nowhere(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('side','it1','Side Checking',"
            "'depository','checking',0)")
        write_config(self.conn, excluded_accounts=["side"])
        d = dt.date(2025, 5, 1)
        while d < TODAY:
            add_txn(self.conn, d, -4000.0, "LANTERN WORKS PAYROLL",
                    primary="INCOME", account="chk")
            # the same payer's second deposit lands in an excluded account
            add_txn(self.conn, d.replace(day=2), -500.0,
                    "LANTERN WORKS PAYROLL", primary="INCOME",
                    account="side")
            d = (d + dt.timedelta(days=32)).replace(day=1)
        add_bill(self.conn, "Lantern Works Payroll", 4000.0, income=True,
                 next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        head = budget.recurring_load(self.conn, TODAY)["income_monthly"]
        plan = budget.suggest_budgets(
            self.conn, TODAY)["suggestions"]["income_monthly"]
        self.assertAlmostEqual(head, 4000.0, places=2)
        self.assertAlmostEqual(plan, head, places=2)
        self.assertAlmostEqual(self._forecast_income(), head, places=2)

    def test_a_switched_off_unkeyable_series_is_not_the_planners_fallback(self):
        # nothing the matcher can key, so the planner falls back to the
        # stored monthly figures — which must leave the switched-off one out
        write_config(self.conn, disabled_bills=["Old Stipend"])
        self.conn.execute(
            "INSERT INTO bills (id, payee, amount, type, frequency, "
            "monthly_amount, active, raw) VALUES "
            "('rec:old-stipend', 'Old Stipend', 900, 'INCOME', 'MONTHLY', 900, 1, '{}'::jsonb)")
        plan = budget.suggest_budgets(
            self.conn, TODAY)["suggestions"].get("income_monthly") or 0.0
        self.assertAlmostEqual(plan, 0.0, places=2)


class BiweeklyWindowWithAnExtraPayday(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_twenty_seven_checks_in_the_window_read_the_cycles_rate(self):
        # every 14 days through 2025-07-01: the window
        # [2025-07-01, 2026-07-01) holds 27 of them (the last 2026-06-30)
        today = dt.date(2026, 7, 10)
        d, n = dt.date(2025, 5, 20), 0
        while d < today:
            add_txn(self.conn, d, -2000.0, "LANTERN WORKS PAYROLL",
                    primary="INCOME", account="chk")
            if dt.date(2025, 7, 1) <= d < dt.date(2026, 7, 1):
                n += 1
            d += dt.timedelta(days=14)
        self.assertEqual(n, 27)
        add_bill(self.conn, "Lantern Works Payroll", 2000.0,
                 frequency="WEEKLY", interval=2, income=True,
                 next_due=dt.date(2026, 7, 14),
                 last_seen=dt.date(2026, 6, 30))
        head = budget.recurring_load(self.conn, today)["income_monthly"]
        self.assertAlmostEqual(head, 2000 * 365.2425 / 14 / 12, delta=0.05)

class AnEndedSeriesIsNotRevived(unittest.TestCase):
    """A series with endOn in the past and nothing in the window comes to
    zero on its own terms; the stored-schedule fallback is for series the
    matcher cannot key, never for ones that honestly evaluate to zero."""

    def test_the_stored_schedule_does_not_stand_in(self):
        import datetime as dt
        from oikonome.engine import budget
        from .util import add_bill, make_db, write_config
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        add_bill(conn, "Old Employer Payroll", 3000.0, income=True,
                 frequency="WEEKLY", interval=2,
                 next_due=dt.date(2024, 6, 14), last_seen=dt.date(2024, 5, 31))
        conn.execute("UPDATE bills SET raw = jsonb_set(raw, '{recurrence,endOn}', "
                     "'\"2024-06-30\"') WHERE payee = 'Old Employer Payroll'")
        cfg = budget.load_config(conn)
        fig = budget.income_monthly_figure(conn, cfg, dt.date(2026, 11, 8))
        self.assertEqual(fig["monthly"], 0.0)

class AnImpossibleEndDateIsNoEndDate(unittest.TestCase):
    """A stored endOn that does not parse ("2026-02-30") is no end date for
    the stored-schedule fallback, as it is for every other reader — never
    an error that takes the Bills headline, Today and the forecast down."""

    def test_the_fallback_reads_past_it(self):
        import datetime as dt
        from oikonome.engine import budget
        from .util import add_bill, make_db, write_config
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        add_bill(conn, "Invented Payroll", 2500.0, income=True,
                 frequency="MONTHLY", interval=1,
                 next_due=dt.date(2026, 12, 1), last_seen=dt.date(2026, 11, 1))
        conn.execute("UPDATE bills SET raw = jsonb_set(raw, '{recurrence,endOn}', "
                     "'\"2026-02-30\"') WHERE payee = 'Invented Payroll'")
        cfg = budget.load_config(conn)
        fig = budget.income_monthly_figure(conn, cfg, dt.date(2026, 11, 8))
        self.assertEqual(fig["monthly"], 2500.0)


if __name__ == "__main__":
    unittest.main()
