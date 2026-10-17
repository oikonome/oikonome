"""Paychecks in the cash forecast.

- A paycheck due TODAY that has not posted yet is still coming: the walk
  and next_paycheck must keep it (on tomorrow, the walk's first day, just
  as a bill due today is reserved), or the runway is short by a whole
  paycheck while today's bills stay reserved. An earlier paycheck that
  never arrived stays dropped.
- The active income scenario's take-home is ONE paycheck at the scenario's
  cadence. It caps the series it describes and nothing else: a monthly
  pension must not walk in at a biweekly paycheck's size.
"""
import datetime as dt
import unittest

from oikonome.engine import budget, forecast

from .util import TODAY, add_bill, add_txn, make_db, write_config


class PaycheckDueTodayIsStillComing(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Acme Payroll", 2000.0, frequency="WEEKLY",
                 interval=2, income=True, next_due=TODAY,
                 last_seen=TODAY - dt.timedelta(days=14))
        self.conn.commit()
        self.cfg = budget.load_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _upcoming(self):
        return [(o["payee"], o["due"]) for o in
                budget.upcoming_bill_occurrences(
                    self.conn, self.cfg, TODAY,
                    TODAY + dt.timedelta(days=10), income=True)]

    def test_an_unposted_paycheck_due_today_stays_in_the_window(self):
        self.assertEqual(self._upcoming(),
                         [("Acme Payroll", TODAY + dt.timedelta(days=1))])
        self.assertEqual(forecast.next_paycheck(self.conn, TODAY),
                         TODAY + dt.timedelta(days=1))

    def test_a_paycheck_that_posted_today_is_not_walked_in_again(self):
        add_txn(self.conn, TODAY, -2000.0, "ACME PAYROLL", primary="INCOME",
                account="chk")
        self.conn.commit()
        self.assertEqual(self._upcoming(), [])

    def test_a_paycheck_pending_today_is_not_walked_in_again(self):
        """The feed shows today's paycheck as a pending deposit, which the
        available balance the forecast starts from already carries:
        re-dating it to tomorrow would count the same deposit twice."""
        add_txn(self.conn, TODAY, -2000.0, "ACME PAYROLL", primary="INCOME",
                account="chk", pending=1)
        self.conn.commit()
        self.assertEqual(self._upcoming(), [])
        self.assertEqual(forecast.next_paycheck(self.conn, TODAY),
                         TODAY + dt.timedelta(days=14))
        fc = forecast.build(self.conn, today=TODAY, days=10)
        self.assertEqual([e for e in fc["events"] if e[2] == "Acme Payroll"],
                         [])

    def test_a_pending_paycheck_still_lands_when_the_bank_reports_no_available(self):
        """With no available balance the walk starts from the current
        balance, which a pending deposit is not in: dropping the re-date
        would hide the paycheck for the day rather than avoid counting it
        twice."""
        self.conn.execute(
            "UPDATE accounts SET balance_available = NULL WHERE id = 'chk'")
        add_txn(self.conn, TODAY, -2000.0, "ACME PAYROLL", primary="INCOME",
                account="chk", pending=1)
        self.conn.commit()
        self.assertEqual(self._upcoming(),
                         [("Acme Payroll", TODAY + dt.timedelta(days=1))])

    def test_a_series_already_moved_past_today_adds_no_paycheck_today(self):
        """When the series' own next payday is already two weeks out,
        today's occurrence is a back-projection of a deposit the series
        has recorded, not a paycheck still coming."""
        self.conn.execute("DELETE FROM bills")
        add_bill(self.conn, "Acme Payroll", 2000.0, frequency="WEEKLY",
                 interval=2, income=True,
                 next_due=TODAY + dt.timedelta(days=14), last_seen=TODAY)
        self.conn.commit()
        self.assertEqual(self._upcoming(), [])

    def test_an_earlier_missed_paycheck_stays_dropped(self):
        """The occurrence two weeks back never posted: a late paycheck is
        not owed the way an overdue bill is."""
        got = budget.upcoming_bill_occurrences(
            self.conn, self.cfg, TODAY + dt.timedelta(days=1),
            TODAY + dt.timedelta(days=10), income=True)
        self.assertEqual(got, [])


class ScenarioCapsOnlyItsOwnPaycheck(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn,
                     income_scenarios=[{"name": "job", "take_home": 1800,
                                        "cadence": "biweekly"}],
                     active_scenario="job")
        add_bill(self.conn, "Acme Payroll", 2000.0, frequency="WEEKLY",
                 interval=2, income=True,
                 next_due=TODAY + dt.timedelta(days=3),
                 last_seen=TODAY - dt.timedelta(days=11))
        add_bill(self.conn, "State Pension", 3000.0, income=True,
                 next_due=dt.date(2026, 7, 20),
                 last_seen=dt.date(2026, 6, 20))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _amounts(self):
        fc = forecast.build(self.conn, today=TODAY, days=30)
        out: dict[str, set] = {}
        for _d, amt, payee in fc["events"]:
            if amt > 0:
                out.setdefault(payee, set()).add(amt)
        return out

    def test_a_monthly_pension_walks_in_at_its_own_amount(self):
        self.assertEqual(self._amounts()["State Pension"], {3000.0})

    def test_the_scenarios_own_series_is_still_capped(self):
        self.assertEqual(self._amounts()["Acme Payroll"], {1800.0})

    def test_a_scenario_with_no_series_at_its_cadence_caps_nothing(self):
        write_config(self.conn,
                     income_scenarios=[{"name": "gig", "take_home": 500,
                                        "cadence": "weekly"}],
                     active_scenario="gig")
        got = self._amounts()
        self.assertEqual(got["State Pension"], {3000.0})
        self.assertEqual(got["Acme Payroll"], {2000.0})


