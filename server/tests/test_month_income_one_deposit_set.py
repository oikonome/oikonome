"""The month's income: what landed plus what is still due, split at one day.

The month plan's income is the deposits that landed so far plus the
paychecks still due before month end. The two halves must be read from the
same deposits and the same day, or a check is counted twice or not at all:

- a payday due today — the month's last day included — is still due until
  its deposit lands;
- a paycheck the series matched is received even when the ledger's income
  definition leaves its account or descriptor out;
- a series disabled in the budget is out of the month's income, landed
  checks included;
- a day viewed earlier in the month counts what had landed by then;
- a closed month is read under the excluded accounts it was judged with;
- the Bills headline averages a long-cadence series over the months the
  ledger has known it, not the months since its last deposit;
- a month frozen late keeps the bill schedule it already froze.
"""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import TODAY, add_bill, add_txn, make_db, write_config

CHECK = 2500.0          # one paycheck
SIDE = 300.0            # a deposit outside the paycheck series


def _pay(conn, day, amount=CHECK, name="NORTHWIND PAYROLL", account="chk",
         pending=0):
    add_txn(conn, day, -amount, name, primary="INCOME", account=account,
            pending=pending)


class MonthIncomeOneDepositSet(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, budgeted_income_monthly=5000.0)

    def _series(self, next_due, amount=CHECK, payee="Northwind Payroll",
                **kw):
        kw.setdefault("frequency", "WEEKLY")
        kw.setdefault("interval", 2)
        add_bill(self.conn, payee, amount, income=True, next_due=next_due,
                 last_seen=next_due - dt.timedelta(days=14), **kw)

    def _halves(self, day, **kw):
        st = budget.month_status(self.conn, day, **kw)
        return st["income"], st["income_received"], st["income_expected"]

    # ---- a payday due today ------------------------------------------

    def test_last_day_payday_counts_in_month_income_until_it_lands(self):
        last = dt.date(2026, 7, 31)
        _pay(self.conn, dt.date(2026, 7, 15), name="NORTHWIND MIDMONTH")
        self._series(last, frequency="MONTHLY", interval=1)
        add_bill(self.conn, "Northwind Midmonth", CHECK, income=True,
                 frequency="MONTHLY", next_due=dt.date(2026, 8, 15),
                 last_seen=dt.date(2026, 7, 15))
        income, received, expected = self._halves(last)
        self.assertAlmostEqual(received, CHECK, places=2)
        self.assertAlmostEqual(expected, CHECK, places=2)
        self.assertAlmostEqual(income, 2 * CHECK, places=2)
        self.assertAlmostEqual(budget.expected_income_remaining(
            self.conn, budget.load_config(self.conn), last), CHECK, places=2)
        # once it lands it is received, and not also still due
        _pay(self.conn, last)
        income, received, expected = self._halves(last)
        self.assertAlmostEqual(received, 2 * CHECK, places=2)
        self.assertEqual(expected, 0.0)
        self.assertAlmostEqual(income, 2 * CHECK, places=2)

    def test_a_payday_due_today_mid_month_is_still_due(self):
        _pay(self.conn, TODAY - dt.timedelta(days=14))
        self._series(TODAY)
        income, received, expected = self._halves(TODAY)
        self.assertAlmostEqual(received, CHECK, places=2)
        # today's and the one on the 29th
        self.assertAlmostEqual(expected, 2 * CHECK, places=2)
        self.assertAlmostEqual(income, 3 * CHECK, places=2)

    # ---- one deposit set for both halves --------------------------------

    def _brokerage_named_bank(self):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, access_token)"
            " VALUES ('it2','test','Charles Schwab Bank','tok-2')")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('sch','it2','Schwab Checking',"
            "'depository','checking',1000)")
        # the household's other bank has seen the month, so its income is
        # known
        add_txn(self.conn, TODAY.replace(day=2), -SIDE, "INTEREST PAYMENT",
                primary="INCOME", account="chk")

    def test_a_paycheck_into_a_brokerage_named_bank_counts_once_landed(self):
        self._brokerage_named_bank()
        _pay(self.conn, TODAY.replace(day=1), account="sch")
        self._series(TODAY.replace(day=15))
        before = self._halves(TODAY.replace(day=14))
        self.assertAlmostEqual(before[1], CHECK + SIDE, places=2)
        self.assertAlmostEqual(before[2], 2 * CHECK, places=2)
        # the 15th's check lands at the same bank: received, not dropped
        _pay(self.conn, TODAY, account="sch")
        income, received, expected = self._halves(TODAY)
        self.assertAlmostEqual(received, 2 * CHECK + SIDE, places=2)
        self.assertAlmostEqual(expected, CHECK, places=2)
        self.assertAlmostEqual(income, before[0], places=2)

    def test_a_payroll_whose_descriptor_names_a_brokerage_counts_once_landed(self):
        _pay(self.conn, TODAY.replace(day=1), name="FIDELITY PAYROLL")
        _pay(self.conn, TODAY, name="FIDELITY PAYROLL")
        self._series(TODAY.replace(day=29), payee="Fidelity Payroll")
        income, received, expected = self._halves(TODAY)
        self.assertAlmostEqual(received, 2 * CHECK, places=2)
        self.assertAlmostEqual(expected, CHECK, places=2)
        self.assertAlmostEqual(income, 3 * CHECK, places=2)

    # ---- a series disabled in the budget --------------------------------

    def test_a_disabled_only_paycheck_series_leaves_the_budgeted_income(self):
        _pay(self.conn, TODAY.replace(day=1))
        self._series(TODAY.replace(day=15))
        write_config(self.conn, budgeted_income_monthly=5000.0,
                     disabled_bills=["Northwind Payroll"])
        income, received, expected = self._halves(TODAY)
        self.assertAlmostEqual(income, 5000.0, places=2)
        self.assertIsNone(received)
        self.assertIsNone(expected)

    def test_a_disabled_series_landed_checks_are_out_of_the_month(self):
        _pay(self.conn, TODAY.replace(day=1))
        self._series(TODAY.replace(day=15))
        _pay(self.conn, TODAY.replace(day=5), SIDE, name="CONTOSO PENSION")
        add_bill(self.conn, "Contoso Pension", SIDE, income=True,
                 next_due=dt.date(2026, 8, 5), last_seen=TODAY.replace(day=5))
        write_config(self.conn, budgeted_income_monthly=5000.0,
                     disabled_bills=["Contoso Pension"])
        income, received, expected = self._halves(TODAY)
        self.assertAlmostEqual(received, CHECK, places=2)
        self.assertAlmostEqual(expected, 2 * CHECK, places=2)
        self.assertAlmostEqual(income, 3 * CHECK, places=2)

    # ---- an earlier day of the month ------------------------------------

    def test_an_earlier_day_of_the_month_counts_each_check_once(self):
        _pay(self.conn, TODAY.replace(day=1))
        _pay(self.conn, TODAY)
        self._series(TODAY.replace(day=29))
        now = self._halves(TODAY)
        self.assertAlmostEqual(now[0], 3 * CHECK, places=2)
        income, received, expected = self._halves(TODAY.replace(day=10))
        self.assertAlmostEqual(received, CHECK, places=2)
        self.assertAlmostEqual(expected, 2 * CHECK, places=2)
        self.assertAlmostEqual(income, now[0], places=2)

    # ---- a closed month under its own exclusions --------------------------

    def test_a_closed_month_reads_income_under_its_snapshot_exclusions(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('side','it1','Side Account',"
            "'depository','checking',100)")
        june = dt.date(2026, 6, 30)
        _pay(self.conn, dt.date(2026, 6, 3))
        _pay(self.conn, dt.date(2026, 6, 17))
        add_txn(self.conn, dt.date(2026, 6, 10), -SIDE, "CLIENT INVOICE",
                primary="INCOME", account="side")
        self._series(TODAY.replace(day=15))
        frozen = dict(budget.load_config(self.conn),
                      excluded_accounts=["side"])
        closed = self._halves(june, historical=True, closed=True, cfg=frozen)
        self.assertAlmostEqual(closed[0], 2 * CHECK, places=2)
        # the live config, which includes the account, reads it in
        live = self._halves(june, historical=True, closed=True)
        self.assertAlmostEqual(live[0], 2 * CHECK + SIDE, places=2)


class IncomeAverageCoversTheSeriesHistory(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def test_a_yearly_bonus_with_history_averages_over_twelve_months(self):
        for y in (2024, 2025, 2026):
            _pay(self.conn, dt.date(y, 3, 16), 12 * SIDE,
                 name="NORTHWIND BONUS")
        add_bill(self.conn, "Northwind Bonus", 12 * SIDE, frequency="YEARLY",
                 income=True, next_due=dt.date(2027, 3, 16),
                 last_seen=dt.date(2026, 3, 16))
        load = budget.recurring_load(self.conn, TODAY)
        self.assertEqual(load["income_basis"], "actual")
        self.assertEqual(load["income_months"], 12)
        self.assertAlmostEqual(load["income_monthly"], SIDE, places=2)

    def test_a_quarterly_series_averages_over_twelve_months(self):
        for d in (dt.date(2025, 3, 2), dt.date(2025, 6, 2), dt.date(2025, 9, 2),
                  dt.date(2025, 12, 2), dt.date(2026, 3, 2),
                  dt.date(2026, 6, 2)):
            _pay(self.conn, d, 3 * SIDE, name="CONTOSO DIVIDEND")
        add_bill(self.conn, "Contoso Dividend", 3 * SIDE, frequency="MONTHLY",
                 interval=3, income=True, next_due=dt.date(2026, 9, 2),
                 last_seen=dt.date(2026, 6, 2))
        load = budget.recurring_load(self.conn, TODAY)
        # Sep, Dec, Mar, Jun inside the window: four checks over twelve
        self.assertAlmostEqual(load["income_monthly"], SIDE, places=2)

    def test_a_paycheck_that_stopped_and_restarted_reads_as_new(self):
        # checks a year and a half ago, a long break, then four recent ones
        d = dt.date(2025, 1, 3)
        while d < dt.date(2025, 3, 1):
            _pay(self.conn, d)
            d += dt.timedelta(days=14)
        d = dt.date(2026, 5, 8)
        while d < dt.date(2026, 7, 1):
            _pay(self.conn, d)
            d += dt.timedelta(days=14)
        add_bill(self.conn, "Northwind Payroll", CHECK, frequency="WEEKLY",
                 interval=2, income=True, next_due=dt.date(2026, 7, 17),
                 last_seen=dt.date(2026, 6, 19))
        load = budget.recurring_load(self.conn, TODAY)
        self.assertEqual(load["income_basis"], "schedule")
        self.assertAlmostEqual(load["income_monthly"],
                               CHECK * 365.2425 / 14 / 12, delta=0.01)

    def test_a_paycheck_back_late_in_a_month_counts_from_the_next_month(self):
        # back from a long leave on Mar 27: March held one check, so
        # counting March whole would read a month's income one check short
        d = dt.date(2025, 1, 3)
        while d < dt.date(2025, 3, 1):
            _pay(self.conn, d)
            d += dt.timedelta(days=14)
        d = dt.date(2026, 3, 27)
        while d < TODAY:
            _pay(self.conn, d)
            d += dt.timedelta(days=14)
        add_bill(self.conn, "Northwind Payroll", CHECK, frequency="WEEKLY",
                 interval=2, income=True, next_due=dt.date(2026, 7, 17),
                 last_seen=dt.date(2026, 7, 3))
        load = budget.recurring_load(self.conn, TODAY)
        # Apr 10 .. Jun 19: six checks over April, May and June
        self.assertEqual(load["income_basis"], "actual")
        self.assertEqual(load["income_months"], 3)
        self.assertAlmostEqual(load["income_monthly"], 6 * CHECK / 3,
                               places=2)

    def test_an_irregular_commission_is_one_run_across_late_payments(self):
        # a monthly commission that arrives every ten weeks or so
        d = dt.date(2024, 9, 5)
        dates = []
        while d < dt.date(2026, 7, 1):
            dates.append(d)
            d += dt.timedelta(days=70)
        for d in dates:
            _pay(self.conn, d, 3 * SIDE, name="CONTOSO COMMISSION")
        add_bill(self.conn, "Contoso Commission", 3 * SIDE,
                 frequency="MONTHLY", income=True,
                 next_due=dates[-1] + dt.timedelta(days=30),
                 last_seen=dates[-1])
        load = budget.recurring_load(self.conn, TODAY)
        window = [d for d in dates
                  if dt.date(2025, 7, 1) <= d < dt.date(2026, 7, 1)]
        self.assertEqual(load["income_basis"], "actual")
        self.assertEqual(load["income_months"], 12)
        self.assertAlmostEqual(load["income_monthly"],
                               len(window) * 3 * SIDE / 12, places=2)

    def test_a_new_yearly_series_keeps_its_scheduled_rate(self):
        _pay(self.conn, dt.date(2026, 3, 16), 12 * SIDE,
             name="NORTHWIND BONUS")
        add_bill(self.conn, "Northwind Bonus", 12 * SIDE, frequency="YEARLY",
                 income=True, next_due=dt.date(2027, 3, 16),
                 last_seen=dt.date(2026, 3, 16))
        load = budget.recurring_load(self.conn, TODAY)
        self.assertEqual(load["income_basis"], "schedule")
        self.assertAlmostEqual(load["income_monthly"], SIDE, delta=1.0)


class LateMonthCloseKeepsItsSchedule(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, food_monthly=600)

    def _frozen_payees(self):
        return {r["payee"] for r in
                budget.snapshot_bill_rows(self.conn, 2026, 6) or []}

    def test_a_bill_archived_after_the_month_ended_stays_in_its_schedule(self):
        add_bill(self.conn, "Contoso Power", 120.0,
                 next_due=dt.date(2026, 7, 10),
                 last_seen=dt.date(2026, 6, 10))
        june = dt.date(2026, 6, 30)
        self.assertTrue(budget.snapshot_month(self.conn, june))
        self.assertIn("Contoso Power", self._frozen_payees())
        ended = self.conn.execute("SELECT now() AS t").fetchone()["t"]
        self.conn.execute("UPDATE bills SET active = 0")
        self.assertTrue(budget.snapshot_month(self.conn, june,
                                              unchanged_since=ended))
        self.assertIn("Contoso Power", self._frozen_payees())

    def test_a_late_close_with_no_earlier_snapshot_freezes_the_schedule(self):
        add_bill(self.conn, "Contoso Power", 120.0,
                 next_due=dt.date(2026, 7, 10),
                 last_seen=dt.date(2026, 6, 10))
        ended = self.conn.execute("SELECT now() AS t").fetchone()["t"]
        self.assertTrue(budget.snapshot_month(
            self.conn, dt.date(2026, 6, 30), unchanged_since=ended))
        self.assertIn("Contoso Power", self._frozen_payees())


if __name__ == "__main__":
    unittest.main()
