"""Income is read from what actually landed, not from the current check.

A paycheck changes through the year — withholding reaches a cap, a raise
lands — so the current check annualised is not what a year of it brings
in. The Bills headline now averages each income series' deposits over the
last twelve complete months (fewer, down to three, while the ledger is
young; the schedule's rate before that). The month plan counts THIS
month's income: the deposits that landed plus the checks still due before
month end, so a three-paycheck month shows its third check; a closed month
is what landed; a household with no income series keeps its budgeted
figure, because a half-landed month would otherwise squeeze every
variable budget to nothing.
"""

import datetime as dt
import unittest

from oikonome.engine import budget, reporting

from .util import TODAY, add_bill, add_txn, make_db, write_config


def _months_back(d: dt.date, n: int) -> dt.date:
    y, m = d.year, d.month - n
    while m <= 0:
        y, m = y - 1, m + 12
    return d.replace(year=y, month=m, day=1)


class IncomeIsWhatActuallyLanded(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, budgeted_income_monthly=5000.0)

    def tearDown(self):
        self.conn.close()

    def _deposits(self, since: dt.date, until: dt.date, amount_of):
        """A biweekly deposit every 14 days from `since` to `until`
        (exclusive), the amount decided per date."""
        d, out = since, []
        while d < until:
            add_txn(self.conn, d, -amount_of(d), "NORTHWIND PAYROLL",
                    primary="INCOME", account="chk")
            out.append((d, amount_of(d)))
            d += dt.timedelta(days=14)
        return out

    def _series(self, amount=3200.0, next_due=None):
        add_bill(self.conn, "Northwind Payroll", amount, frequency="WEEKLY",
                 interval=2, income=True,
                 next_due=next_due or TODAY + dt.timedelta(days=2),
                 last_seen=TODAY - dt.timedelta(days=12))

    def test_the_headline_averages_twelve_months_of_deposits(self):
        start = _months_back(TODAY, 14)
        month_start = TODAY.replace(day=1)
        # smaller checks until three months ago, then the bigger current one
        bump = _months_back(TODAY, 3)
        paid = self._deposits(start, TODAY,
                              lambda d: 3000.0 if d < bump else 3200.0)
        self._series(3200.0)
        load = budget.recurring_load(self.conn, TODAY)
        window = _months_back(TODAY, 12)
        expect = sum(a for d, a in paid if window <= d < month_start) / 12
        self.assertAlmostEqual(load["income_monthly"], expect, places=2)
        self.assertEqual(load["income_basis"], "actual")
        self.assertEqual(load["income_months"], 12)
        # and it is NOT the current check annualised
        self.assertNotAlmostEqual(load["income_monthly"],
                                  3200 * 365.2425 / 14 / 12, places=0)

    def test_a_young_series_averages_the_months_it_has(self):
        start = _months_back(TODAY, 4)
        month_start = TODAY.replace(day=1)
        paid = self._deposits(start, TODAY, lambda d: 3200.0)
        self._series(3200.0)
        load = budget.recurring_load(self.conn, TODAY)
        expect = sum(a for d, a in paid if d < month_start) / 4
        self.assertAlmostEqual(load["income_monthly"], expect, places=2)
        self.assertEqual(load["income_months"], 4)

    def test_a_series_with_no_history_keeps_its_scheduled_rate(self):
        self._series(3200.0)
        load = budget.recurring_load(self.conn, TODAY)
        self.assertAlmostEqual(load["income_monthly"],
                               3200 * 365.2425 / 14 / 12, delta=0.01)
        self.assertEqual(load["income_basis"], "schedule")

    def test_the_month_plan_counts_this_months_checks(self):
        # one check landed on the 3rd; the series pays again on the 17th
        # and the 31st — both still due this month
        add_txn(self.conn, TODAY.replace(day=3), -3200.0, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk")
        self._series(3200.0, next_due=TODAY.replace(day=17))
        st = budget.month_status(self.conn, TODAY)
        flows = reporting.month_flows(self.conn, TODAY.year, TODAY.month)
        self.assertEqual(st["income_received"], flows["in"])
        self.assertAlmostEqual(st["income_received"], 3200.0, places=2)
        self.assertAlmostEqual(st["income_expected"], 6400.0, places=2)
        self.assertAlmostEqual(st["income"], 9600.0, places=2)
        # a closed month is what landed in it, nothing expected
        last = TODAY.replace(day=1) - dt.timedelta(days=1)
        add_txn(self.conn, last.replace(day=5), -3000.0, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk")
        closed = budget.month_status(self.conn, last, historical=True,
                                     closed=True)
        self.assertAlmostEqual(closed["income"], 3000.0, places=2)
        self.assertEqual(closed["income_expected"], 0.0)

    def test_the_checks_still_due_are_sized_by_the_latest_deposit(self):
        # the series is configured at 3000 but the last check was 3200 —
        # the two still due this month are expected at 3200 each
        add_txn(self.conn, TODAY.replace(day=3), -3200.0, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk")
        self._series(3000.0, next_due=TODAY.replace(day=17))
        st = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(st["income_expected"], 6400.0, places=2)
        self.assertAlmostEqual(st["income"], 9600.0, places=2)
        # a deposit far outside the check's size (a bonus) is not the check
        add_txn(self.conn, TODAY.replace(day=10), -25000.0, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk")
        st = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(st["income_expected"], 6000.0, places=2)

    def test_the_planner_suggests_the_same_average_the_bills_page_shows(self):
        start = _months_back(TODAY, 14)
        bump = _months_back(TODAY, 3)
        self._deposits(start, TODAY, lambda d: 3000.0 if d < bump else 3200.0)
        self._series(3200.0)
        sug = budget.suggest_budgets(self.conn, TODAY)["suggestions"]
        load = budget.recurring_load(self.conn, TODAY)
        self.assertAlmostEqual(sug["income_monthly"], load["income_monthly"],
                               places=2)

    def test_without_an_income_series_the_budgeted_figure_stands(self):
        add_txn(self.conn, TODAY.replace(day=3), -3200.0, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk")
        st = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(st["income"], 5000.0, places=2)
        self.assertIsNone(st["income_received"])
        self.assertIsNone(st["income_expected"])

    # ---- one deposit, counted once ------------------------------------

    def test_a_pending_paycheck_before_its_payday_is_counted_once(self):
        """A paycheck the bank already shows as PENDING two days before its
        due date is in the ledger's income for the month (month_flows reads
        pending rows), so it must also satisfy its occurrence in the
        still-due half. Otherwise the month's income carries the same check
        twice — once landed, once still due — until the row posts, and the
        variable budgets and the excess line read a check that does not
        exist."""
        add_txn(self.conn, TODAY.replace(day=3), -3200.0, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk")
        self._series(3200.0, next_due=TODAY.replace(day=17))
        add_txn(self.conn, TODAY, -3200.0, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk", pending=1)
        st = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(st["income_received"], 6400.0, places=2)
        # only the 31st is still due — the 17th is the pending deposit
        self.assertAlmostEqual(st["income_expected"], 3200.0, places=2)
        self.assertAlmostEqual(st["income"], 9600.0, places=2)
        self.assertAlmostEqual(
            st["income"], st["income_received"] + st["income_expected"],
            places=2)
        # the ledger header's "expected" is the same figure
        self.assertAlmostEqual(budget.expected_income_remaining(
            self.conn, budget.load_config(self.conn), TODAY), 3200.0, places=2)

    def test_a_pending_payday_check_is_counted_once_without_an_available_balance(self):
        """Payday at a bank that reports no available balance: the forecast
        re-dates the unposted occurrence to tomorrow (the walk starts from
        the current balance, which a pending credit is not in), but the
        month's income already holds the pending row as landed — the
        still-due half must not count it again."""
        self.conn.execute(
            "UPDATE accounts SET balance_available = NULL WHERE id = 'chk'")
        add_txn(self.conn, TODAY.replace(day=1), -3200.0, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk")
        self._series(3200.0, next_due=TODAY)
        add_txn(self.conn, TODAY, -3200.0, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk", pending=1)
        st = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(st["income_received"], 6400.0, places=2)
        self.assertAlmostEqual(st["income_expected"], 3200.0, places=2)
        self.assertAlmostEqual(st["income"], 9600.0, places=2)

    def test_a_pending_check_dated_last_month_does_not_settle_this_months_payday(self):
        """A paycheck due on the 1st that the feed shows pending on the last
        day of the previous month sits in LAST month's landed income. It must
        not also settle this month's first payday, or the new month reads a
        whole paycheck short until the row posts."""
        first = TODAY.replace(day=1)
        for back in (28, 14):
            add_txn(self.conn, first - dt.timedelta(days=back), -3200.0,
                    "NORTHWIND PAYROLL", primary="INCOME", account="chk")
        add_bill(self.conn, "Northwind Payroll", 3200.0, frequency="WEEKLY",
                 interval=2, income=True, next_due=first,
                 last_seen=first - dt.timedelta(days=14))
        before = budget.month_status(self.conn, first)["income"]
        add_txn(self.conn, first - dt.timedelta(days=1), -3200.0,
                "NORTHWIND PAYROLL", primary="INCOME", account="chk", pending=1)
        after = budget.month_status(self.conn, first)
        self.assertAlmostEqual(after["income"], before, places=2)
        self.assertAlmostEqual(
            after["income"], after["income_received"] + after["income_expected"],
            places=2)

    def test_two_series_sharing_a_lead_word_each_count_their_own_deposits(self):
        """Every deposit belongs to ONE income series. Two series whose names
        share their first word (a pension and a payroll from the same
        state) must not each sum the other's deposits: the Bills headline
        and the planner's suggestion would carry both incomes twice, and a
        pension's "latest check" could be a payroll deposit."""
        start = _months_back(TODAY, 14)
        d = start
        while d < TODAY:
            add_txn(self.conn, d.replace(day=1), -2000.0, "STATE PENSION",
                    primary="INCOME", account="chk")
            add_txn(self.conn, d.replace(day=15), -3000.0,
                    "STATE EXAMPLE CO PAYROLL", primary="INCOME",
                    account="chk")
            d = (d + dt.timedelta(days=32)).replace(day=1)
        add_bill(self.conn, "State Pension", 2000.0, income=True,
                 next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        add_bill(self.conn, "State Example Co Payroll", 3000.0, income=True,
                 next_due=dt.date(2026, 8, 15),
                 last_seen=dt.date(2026, 7, 15))
        load = budget.recurring_load(self.conn, TODAY)
        self.assertEqual(load["income_basis"], "actual")
        self.assertAlmostEqual(load["income_monthly"], 5000.0, places=2)
        sug = budget.suggest_budgets(self.conn, TODAY)["suggestions"]
        self.assertAlmostEqual(sug["income_monthly"], 5000.0, places=2)
        series = {b["payee"]: b for b in budget._recurring_bills(
            self.conn, income=True)}
        self.assertEqual(budget.latest_deposit_amount(
            self.conn, TODAY, series["State Pension"]), 2000.0)
        self.assertEqual(budget.latest_deposit_amount(
            self.conn, TODAY, series["State Example Co Payroll"]), 3000.0)

    def test_the_forecast_sizes_each_series_from_its_own_deposits(self):
        """The forecast walks each paycheck in at the median of its own
        recent deposits. A series must not take a sibling's deposits as its
        own because their names share a first word — a $2,000 pension sized
        from a $3,000 payroll walks $1,000 of money in that never comes."""
        from oikonome.engine import forecast
        d = _months_back(TODAY, 5)
        while d < TODAY:
            add_txn(self.conn, d.replace(day=1), -2000.0, "STATE PENSION",
                    primary="INCOME", account="chk")
            add_txn(self.conn, d.replace(day=15), -3000.0,
                    "STATE EXAMPLE CO PAYROLL", primary="INCOME",
                    account="chk")
            d = (d + dt.timedelta(days=32)).replace(day=1)
        add_bill(self.conn, "State Pension", 2000.0, income=True,
                 next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        add_bill(self.conn, "State Example Co Payroll", 3000.0, income=True,
                 next_due=dt.date(2026, 8, 15),
                 last_seen=dt.date(2026, 7, 15))
        fc = forecast.build(self.conn, today=TODAY, days=40)
        walked = {p: a for _, a, p in fc["events"] if a > 0}
        self.assertAlmostEqual(walked["State Pension"], 2000.0, places=2)
        self.assertAlmostEqual(walked["State Example Co Payroll"], 3000.0,
                               places=2)

    def test_bill_health_reads_each_deposit_once(self):
        """The Bills page's health pills read each income series from the
        deposits it OWNS, the same one-owner claim the headline uses: two
        series whose rules both accept a deposit must not each count it,
        or a pension's last check reads as the payroll deposit."""
        from oikonome.engine import bills
        d = _months_back(TODAY, 5)
        while d < TODAY:
            add_txn(self.conn, d.replace(day=1), -2000.0, "STATE PENSION",
                    primary="INCOME", account="chk")
            add_txn(self.conn, d.replace(day=15), -3000.0,
                    "STATE EXAMPLE CO PAYROLL", primary="INCOME",
                    account="chk")
            d = (d + dt.timedelta(days=32)).replace(day=1)
        # both series ride on the shared lead word alone
        add_bill(self.conn, "State Pension", 2000.0, income=True,
                 merchant="state",
                 next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        add_bill(self.conn, "State Example Co Payroll", 3000.0, income=True,
                 merchant="state",
                 next_due=dt.date(2026, 8, 15),
                 last_seen=dt.date(2026, 7, 15))
        health = bills.analyze_bills(self.conn, TODAY)
        pension = health["State Pension"]
        payroll = health["State Example Co Payroll"]
        self.assertEqual(pension["last_seen"]["amount"], 2000.0, pension)
        self.assertEqual(payroll["last_seen"]["amount"], 3000.0, payroll)
        one = bills.analyze_bills(self.conn, TODAY, payee="State Pension")
        self.assertEqual(one["State Pension"]["last_seen"]["amount"], 2000.0)
        self.assertEqual(pension["matched"] + payroll["matched"],
                         self.conn.execute(
                             "SELECT count(*) AS n FROM transactions "
                             "WHERE amount < 0").fetchone()["n"])

    # ---- closed and future months ---------------------------------------

    def test_a_closed_months_income_reads_the_schedule_it_was_judged_under(self):
        """A closed month judged on its frozen schedule decides whether the
        household tracked an income series from THAT schedule, not today's
        bills table. Archiving a series today must not flip a past month
        from what landed back to the budgeted figure (or the other way
        round), rewriting a verdict the month already earned."""
        last = TODAY.replace(day=1) - dt.timedelta(days=1)
        add_txn(self.conn, last.replace(day=5), -3000.0, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk")
        frozen = [{"payee": "Northwind Payroll", "amount": 3200.0,
                   "merchant": None, "category": None,
                   "raw": {"bill_type": "occurrence", "dueOn": "2026-06-19",
                           "lastDueOn": "2026-06-05",
                           "recurrence": {"frequency": "WEEKLY",
                                          "interval": 2}}}]
        # today's table has no income series at all (archived since)
        st = budget.month_status(self.conn, last, closed=True,
                                 bill_rows=frozen)
        self.assertAlmostEqual(st["income"], 3000.0, places=2)
        self.assertAlmostEqual(st["income_received"], 3000.0, places=2)
        # ...and a frozen schedule with no income series keeps the
        # budgeted figure even though today's table has one
        self._series(3200.0)
        st = budget.month_status(self.conn, last, closed=True, bill_rows=[])
        self.assertAlmostEqual(st["income"], 5000.0, places=2)
        self.assertIsNone(st["income_received"])

    def test_a_future_month_plans_its_own_scheduled_checks(self):
        """A month that has not started (the Month lens reads it as of its
        1st) has nothing landed; its income is the checks scheduled in it,
        each at the latest deposit — the same rule the current month uses
        for what is still due."""
        from oikonome.web import lenses
        add_txn(self.conn, TODAY.replace(day=3), -3200.0, "NORTHWIND PAYROLL",
                primary="INCOME", account="chk")
        # biweekly from the 17th: September pays on the 11th and the 25th
        self._series(3000.0, next_due=TODAY.replace(day=17))
        st = lenses.month_state(self.conn, 2026, 9, TODAY)
        self.assertAlmostEqual(st["income_received"] or 0.0, 0.0, places=2)
        self.assertAlmostEqual(st["income_expected"], 6400.0, places=2)
        self.assertAlmostEqual(st["income"], 6400.0, places=2)


if __name__ == "__main__":
    unittest.main()
