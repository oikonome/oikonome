"""A series paid on two fixed days of each month (the 15th and the last day,
the 1st and the 15th) is 24 payments a year on calendar days. Read as "every
2 weeks" it schedules 26 payments on days that slide through the month, so a
month's income and the forecast swing by a whole paycheck and the walk holds
checks that never come. Detection stores it as one MONTHLY rule with both
days in byMonthDay, and every door that rewrites a bill keeps that rule
unless it was asked to change it.
"""
import calendar
import datetime as dt
import unittest

from oikonome.engine import bills as rd
from oikonome.engine import budget, forecast
from oikonome.engine.compat import as_dict

from .util import add_bill, add_txn, make_db, write_config


def _last(y, m):
    return dt.date(y, m, calendar.monthrange(y, m)[1])


def _months(start, n):
    y, m = start
    for _ in range(n):
        yield y, m
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)


def fifteenth_and_last(start=(2026, 1), n=10):
    out = []
    for y, m in _months(start, n):
        out += [dt.date(y, m, 15), _last(y, m)]
    return out


def first_and_fifteenth(start=(2026, 1), n=10):
    out = []
    for y, m in _months(start, n):
        out += [dt.date(y, m, 1), dt.date(y, m, 15)]
    return out


def friday_before_a_weekend(d):
    return d - dt.timedelta(days=max(0, d.weekday() - 4))


class TheFitTellsTwoCalendarDaysFromFourteenDays(unittest.TestCase):
    def _shape(self, dates):
        cyc, run = rd._fit_cycle(dates)
        return cyc, run, rd._cycle_to_proposal(cyc, run, 2500.0, [])

    def test_the_fifteenth_and_the_last_day(self):
        cyc, run, ev = self._shape(fifteenth_and_last())
        self.assertEqual(cyc, (15, "MONTHLY", 1))
        self.assertEqual(len(run), 20)
        self.assertEqual(ev["by_month_day"], [15, -1])
        self.assertEqual(ev["next_due"], "2026-11-15")

    def test_the_first_and_the_fifteenth(self):
        cyc, _run, ev = self._shape(first_and_fifteenth())
        self.assertEqual(cyc, (15, "MONTHLY", 1))
        self.assertEqual(ev["by_month_day"], [1, 15])
        self.assertEqual(ev["next_due"], "2026-11-01")

    def test_paydays_moved_to_the_friday_before_a_weekend(self):
        for dates, days in ((fifteenth_and_last(), [15, -1]),
                            (first_and_fifteenth(), [1, 15])):
            shifted = [friday_before_a_weekend(d) for d in dates]
            cyc, _run, ev = self._shape(shifted)
            self.assertEqual(cyc, (15, "MONTHLY", 1), shifted)
            self.assertEqual(ev["by_month_day"], days)

    def test_the_next_due_is_not_the_payday_an_early_deposit_already_paid(self):
        # the 15th falls on a Sunday; the check landed on Friday the 13th
        dates = fifteenth_and_last(start=(2026, 5), n=6) + [dt.date(2026, 11, 13)]
        _cyc, _run, ev = self._shape(dates)
        self.assertEqual(ev["next_due"], "2026-11-30")

    def test_a_true_every_two_weeks_series_stays_every_two_weeks(self):
        for n in (6, 12, 26, 52):
            for start in (dt.date(2026, 1, 2), dt.date(2025, 3, 14)):
                dates = [start + dt.timedelta(days=14 * i) for i in range(n)]
                cyc, _run = rd._fit_cycle(dates)
                self.assertEqual(cyc, (14, "WEEKLY", 2), (start, n))

    def test_an_every_two_weeks_series_with_bank_holiday_delays_stays(self):
        dates = [dt.date(2026, 1, 2) + dt.timedelta(days=14 * i)
                 for i in range(26)]
        dates[5] += dt.timedelta(days=1)
        dates[17] += dt.timedelta(days=2)
        cyc, _run = rd._fit_cycle(dates)
        self.assertEqual(cyc, (14, "WEEKLY", 2))

    def test_too_few_deposits_to_tell_stay_every_two_weeks(self):
        cyc, _run = rd._fit_cycle(fifteenth_and_last(n=2)[:4])
        self.assertEqual(cyc, (14, "WEEKLY", 2))


