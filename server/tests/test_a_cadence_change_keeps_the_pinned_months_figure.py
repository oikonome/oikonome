"""A pinned amount is what the household said the bill comes to, and a cadence
offer approved on a pinned bill must not change that by itself. The amount is
stored per payment, so moving a pinned $4,000 a month to two days of the month
would write $4,000 on each day and double the bill. When the charges say the
pinned figure was the month's, the payment becomes its share of the month and
the pin stays. When they say it was the price of a payment, the price stands.
"""
import datetime as dt
import unittest

from oikonome.engine import bills as rd
from oikonome.engine import budget
from oikonome.engine.compat import as_dict, jsonb

from .test_twice_a_month_pay_stays_twice_a_month import fifteenth_and_last
from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 11, 5)
NEXT = dt.date(2026, 11, 15)
PAYEE = "Northwind Logistics Payroll"
PID = "prop:cadence-pin"


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def bill(self):
        return rd.get_bill(self.conn, PAYEE)

    def pin(self):
        self.conn.execute("UPDATE bills SET raw = raw || %s::jsonb "
                          "WHERE payee=%s",
                          (jsonb({"lock_amount": True}), PAYEE))

    def offer_and_approve(self, amount, frequency="MONTHLY", interval=1,
                          days=None, **ev):
        old = self.bill()
        evidence = {"old_amount": abs(float(old["amount"])),
                    "old_due": old["due_on"].isoformat(), **ev}
        if days is not None:
            evidence["by_month_day"] = days
        rd._insert_proposal(self.conn, pid=PID, kind="frequency_change",
                            payee=PAYEE, amount=amount, frequency=frequency,
                            interval=interval, next_due=NEXT,
                            evidence=evidence)
        out = rd.apply_proposal(self.conn, PID, "approve")
        self.assertNotIn("error", out, out)
        return out

    def assertBill(self, amount, monthly, pinned=True):
        b = self.bill()
        self.assertAlmostEqual(abs(float(b["amount"])), amount, places=2)
        self.assertAlmostEqual(abs(float(b["monthly_amount"])), monthly,
                               places=2)
        self.assertEqual(bool(as_dict(b["raw"]).get("lock_amount")), pinned)


class AMonthlyFigureCarriedOntoSeveralDays(_Fixture):
    def _pinned_monthly(self, amount=4000.0):
        add_bill(self.conn, PAYEE, amount, next_due=NEXT, by_month_day=15,
                 income=True)
        self.pin()

    def test_the_deposits_splitting_in_two_keeps_the_months_figure(self):
        self._pinned_monthly()
        out = self.offer_and_approve(2000.0, days=[15, -1])
        self.assertBill(2000.0, 4000.0)
        self.assertEqual(budget.monthly_rule_days(
            as_dict(self.bill()["raw"])["recurrence"]), [15, -1])
        self.assertIn("4,000", out["note"])

    def test_an_offer_near_half_still_keeps_the_figure_the_household_set(self):
        # the household's figure, not the machine's, is what the pin holds
        self._pinned_monthly()
        self.offer_and_approve(2100.0, days=[15, -1])
        self.assertBill(2000.0, 4000.0)

    def test_the_whole_way_from_the_ledger_through_the_nightly_offer(self):
        for d in fifteenth_and_last():
            add_txn(self.conn, d, -2000.0, PAYEE, primary="INCOME",
                    account="chk")
        self.conn.commit()
        self._pinned_monthly()
        rd.run(self.conn, TODAY)
        offers = self.conn.execute(
            "SELECT * FROM bill_proposals WHERE status='pending' "
            "AND kind='frequency_change'").fetchall()
        self.assertEqual(len(offers), 1, offers)
        self.assertAlmostEqual(float(offers[0]["amount"]), 2000.0)
        out = rd.apply_proposal(self.conn, offers[0]["id"], "approve")
        self.assertNotIn("error", out, out)
        self.assertBill(2000.0, 4000.0)

    def test_an_unpinned_bill_takes_the_offer(self):
        add_bill(self.conn, PAYEE, 4000.0, next_due=NEXT, by_month_day=15,
                 income=True)
        self.offer_and_approve(2100.0, days=[15, -1])
        self.assertBill(2100.0, 4200.0, pinned=False)