class SemimonthlyScenarioCapsItsPaydays(unittest.TestCase):
    """A 1st-and-15th payroll is stored as two monthly series of one payer;
    a semimonthly scenario must still reach them, one take-home per
    deposit, without touching a lone monthly pension."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn,
                     income_scenarios=[{"name": "job", "take_home": 1700,
                                        "cadence": "semimonthly"}],
                     active_scenario="job")
        add_bill(self.conn, "Acme Payroll", 2000.0, income=True,
                 next_due=dt.date(2026, 8, 1), last_seen=dt.date(2026, 7, 1))
        add_bill(self.conn, "Acme Payroll Mid", 2000.0, income=True,
                 next_due=dt.date(2026, 7, 15) + dt.timedelta(days=31),
                 last_seen=dt.date(2026, 7, 15))
        add_bill(self.conn, "State Pension", 3000.0, income=True,
                 next_due=dt.date(2026, 7, 20),
                 last_seen=dt.date(2026, 6, 20))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _amounts(self):
        fc = forecast.build(self.conn, today=TODAY, days=45)
        out: dict[str, set] = {}
        for _d, amt, payee in fc["events"]:
            if amt > 0:
                out.setdefault(payee, set()).add(amt)
        return out

    def test_each_payday_is_capped_at_one_take_home(self):
        got = self._amounts()
        self.assertEqual(got["Acme Payroll"], {1700.0})
        self.assertEqual(got["Acme Payroll Mid"], {1700.0})
        self.assertEqual(got["State Pension"], {3000.0})

    def test_a_biweekly_scenario_still_caps_only_its_biweekly_series(self):
        add_bill(self.conn, "Beta Wages", 2000.0, frequency="WEEKLY",
                 interval=2, income=True,
                 next_due=TODAY + dt.timedelta(days=3),
                 last_seen=TODAY - dt.timedelta(days=11))
        write_config(self.conn,
                     income_scenarios=[{"name": "job", "take_home": 1700,
                                        "cadence": "biweekly"}],
                     active_scenario="job")
        self.conn.commit()
        got = self._amounts()
        self.assertEqual(got["Beta Wages"], {1700.0})
        self.assertEqual(got["Acme Payroll"], {2000.0})
        self.assertEqual(got["Acme Payroll Mid"], {2000.0})


class SemimonthlyPayerIsTheMerchantNotTheFirstWord(unittest.TestCase):
    """Two payers whose names share a first word are still two payers:
    a pension and a payroll that both start "State" must not be read as
    one employer's two paydays, or the pension is capped at a payroll
    take-home."""

    def setUp(self):
        from oikonome.engine import merchant_identity
        self.conn = make_db()
        write_config(self.conn,
                     income_scenarios=[{"name": "job", "take_home": 1700,
                                        "cadence": "semimonthly"}],
                     active_scenario="job")
        for name in ("State Pension", "State Example Co", "Acme Corp"):
            add_txn(self.conn, dt.date(2026, 1, 5), -10.0, name.upper(),
                    primary="INCOME", account="chk", merchant=name)
        self.conn.commit()
        merchant_identity.resolve(self.conn)
        add_bill(self.conn, "State Pension", 3000.0, income=True,
                 next_due=dt.date(2026, 7, 20),
                 last_seen=dt.date(2026, 6, 20), merchants=["State Pension"])
        add_bill(self.conn, "State Example Co Payroll", 2500.0, income=True,
                 next_due=dt.date(2026, 7, 25),
                 last_seen=dt.date(2026, 6, 25), merchants=["State Example Co"])
        add_bill(self.conn, "Acme Payroll", 2000.0, income=True,
                 next_due=dt.date(2026, 8, 1), last_seen=dt.date(2026, 7, 1),
                 merchants=["Acme Corp"])
        add_bill(self.conn, "Acme Payroll Mid", 2000.0, income=True,
                 next_due=dt.date(2026, 8, 15),
                 last_seen=dt.date(2026, 7, 15), merchants=["Acme Corp"])
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_the_pension_is_uncapped_and_the_real_paydays_are_capped(self):
        fc = forecast.build(self.conn, today=TODAY, days=45)
        got: dict[str, set] = {}
        for _d, amt, payee in fc["events"]:
            if amt > 0:
                got.setdefault(payee, set()).add(amt)
        self.assertEqual(got["State Pension"], {3000.0})
        self.assertEqual(got["State Example Co Payroll"], {2500.0})
        self.assertEqual(got["Acme Payroll"], {1700.0})
        self.assertEqual(got["Acme Payroll Mid"], {1700.0})


if __name__ == "__main__":
    unittest.main()