class CycleAndMonthlyFigures(unittest.TestCase):
    def test_the_cycle_is_half_a_month(self):
        self.assertEqual(budget._cycle_days(
            {"frequency": "MONTHLY", "interval": 1, "byMonthDay": [15, -1]}),
            15)

    def test_a_single_day_or_malformed_list_is_still_a_month(self):
        for days in ([15], [], None, "15", ["x"], [15, "x"], [15, 15],
                     [15, [1]], [15, {"a": 1}], [15, True]):
            self.assertEqual(budget._cycle_days(
                {"frequency": "MONTHLY", "interval": 1, "byMonthDay": days}),
                30, days)

    def test_every_other_month_is_not_read_as_twice_a_month(self):
        self.assertEqual(budget._cycle_days(
            {"frequency": "MONTHLY", "interval": 2, "byMonthDay": [1, 15]}),
            60)


class _PayrollFixture(unittest.TestCase):
    PAYER = "NORTHWIND LOGISTICS PAYROLL"

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def deposit(self, dates, amount=2500.0):
        for d in dates:
            add_txn(self.conn, d, -amount, self.PAYER, primary="INCOME",
                    account="chk")
        self.conn.commit()

    def approve_the_offer(self, today):
        rd.run(self.conn, today=today)
        offers = self.conn.execute(
            "SELECT * FROM bill_proposals WHERE status='pending' "
            "AND kind='add'").fetchall()
        self.assertEqual(len(offers), 1, offers)
        got = rd.apply_proposal(self.conn, offers[0]["id"], "approve")
        self.assertNotIn("error", got)
        self.conn.commit()
        return offers[0]

    def bill(self):
        return self.conn.execute(
            "SELECT * FROM bills WHERE amount > 0").fetchone()

    def rule(self):
        return as_dict(self.bill()["raw"])["recurrence"]


class DetectionToTheMonthsIncome(_PayrollFixture):
    def test_the_offer_approves_into_a_twice_a_month_rule(self):
        self.deposit(fifteenth_and_last())
        offer = self.approve_the_offer(dt.date(2026, 11, 5))
        self.assertEqual((offer["frequency"], offer["interval"]),
                         ("MONTHLY", 1))
        self.assertEqual(self.rule()["byMonthDay"], [15, -1])
        self.assertEqual(self.rule()["frequency"], "MONTHLY")
        self.assertEqual(float(self.bill()["monthly_amount"]), 5000.0)
        self.assertEqual(self.bill()["due_on"], dt.date(2026, 11, 15))

    def test_every_month_of_a_year_reads_two_checks(self):
        dates = fifteenth_and_last(n=10)
        self.deposit(dates)
        self.approve_the_offer(dt.date(2026, 11, 5))
        later = fifteenth_and_last(start=(2026, 11), n=14)
        self.deposit(later)
        cfg = budget.load_config(self.conn)
        for y, m in _months((2026, 11), 13):
            parts = budget._month_income_parts(self.conn, cfg, _last(y, m),
                                               closed=True)
            self.assertEqual(parts["received"], 5000.0, (y, m))

    def test_the_walk_holds_two_checks_a_month_on_their_days(self):
        self.deposit(fifteenth_and_last())
        self.approve_the_offer(dt.date(2026, 11, 5))
        cfg = budget.load_config(self.conn)
        due = [o["due"] for o in budget.upcoming_bill_occurrences(
            self.conn, cfg, dt.date(2026, 11, 5), dt.date(2027, 1, 31),
            income=True)]
        self.assertEqual(due, [dt.date(2026, 11, 15), dt.date(2026, 11, 30),
                               dt.date(2026, 12, 15), dt.date(2026, 12, 31),
                               dt.date(2027, 1, 15), dt.date(2027, 1, 31)])

    def test_the_forecast_reads_it_as_semimonthly(self):
        self.deposit(fifteenth_and_last())
        self.approve_the_offer(dt.date(2026, 11, 5))
        self.assertEqual(forecast._series_cadence(self.rule()), "semimonthly")

    def test_a_later_night_does_not_offer_every_two_weeks_back(self):
        self.deposit(fifteenth_and_last())
        self.approve_the_offer(dt.date(2026, 11, 5))
        self.deposit(fifteenth_and_last(start=(2026, 11), n=3))
        rd.run(self.conn, today=dt.date(2027, 2, 1))
        self.assertEqual(self.conn.execute(
            "SELECT kind, frequency FROM bill_proposals "
            "WHERE status='pending'").fetchall(), [])
        self.assertEqual(self.rule()["byMonthDay"], [15, -1])

    def test_a_pending_offer_seeds_income_at_two_checks(self):
        self.deposit(fifteenth_and_last())
        rd.run(self.conn, today=dt.date(2026, 11, 5))
        got = budget.suggest_budgets(self.conn, dt.date(2026, 11, 5))
        self.assertEqual(got["suggestions"]["income_monthly"], 5000.0)

    def test_a_pending_expense_offer_loads_at_two_payments(self):
        for d in first_and_fifteenth():
            add_txn(self.conn, d, 800.0, "HARBOR MORTGAGE SERVICING",
                    primary="LOAN_PAYMENTS", account="chk")
        self.conn.commit()
        rd.run(self.conn, today=dt.date(2026, 11, 5))
        offer = self.conn.execute(
            "SELECT evidence FROM bill_proposals WHERE status='pending' "
            "AND kind='add'").fetchone()
        self.assertEqual(as_dict(offer["evidence"])["by_month_day"], [1, 15])
        self.assertEqual(budget._pending_expense_monthly(self.conn),
                         (1600.0, 1))