class TheMoveBack(_Fixture):
    def _pinned_twice(self, amount=2000.0):
        add_bill(self.conn, PAYEE, amount, next_due=NEXT,
                 by_month_day=[15, -1], income=True)
        self.pin()

    def test_two_payments_merging_into_one_keep_the_months_figure(self):
        self._pinned_twice()
        out = self.offer_and_approve(4000.0, days=15)
        self.assertBill(4000.0, 4000.0)
        self.assertEqual(as_dict(self.bill()["raw"])["recurrence"]
                         ["byMonthDay"], [15])
        self.assertIn("note", out)

    def test_one_payment_dropped_at_the_same_price_keeps_the_price(self):
        # the charges still say $2,000: the pin was the price of a payment
        self._pinned_twice()
        out = self.offer_and_approve(2000.0, days=15)
        self.assertBill(2000.0, 2000.0)
        self.assertNotIn("note", out)

    def test_an_offer_that_leaves_the_two_days_changes_nothing(self):
        # an offer filed without days keeps the rule save_bill would keep,
        # so there are as many payments as before
        self._pinned_twice()
        self.offer_and_approve(4000.0)
        self.assertBill(2000.0, 4000.0)


class EveryOtherCycleChange(_Fixture):
    def _pinned(self, amount, **kw):
        add_bill(self.conn, PAYEE, amount, next_due=NEXT, **kw)
        self.pin()

    def test_a_price_that_matches_the_charges_stands(self):
        # every two weeks to twice a month at the same check: the rule the
        # pin was first written for
        self._pinned(2000.0, frequency="WEEKLY", interval=2, income=True)
        out = self.offer_and_approve(2000.0, days=[15, -1])
        self.assertBill(2000.0, 4000.0)
        self.assertNotIn("note", out)

    def test_twice_a_month_to_every_two_weeks_at_the_same_check_stands(self):
        self._pinned(2000.0, by_month_day=[15, -1], income=True)
        out = self.offer_and_approve(2000.0, frequency="WEEKLY", interval=2)
        self.assertAlmostEqual(abs(float(self.bill()["amount"])), 2000.0)
        self.assertNotIn("note", out)

    def test_a_salary_cut_differently_keeps_the_months_figure(self):
        # the same salary paid 24 times instead of 26: the check grows
        self._pinned(1673.0, frequency="WEEKLY", interval=2, income=True)
        before = abs(float(self.bill()["monthly_amount"]))
        self.offer_and_approve(1812.0, days=[15, -1])
        self.assertAlmostEqual(abs(float(self.bill()["monthly_amount"])),
                               before, delta=0.02)

    def test_a_monthly_price_charged_weekly_at_that_price_stands(self):
        self._pinned(20.0)
        self.offer_and_approve(20.0, frequency="WEEKLY")
        self.assertAlmostEqual(abs(float(self.bill()["amount"])), 20.0)

    def test_a_monthly_figure_split_into_weekly_charges_keeps_the_figure(self):
        self._pinned(100.0)
        self.offer_and_approve(23.0, frequency="WEEKLY")
        self.assertAlmostEqual(abs(float(self.bill()["monthly_amount"])),
                               100.0, delta=0.05)

    def test_a_yearly_figure_billed_monthly_keeps_the_figure(self):
        self._pinned(1200.0, frequency="YEARLY")
        self.offer_and_approve(100.0, days=15)
        self.assertBill(100.0, 100.0)

    def test_an_unreadable_old_cadence_leaves_the_pinned_amount_alone(self):
        self._pinned(4000.0)
        self.conn.execute(
            "UPDATE bills SET raw = jsonb_set(raw, '{recurrence}', "
            "'{\"frequency\": \"FORTNIGHTLY\", \"interval\": \"x\"}') "
            "WHERE payee=%s", (PAYEE,))
        self.offer_and_approve(2000.0, days=[15, -1])
        self.assertAlmostEqual(abs(float(self.bill()["amount"])), 4000.0)


class AnOfferFiledWithoutItsStartingAmount(_Fixture):
    def test_it_changes_the_cadence_without_doubling_the_bill(self):
        add_bill(self.conn, PAYEE, 4000.0, next_due=NEXT, by_month_day=15,
                 income=True)
        rd._insert_proposal(self.conn, pid=PID, kind="frequency_change",
                            payee=PAYEE, amount=2000.0, frequency="MONTHLY",
                            interval=1, next_due=NEXT,
                            evidence={"by_month_day": [15, -1]})
        out = rd.apply_proposal(self.conn, PID, "approve")
        self.assertNotIn("error", out, out)
        self.assertBill(2000.0, 4000.0, pinned=False)


if __name__ == "__main__":
    unittest.main()
