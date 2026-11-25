"""Irregular deposits are not a pay cadence.

A cadence is a check of about one size on about the same days. A monthly
paycheck whose payer also sends three mid-month bonuses of unrelated sizes
has deposits on two days of the month for three months running, but the
second "payday" is not a paycheck: offering to move the series to twice a
month would schedule a bonus-sized check every month that never comes.
Two regular checks a month still read as twice a month, even when the
two paydays carry different sizes (deductions taken from one check): the
amounts are judged payday by payday, not pooled.

A variable deposit from the same payer beside a steady salary (a monthly
commission) does not hide the salary: it is still proposed. But a run
whose gaps each hold a deposit on the run's own day of the month is every
other deposit of one finer stream of varying sizes, not a paycheck on the
longer cycle.
"""
import datetime as dt
import unittest

from oikonome.engine import bills as rd

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 8)
PAYER = "BRIGHTWATER CONSULTING PAYROLL"


class IrregularDepositsAreNotAPayCadence(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _deposit(self, rows):
        for d, amount in rows:
            add_txn(self.conn, d, -amount, PAYER, primary="INCOME",
                    account="chk")
        self.conn.commit()

    def _monthly_series(self):
        add_bill(self.conn, "Brightwater Consulting Payroll", 2400.0,
                 income=True, next_due=dt.date(2026, 12, 1),
                 last_seen=dt.date(2026, 11, 1))
        self.conn.commit()

    def _offers(self):
        rd.run(self.conn, today=TODAY)
        return self.conn.execute(
            "SELECT kind, frequency, interval, evidence FROM bill_proposals "
            "WHERE status = 'pending'").fetchall()

    def test_three_mid_month_bonuses_are_not_offered_as_twice_a_month(self):
        self._monthly_series()
        self._deposit([(dt.date(2026, m, 1), 2400.0) for m in range(4, 12)])
        # invented bonuses: unrelated sizes, near the middle of the month
        self._deposit([(dt.date(2026, 8, 15), 1500.0),
                       (dt.date(2026, 9, 16), 4000.0),
                       (dt.date(2026, 10, 14), 1800.0)])
        self.assertEqual([dict(o) for o in self._offers()], [])

    def test_three_irregular_deposits_are_offered_as_no_series(self):
        self._deposit([(dt.date(2026, 8, 9), 1500.0),
                       (dt.date(2026, 9, 17), 4000.0),
                       (dt.date(2026, 10, 22), 900.0)])
        self.assertEqual([dict(o) for o in self._offers()], [])

    def test_a_monthly_series_paid_twice_a_month_is_still_offered(self):
        self._monthly_series()
        self._deposit([(dt.date(2026, m, 1), 2400.0) for m in range(4, 8)])
        self._deposit([(dt.date(2026, m, d), 2400.0)
                       for m in (8, 9, 10) for d in (1, 15)]
                      + [(dt.date(2026, 11, 1), 2400.0)])
        offers = self._offers()
        self.assertEqual(len(offers), 1, offers)
        self.assertEqual(offers[0]["kind"], "frequency_change")
        self.assertEqual(offers[0]["evidence"]["by_month_day"], [1, 15])

    def test_regular_paychecks_on_the_first_and_fifteenth_are_offered(self):
        self._deposit([(dt.date(2026, m, d), 2400.0)
                       for m in (8, 9, 10) for d in (1, 15)]
                      + [(dt.date(2026, 11, 1), 2400.0)])
        offers = self._offers()
        self.assertEqual(len(offers), 1, offers)
        self.assertEqual(offers[0]["kind"], "add")
        self.assertEqual(offers[0]["evidence"]["by_month_day"], [1, 15])

    def test_unequal_checks_on_the_first_and_fifteenth_are_offered(self):
        # invented: the 15th's check is smaller every month
        self._monthly_series()
        self._deposit([(dt.date(2026, m, 1), 2400.0) for m in range(4, 8)])
        self._deposit([(dt.date(2026, m, d), a)
                       for m in (8, 9, 10)
                       for d, a in ((1, 2400.0), (15, 1300.0))]
                      + [(dt.date(2026, 11, 1), 2400.0)])
        offers = self._offers()
        self.assertEqual(len(offers), 1, offers)
        self.assertEqual(offers[0]["kind"], "frequency_change")
        self.assertEqual(offers[0]["evidence"]["by_month_day"], [1, 15])
        # one amount pays on each of the two days, so it is the mean check:
        # the month comes to what actually lands, not two large checks
        amount = self.conn.execute(
            "SELECT amount FROM bill_proposals WHERE status = 'pending' "
            "AND kind = 'frequency_change'").fetchone()["amount"]
        self.assertAlmostEqual(2 * float(amount), 3700.0, places=2)

    def test_a_salary_beside_a_monthly_commission_is_proposed(self):
        # invented: a steady salary on the 1st, a commission of varying
        # size around the 20th from the same payer
        self._deposit([(dt.date(2026, m, 1), 5000.0) for m in range(4, 12)])
        self._deposit([(dt.date(2026, m, 20), a) for m, a in zip(
            range(4, 11), (800.0, 2400.0, 1500.0, 3100.0, 1200.0, 1900.0,
                           2700.0))])
        offers = self._offers()
        adds = [o for o in offers if o["kind"] == "add"]
        self.assertEqual(len(adds), 1, offers)
        self.assertEqual((adds[0]["frequency"], adds[0]["interval"]),
                         ("MONTHLY", 1))
        amount = self.conn.execute(
            "SELECT amount FROM bill_proposals WHERE status = 'pending' "
            "AND kind = 'add'").fetchone()["amount"]
        self.assertAlmostEqual(float(amount), 5000.0, places=2)

    def test_every_other_deposit_of_a_same_day_stream_is_not_a_paycheck(self):
        # invented sizes, one deposit a month on the 3rd: three of the six
        # fall inside the latest one's band and fit every two months, but
        # the deposits between them land on the same day — one monthly
        # stream of varying sizes, not a paycheck every two months
        amounts = [1500.0, 4000.0, 900.0, 2600.0, 1700.0, 3300.0]
        self._deposit([(dt.date(2026, m, 3), a)
                       for m, a in zip(range(6, 12), amounts)])
        self.assertEqual([dict(o) for o in self._offers()], [])

    def test_a_two_month_payment_with_other_day_one_offs_in_every_gap(self):
        # a real every-two-months payment on the 3rd; each gap holds an
        # unrelated deposit on another day, which is another stream
        self._deposit([(dt.date(2026, m, 3), 3000.0)
                       for m in (1, 3, 5, 7, 9, 11)])
        self._deposit([(dt.date(2026, m, d), a) for m, d, a in (
            (2, 20, 600.0), (4, 14, 1100.0), (6, 24, 7500.0),
            (8, 18, 400.0), (10, 12, 250.0))])
        offers = self._offers()
        self.assertEqual(len(offers), 1, offers)
        self.assertEqual((offers[0]["kind"], offers[0]["interval"]),
                         ("add", 2))

    def test_a_deposit_days_before_the_first_is_another_stream(self):
        # every two months on the 1st; each gap holds a smaller deposit four
        # to six days before a 1st. Read round a short month's end those
        # days once counted as the run's own day, and the run was dropped.
        self._deposit([(d, 3000.0) for d in (
            dt.date(2026, 2, 1), dt.date(2026, 4, 1), dt.date(2026, 6, 1),
            dt.date(2026, 8, 1), dt.date(2026, 10, 1))])
        self._deposit([(dt.date(2026, 3, 26), 600.0),
                       (dt.date(2026, 5, 28), 450.0),
                       (dt.date(2026, 7, 28), 700.0),
                       (dt.date(2026, 9, 26), 520.0)])
        offers = self._offers()
        self.assertEqual(len(offers), 1, offers)
        self.assertEqual((offers[0]["kind"], offers[0]["interval"]),
                         ("add", 2))

    def test_days_apart_are_real_days_round_a_month_end(self):
        for a, b, want in (
                (dt.date(2026, 1, 26), dt.date(2026, 2, 1), 6),
                (dt.date(2026, 1, 29), dt.date(2026, 2, 1), 3),
                (dt.date(2026, 4, 26), dt.date(2026, 5, 1), 5),
                (dt.date(2026, 1, 30), dt.date(2026, 3, 2), 3),
                (dt.date(2026, 3, 3), dt.date(2026, 6, 3), 0),
                (dt.date(2026, 2, 28), dt.date(2026, 1, 31), 0),
                (dt.date(2026, 3, 1), dt.date(2026, 1, 31), 1)):
            self.assertEqual(rd._day_apart(a, b), want, (a, b))

    def test_a_two_month_payment_beside_a_few_one_offs_is_offered(self):
        self._deposit([(dt.date(2026, m, 3), 3000.0)
                       for m in (1, 3, 5, 7, 9, 11)])
        self._deposit([(dt.date(2026, 2, 20), 600.0),
                       (dt.date(2026, 6, 12), 7500.0),
                       (dt.date(2026, 10, 8), 250.0)])
        offers = self._offers()
        self.assertEqual(len(offers), 1, offers)
        self.assertEqual((offers[0]["kind"], offers[0]["interval"]),
                         ("add", 2))

    def test_a_paycheck_split_in_two_every_period_is_still_offered(self):
        # two deposits of fixed sizes each payday: every gap of either
        # size's run holds the other, but they are regular, not noise
        self._deposit([(dt.date(2026, m, 1), a)
                       for m in range(4, 12) for a in (2400.0, 500.0)])
        offers = self._offers()
        self.assertEqual(len(offers), 1, offers)
        self.assertEqual(offers[0]["kind"], "add")


if __name__ == "__main__":
    unittest.main()