class ABiweeklyBillAlreadyApprovedIsOfferedTheRealShape(_PayrollFixture):
    def setUp(self):
        super().setUp()
        add_bill(self.conn, "Northwind Logistics", 2500.0, frequency="WEEKLY",
                 interval=2, income=True, next_due=dt.date(2026, 11, 14),
                 last_seen=dt.date(2026, 10, 31))
        self.deposit(fifteenth_and_last())

    def _offer(self):
        rd.run(self.conn, today=dt.date(2026, 11, 5))
        return self.conn.execute(
            "SELECT * FROM bill_proposals WHERE status='pending' "
            "AND kind='frequency_change'").fetchone()

    def test_approving_the_change_stores_both_paydays(self):
        offer = self._offer()
        self.assertIsNotNone(offer)
        self.assertEqual((offer["frequency"], offer["interval"]),
                         ("MONTHLY", 1))
        self.assertNotIn("error",
                         rd.apply_proposal(self.conn, offer["id"], "approve"))
        self.assertEqual(self.rule()["byMonthDay"], [15, -1])
        self.assertEqual(float(self.bill()["monthly_amount"]), 5000.0)

    def test_a_true_every_two_weeks_bill_gets_no_offer(self):
        self.conn.execute("DELETE FROM transactions")
        self.conn.execute("DELETE FROM bill_proposals")
        self.deposit([dt.date(2026, 1, 2) + dt.timedelta(days=14 * i)
                      for i in range(22)])
        self.assertIsNone(self._offer())


class AMonthlyRuleThatStoppedBeingTwiceAMonth(_PayrollFixture):
    def test_paid_every_two_weeks_for_six_months_it_is_offered_back(self):
        add_bill(self.conn, "Northwind Logistics", 2500.0, income=True,
                 next_due=dt.date(2026, 11, 15), by_month_day=[15, -1],
                 last_seen=dt.date(2026, 10, 31))
        self.deposit([dt.date(2026, 10, 30) - dt.timedelta(days=14 * i)
                      for i in range(14)])
        rd.run(self.conn, today=dt.date(2026, 11, 5))
        offer = self.conn.execute(
            "SELECT * FROM bill_proposals WHERE status='pending' "
            "AND kind='frequency_change'").fetchone()
        self.assertEqual((offer["frequency"], offer["interval"]),
                         ("WEEKLY", 2))
        rd.apply_proposal(self.conn, offer["id"], "approve")
        self.assertNotIn("byMonthDay", self.rule())


