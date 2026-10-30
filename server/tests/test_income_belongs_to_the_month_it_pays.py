"""A paycheck belongs to the month it pays, and to the series it pays.

- A check due on a weekend 1st and paid the Friday before is the new
  month's income, not the old month's second check: the new month must not
  read a paycheck short (and squeeze every variable budget to nothing).
- A payroll kept as two monthly series from one payer (the 1st's and the
  15th's) has each series claim its own checks, so the average is the two
  checks, not one series' twice plus the other's schedule on top.
- A check landing in an excluded twin account is neither averaged nor
  sized a second time.
- A long-cadence series' deposits never stand for fewer cycles than they
  are, so a bonus that came a few weeks early or a quarter-end series in
  its first year reads its true rate.
- A closed month's averages are claimed by its frozen schedule alone.
- One request reads the deposit set once.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import bills, budget, forecast

from .util import TODAY, add_bill, add_txn, make_db, write_config

CHECK = 2500.0
SIDE = 300.0


def _pay(conn, day, amount=CHECK, name="NORTHWIND PAYROLL", account="chk"):
    add_txn(conn, day, -amount, name, primary="INCOME", account=account)


def _months(start: dt.date, until: dt.date, day: int):
    d = start.replace(day=day)
    while d < until:
        yield d
        d = (d.replace(day=1) + dt.timedelta(days=32)).replace(day=day)


class PaycheckBelongsToItsMonth(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, budgeted_income_monthly=1000.0)

    def test_a_sunday_the_first_paycheck_paid_friday_counts_in_its_own_month(self):
        # Nov 1 2026 is a Sunday: the employer pays Friday Oct 30
        for d in (dt.date(2026, 9, 1), dt.date(2026, 10, 1),
                  dt.date(2026, 10, 30)):
            _pay(self.conn, d)
        add_bill(self.conn, "Northwind Payroll", CHECK, income=True,
                 next_due=dt.date(2026, 12, 1),
                 last_seen=dt.date(2026, 10, 30))
        nov = budget.month_status(self.conn, dt.date(2026, 11, 3))
        self.assertAlmostEqual(nov["income_received"], CHECK, places=2)
        self.assertAlmostEqual(nov["income"], CHECK, places=2)
        # and October holds only its own check, open or closed
        octo = budget.month_status(self.conn, dt.date(2026, 10, 30))
        self.assertAlmostEqual(octo["income"], CHECK, places=2)
        closed = budget.month_status(self.conn, dt.date(2026, 10, 31),
                                     historical=True, closed=True)
        self.assertAlmostEqual(closed["income"], CHECK, places=2)

    def test_a_check_paid_two_days_late_counts_in_the_month_it_pays(self):
        for d in (dt.date(2026, 5, 29), dt.date(2026, 7, 2)):
            _pay(self.conn, d)
        add_bill(self.conn, "Northwind Payroll", CHECK, income=True,
                 next_due=dt.date(2026, 7, 30),
                 last_seen=dt.date(2026, 7, 2), frequency="MONTHLY")
        # the 30th's check for June landed on Jul 2
        self.conn.execute("UPDATE bills SET raw = jsonb_set(raw, '{dueOn}',"
                          " '\"2026-07-30\"')")
        june = budget.month_status(self.conn, dt.date(2026, 6, 30),
                                   historical=True, closed=True)
        self.assertAlmostEqual(june["income"], CHECK, places=2)

    def test_the_first_and_fifteenth_series_each_claim_their_own_checks(self):
        start = dt.date(2025, 1, 1)
        for d in list(_months(start, TODAY + dt.timedelta(days=1), 1)) + \
                list(_months(start, TODAY + dt.timedelta(days=1), 15)):
            _pay(self.conn, d, name="ACME PAYROLL")
        add_bill(self.conn, "Acme Payroll", CHECK, income=True,
                 next_due=dt.date(2026, 8, 1), last_seen=dt.date(2026, 7, 1))
        add_bill(self.conn, "Acme Payroll Mid", CHECK, income=True,
                 next_due=dt.date(2026, 8, 15),
                 last_seen=dt.date(2026, 7, 15))
        load = budget.recurring_load(self.conn, TODAY)
        self.assertEqual(load["income_basis"], "actual")
        self.assertAlmostEqual(load["income_monthly"], 2 * CHECK, places=2)
        series = budget._recurring_bills(self.conn, income=True)
        claims = budget._claim_income_rows(
            bills._income_rows(self.conn, TODAY),
            budget._income_claimants(self.conn, series))
        self.assertTrue(all(r["date"].day == 1
                            for r in claims["Acme Payroll"]))
        self.assertTrue(all(r["date"].day == 15
                            for r in claims["Acme Payroll Mid"]))
        st = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(st["income"], 2 * CHECK, places=2)

    def test_an_excluded_twin_account_is_not_averaged_or_sized_twice(self):
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('chk2','it1','Twin Checking',"
            "'depository','checking',100)")
        d = dt.date(2025, 6, 6)
        while d <= TODAY:
            _pay(self.conn, d)
            _pay(self.conn, d, account="chk2")
            d += dt.timedelta(days=14)
        add_bill(self.conn, "Northwind Payroll", CHECK, frequency="WEEKLY",
                 interval=2, income=True, next_due=d,
                 last_seen=d - dt.timedelta(days=14))
        write_config(self.conn, budgeted_income_monthly=1000.0,
                     excluded_accounts=["chk2"])
        load = budget.recurring_load(self.conn, TODAY)
        self.assertLess(load["income_monthly"], 2 * CHECK * 1.2)
        self.assertGreater(load["income_monthly"], 2 * CHECK * 0.8)
        # Jul 17 and Jul 31 still due, each one check — not the same-day
        # pair merged into a double one
        cfg = budget.load_config(self.conn)
        self.assertAlmostEqual(budget.expected_income_remaining(
            self.conn, cfg, TODAY), 2 * CHECK, places=2)


class LongCadenceStandsForItsCycles(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def test_a_quarter_end_series_in_its_first_year_reads_one_check_a_quarter(self):
        for d in (dt.date(2026, 3, 31), dt.date(2026, 6, 30)):
            _pay(self.conn, d, 3 * SIDE, name="CONTOSO DIVIDEND")
        add_bill(self.conn, "Contoso Dividend", 3 * SIDE, frequency="MONTHLY",
                 interval=3, income=True, next_due=dt.date(2026, 9, 30),
                 last_seen=dt.date(2026, 6, 30))
        load = budget.recurring_load(self.conn, TODAY)
        self.assertEqual(load["income_basis"], "actual")
        self.assertAlmostEqual(load["income_monthly"], SIDE, places=2)

    def test_a_yearly_bonus_that_came_weeks_early_reads_a_years_income(self):
        for d in (dt.date(2024, 7, 19), dt.date(2025, 7, 18),
                  dt.date(2026, 6, 26)):
            _pay(self.conn, d, 12 * SIDE, name="NORTHWIND BONUS")
        add_bill(self.conn, "Northwind Bonus", 12 * SIDE, frequency="YEARLY",
                 income=True, next_due=dt.date(2027, 6, 26),
                 last_seen=dt.date(2026, 6, 26))
        load = budget.recurring_load(self.conn, TODAY)
        self.assertAlmostEqual(load["income_monthly"], SIDE, places=2)


class AverageCountsOccurrencesNotDeposits(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _monthly_on_the_first(self, dates):
        for d in dates:
            _pay(self.conn, d)
        add_bill(self.conn, "Northwind Payroll", CHECK, income=True,
                 next_due=dt.date(2026, 8, 1), last_seen=dt.date(2026, 7, 1))

    def test_an_extra_deposit_from_the_payer_adds_to_the_average(self):
        firsts = list(_months(dt.date(2024, 6, 1), TODAY, 1))
        self._monthly_on_the_first(firsts)
        base = budget.recurring_load(self.conn, TODAY)["income_monthly"]
        self.assertAlmostEqual(base, CHECK, places=2)
        # a reimbursement from the same employer, off the payday
        _pay(self.conn, dt.date(2026, 3, 17), SIDE)
        load = budget.recurring_load(self.conn, TODAY)
        self.assertAlmostEqual(load["income_monthly"], CHECK + SIDE / 12,
                               places=2)

    def test_a_first_and_fifteenth_series_in_one_recurrence_reads_both(self):
        start = dt.date(2025, 1, 1)
        for d in list(_months(start, TODAY, 1)) + list(_months(start, TODAY, 15)):
            _pay(self.conn, d)
        add_bill(self.conn, "Northwind Payroll", CHECK, income=True,
                 next_due=dt.date(2026, 7, 15), last_seen=dt.date(2026, 7, 1))
        self.conn.execute(
            "UPDATE bills SET raw = jsonb_set(raw, '{recurrence,byMonthDay}',"
            " '[1, 15]')")
        load = budget.recurring_load(self.conn, TODAY)
        self.assertEqual(load["income_basis"], "actual")
        self.assertAlmostEqual(load["income_monthly"], 2 * CHECK, places=2)

    def test_a_check_prepaid_before_the_window_still_pays_its_month(self):
        # 2025-06-01 was a Sunday: that check landed Friday May 30, before
        # the window (Jun 2025 .. May 2026) seen from mid-June 2026
        day = dt.date(2026, 6, 10)
        dates = [d if d.weekday() < 5 else
                 d - dt.timedelta(days=d.weekday() - 4)
                 for d in _months(dt.date(2024, 6, 1), day, 1)]
        self._monthly_on_the_first(dates)
        for today in (dt.date(2026, 5, 10), day, dt.date(2026, 7, 10)):
            load = budget.recurring_load(self.conn, today)
            self.assertAlmostEqual(load["income_monthly"], CHECK, places=2,
                                   msg=str(today))


class ChecksAtTheSpansEdges(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, budgeted_income_monthly=1000.0)

    def test_a_check_paid_late_for_the_spans_last_month_still_counts(self):
        # a pension due the 30th (the 28th in February) that always lands
        # two days later — June's lands Jul 2, after the window's last month
        for d in _months(dt.date(2024, 6, 1), dt.date(2026, 7, 1), 1):
            due = d.replace(day=28 if d.month == 2 else 30)
            _pay(self.conn, due + dt.timedelta(days=2),
                 name="CONTOSO PENSION")
        add_bill(self.conn, "Contoso Pension", CHECK, income=True,
                 next_due=dt.date(2026, 7, 30),
                 last_seen=dt.date(2026, 6, 30))
        load = budget.recurring_load(self.conn, TODAY)
        self.assertEqual(load["income_basis"], "actual")
        self.assertAlmostEqual(load["income_monthly"], CHECK, places=2)

    def test_a_paycheck_split_into_two_deposits_follows_its_month(self):
        # Nov 1 2026 is a Sunday; the check for it lands Friday Oct 30 in
        # two parts
        for d in (dt.date(2026, 9, 1), dt.date(2026, 10, 1)):
            _pay(self.conn, d)
        _pay(self.conn, dt.date(2026, 10, 30), CHECK - SIDE)
        _pay(self.conn, dt.date(2026, 10, 30), SIDE)
        add_bill(self.conn, "Northwind Payroll", CHECK, income=True,
                 next_due=dt.date(2026, 12, 1),
                 last_seen=dt.date(2026, 10, 30))
        nov = budget.month_status(self.conn, dt.date(2026, 11, 3))
        self.assertAlmostEqual(nov["income_received"], CHECK, places=2)
        octo = budget.month_status(self.conn, dt.date(2026, 10, 31),
                                   historical=True, closed=True)
        self.assertAlmostEqual(octo["income"], CHECK, places=2)


class ClosedMonthClaimsAreFrozen(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, food_monthly=600)

    def test_a_series_added_later_does_not_move_a_closed_months_average(self):
        d = dt.date(2025, 6, 6)
        while d < dt.date(2026, 3, 31):
            _pay(self.conn, d, name="ACME PAYROLL")
            d += dt.timedelta(days=14)
        # the frozen series' configured check is off the deposits' size, so
        # a live series at the exact size would win every claim
        add_bill(self.conn, "Acme Payroll", 1.3 * CHECK, frequency="WEEKLY",
                 interval=2, income=True, next_due=d,
                 last_seen=d - dt.timedelta(days=14))
        march = dt.date(2026, 3, 31)
        self.assertTrue(budget.snapshot_month(self.conn, march))
        frozen = budget.snapshot_bill_rows(self.conn, 2026, 3)
        series = budget._recurring_bills(self.conn, income=True,
                                         rows=frozen)
        before = budget.income_actual_monthly(self.conn, march, series,
                                              bill_rows=frozen)
        # a second series on the same payer, at the deposits' size, added
        # today
        add_bill(self.conn, "Acme Payroll Direct", CHECK, frequency="WEEKLY",
                 interval=2, income=True, next_due=d,
                 last_seen=d - dt.timedelta(days=14), merchant="ACME PAYROLL")
        after = budget.income_actual_monthly(self.conn, march, series,
                                             bill_rows=frozen)
        self.assertEqual(before["basis"], "actual")
        self.assertEqual(before, after)
        # the live claim really would have moved it
        live = budget.income_actual_monthly(self.conn, march, series)
        self.assertNotEqual(live["monthly"], before["monthly"])


class PendingToPostedMidRequest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, budgeted_income_monthly=1000.0)

    def test_a_paycheck_posting_mid_request_is_counted_once(self):
        """The forecast reads the day's deposits (the pending paycheck)
        before the month plan does; a sync that posts it in between must
        not leave the plan counting the pending row and the posted one."""
        _pay(self.conn, TODAY - dt.timedelta(days=14))
        add_bill(self.conn, "Northwind Payroll", CHECK, frequency="WEEKLY",
                 interval=2, income=True, next_due=TODAY,
                 last_seen=TODAY - dt.timedelta(days=14))
        add_txn(self.conn, TODAY, -CHECK, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk", pending=1, txn_id="pend1")
        memo: dict = {}
        cfg = budget.load_config(self.conn, memo=memo)
        budget.upcoming_bill_occurrences(self.conn, cfg, TODAY,
                                         TODAY + dt.timedelta(days=10),
                                         income=True, memo=memo)
        self.conn.execute("UPDATE transactions SET removed = 1"
                          " WHERE id = 'pend1'")
        add_txn(self.conn, TODAY, -CHECK, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk", txn_id="post1")
        st = budget.month_status(self.conn, TODAY, memo=memo)
        self.assertAlmostEqual(st["income_received"], 2 * CHECK, places=2)
        # Jul 29 still due
        self.assertAlmostEqual(st["income_expected"], CHECK, places=2)


class DepositSetReadOncePerRequest(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn, budgeted_income_monthly=1000.0,
                     dynamic_variable_budget=True)

    def test_one_request_reads_the_deposit_set_once(self):
        d = dt.date(2025, 6, 6)
        while d <= TODAY:
            _pay(self.conn, d)
            d += dt.timedelta(days=14)
        add_bill(self.conn, "Northwind Payroll", CHECK, frequency="WEEKLY",
                 interval=2, income=True, next_due=d,
                 last_seen=d - dt.timedelta(days=14))
        self.conn.commit()
        memo: dict = {}
        with mock.patch.object(bills, "_income_rows_query",
                               wraps=bills._income_rows_query) as q:
            st = budget.month_status(self.conn, TODAY, memo=memo)
            budget.month_status(self.conn, TODAY, memo=memo)
            forecast.build(self.conn, TODAY, memo=memo)
            forecast.standing_surplus(self.conn, st, TODAY, memo=memo)
            budget.recurring_load(self.conn, TODAY)
            budget.expected_income_remaining(
                self.conn, budget.load_config(self.conn, memo=memo), TODAY,
                memo=memo)
        self.assertEqual(q.call_count, 1)


if __name__ == "__main__":
    unittest.main()