class EveryDoorKeepsTheRule(_PayrollFixture):
    def setUp(self):
        super().setUp()
        add_bill(self.conn, "Northwind Logistics", 2500.0, income=True,
                 next_due=dt.date(2026, 11, 15), by_month_day=[15, -1],
                 last_seen=dt.date(2026, 10, 31))
        self.conn.commit()

    def _save(self, **kw):
        args = dict(payee="Northwind Logistics", amount=2500.0,
                    frequency="MONTHLY", interval=1, income=True,
                    source="detected")
        args.update(kw)
        rd.save_bill(self.conn, **args)

    def _assert_kept(self, amount=2500.0):
        self.assertEqual(self.rule()["byMonthDay"], [15, -1])
        self.assertEqual(float(self.bill()["monthly_amount"]), amount * 2)

    def test_an_amount_only_edit(self):
        self._save(amount=2600.0)
        self._assert_kept(2600.0)

    def test_the_form_saving_the_date_it_shows(self):
        self._save(next_due="2026-11-15")
        self._assert_kept()

    def test_the_form_saving_the_month_end_payday(self):
        self._save(next_due="2026-11-30")
        self._assert_kept()

    def test_the_offer_door_passing_the_rules_first_day(self):
        self._save(by_month_day=15)
        self._assert_kept()

    def test_a_list_of_one_other_day_makes_it_monthly(self):
        self._save(by_month_day=[20], next_due="2026-11-20")
        self.assertEqual(self.rule()["byMonthDay"], [20])
        self.assertEqual(float(self.bill()["monthly_amount"]), 2500.0)

    def test_a_form_date_off_both_paydays_keeps_the_rule(self):
        self._save(next_due="2026-11-20")
        self._assert_kept()
        self.assertEqual(self.bill()["due_on"], dt.date(2026, 11, 30))

    def test_a_changed_cadence_drops_the_days(self):
        self._save(frequency="WEEKLY", interval=2, next_due="2026-11-20")
        self.assertNotIn("byMonthDay", self.rule())
        self.assertAlmostEqual(float(self.bill()["monthly_amount"]),
                               2500.0 * 365.2425 / 14 / 12, places=2)

    def test_a_drifted_price_keeps_the_days_and_the_doubled_figure(self):
        add_bill(self.conn, "Harbor Mortgage", 800.0,
                 next_due=dt.date(2026, 11, 15), by_month_day=[1, 15],
                 last_seen=dt.date(2026, 8, 1))
        for d in first_and_fifteenth(start=(2026, 9), n=2):
            add_txn(self.conn, d, 813.37, "HARBOR MORTGAGE", account="chk",
                    primary="LOAN_PAYMENTS")
        self.conn.commit()
        rd.run(self.conn, today=dt.date(2026, 10, 20))
        row = self.conn.execute("SELECT amount, monthly_amount, raw FROM bills "
                                "WHERE payee='Harbor Mortgage'").fetchone()
        self.assertEqual(abs(float(row["amount"])), 813.37)
        self.assertAlmostEqual(abs(float(row["monthly_amount"])), 1626.74)
        self.assertEqual(as_dict(row["raw"])["recurrence"]["byMonthDay"],
                         [1, 15])

    def test_the_cadence_offer_to_a_single_day_replaces_the_rule(self):
        # one of the rule's own days, offered as THE monthly day: the offer
        # asks for a monthly bill, and the keep-the-rule reading must not win
        self.conn.execute(
            """INSERT INTO bill_proposals
                 (id, kind, bill_type, payee, amount, frequency, interval,
                  next_due, evidence, status, created_at)
               VALUES ('prop:one-day','frequency_change','occurrence',
                  'Northwind Logistics',2500,'MONTHLY',1,'2026-11-15',
                  '{"by_month_day": 15}'::jsonb,'pending',now())""")
        got = rd.apply_proposal(self.conn, "prop:one-day", "approve")
        self.assertNotIn("error", got)
        self.assertEqual(self.rule()["byMonthDay"], [15])
        self.assertEqual(float(self.bill()["monthly_amount"]), 2500.0)


class AHandAuthoredTwoSeriesPayrollStillWorks(_PayrollFixture):
    """Two MONTHLY series of one payer are still a valid hand-entered shape:
    each stays a month long, one payday each."""

    def test_each_series_stays_a_single_day(self):
        add_bill(self.conn, "Northwind Logistics", 2500.0, income=True,
                 next_due=dt.date(2026, 11, 15))
        add_bill(self.conn, "Northwind Logistics Mid", 2500.0, income=True,
                 next_due=dt.date(2026, 11, 30))
        rows = self.conn.execute(
            "SELECT raw, monthly_amount FROM bills WHERE amount > 0").fetchall()
        for r in rows:
            self.assertEqual(budget._cycle_days(
                as_dict(r["raw"])["recurrence"]), 30)
            self.assertEqual(float(r["monthly_amount"]), 2500.0)


if __name__ == "__main__":
    unittest.main()


class TwoDaysThatLandOnOneDateCountOnce(unittest.TestCase):
    """A rule of the 30th and the last day falls on one date in February:
    that is one payment, never two in the paychecks, bills or forecast."""

    def test_a_short_month_folds_the_two_days_into_one(self):
        bill = {"recurrence": {"frequency": "MONTHLY", "interval": 1,
                               "byMonthDay": [30, -1]},
                "anchor": dt.date(2026, 1, 30)}
        self.assertEqual(budget._occurrences_in_month(bill, 2027, 2),
                         [dt.date(2027, 2, 28)])
        self.assertEqual(budget._occurrences_in_month(bill, 2027, 3),
                         [dt.date(2027, 3, 30), dt.date(2027, 3, 31)])
