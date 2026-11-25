"""Which deposits are paychecks is decided per payer, once.

A payer is a paycheck payer when the household said so, when any of its
deposits carries the wages tag, when it matches an active income bill,
or when its deposits read as payroll: described as pay, on a regular
weekly / twice-monthly / monthly cadence, at a check's size. An earlier
employer with no bill and untagged rows is paychecks for the years it
paid; a regular transfer from a person, or a payroll-worded one-off, is
not. The household's word beats every rule, the wages tag included, and
the Income tab and the flow picture split by the same verdict.
"""

import datetime as dt
import unittest

from oikonome.engine import paychecks, reporting

from .util import TODAY, add_bill, add_txn, make_db, write_config

CHECK = 1630.0


def _biweekly(conn, name, first, n, amount=CHECK, detailed=None):
    for i in range(n):
        add_txn(conn, first + dt.timedelta(days=14 * i), -amount, name,
                primary="INCOME", detailed=detailed, account="chk")


class PaycheckPayers(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        # an earlier employer: untagged biweekly payroll that ended a year
        # ago — no bill tracks it
        _biweekly(self.conn, "Tailspin Toys Payroll", dt.date(2024, 1, 5), 40)

    def _kinds(self, rng="all"):
        return reporting.income_window(self.conn, TODAY, rng)["kinds"]

    def test_an_earlier_employers_untagged_payroll_is_paychecks(self):
        k = self._kinds()
        self.assertAlmostEqual(k["paychecks"], 40 * CHECK, places=2)
        self.assertAlmostEqual(k["other"], 0.0, places=2)

    def test_the_flow_picture_splits_by_the_same_verdict(self):
        flow = reporting.flow_breakdown(self.conn, TODAY, "all")
        self.assertAlmostEqual(flow["in"]["paychecks"], 40 * CHECK, places=2)
        self.assertAlmostEqual(flow["in"]["other"], 0.0, places=2)

    def test_a_regular_transfer_from_a_person_is_not_paychecks(self):
        for i in range(8):
            add_txn(self.conn, dt.date(2025, 9, 1) + dt.timedelta(days=30 * i),
                    -910.0, "Zelle From Riley Avery", primary="TRANSFER_IN",
                    override="INCOME", account="chk")
        k = self._kinds()
        self.assertAlmostEqual(k["other"], 8 * 910.0, places=2)
        self.assertAlmostEqual(k["paychecks"], 40 * CHECK, places=2)

    def test_payroll_words_without_a_cadence_are_not_paychecks(self):
        add_txn(self.conn, dt.date(2026, 2, 3), -410.0,
                "Contoso Payroll Adjustment", primary="INCOME", account="chk")
        add_txn(self.conn, dt.date(2026, 6, 9), -330.0,
                "Contoso Payroll Adjustment", primary="INCOME", account="chk")
        k = self._kinds()
        self.assertAlmostEqual(k["other"], 740.0, places=2)
        self.assertAlmostEqual(k["paychecks"], 40 * CHECK, places=2)

    def test_a_cadence_with_wildly_different_amounts_is_not_paychecks(self):
        amounts = (53.0, 2.5 * CHECK, 9.0, 0.6 * CHECK, 2 * CHECK, 17.0)
        for i, amt in enumerate(amounts):
            add_txn(self.conn, dt.date(2026, 1, 2) + dt.timedelta(days=14 * i),
                    -amt, "Northwind Direct Dep", primary="INCOME",
                    account="chk")
        k = self._kinds()
        self.assertAlmostEqual(k["other"], sum(amounts), places=2)
        self.assertAlmostEqual(k["paychecks"], 40 * CHECK, places=2)

    def test_the_households_never_beats_the_wages_tag(self):
        _biweekly(self.conn, "Fabrikam Inc", dt.date(2026, 1, 9), 10,
                  amount=2140.0, detailed="INCOME_WAGES")
        write_config(self.conn, paycheck_payers={"fabrikaminc": False})
        k = self._kinds()
        self.assertAlmostEqual(k["other"], 10 * 2140.0, places=2)
        self.assertAlmostEqual(k["paychecks"], 40 * CHECK, places=2)

    def test_the_households_paycheck_counts_any_payer(self):
        add_txn(self.conn, dt.date(2026, 3, 1), -610.0, "Adatum Stipend",
                primary="INCOME", account="chk")
        write_config(self.conn, paycheck_payers={"adatumstipend": True})
        self.assertAlmostEqual(self._kinds()["paychecks"], 40 * CHECK + 610,
                               places=2)

    def test_an_income_bill_still_makes_its_payer_paychecks(self):
        add_txn(self.conn, dt.date(2026, 6, 1), -3170.0, "Woodgrove Clinic",
                primary="INCOME", account="chk")
        add_bill(self.conn, "Woodgrove Clinic", 3170.0, income=True,
                 next_due=dt.date(2026, 7, 1), last_seen=dt.date(2026, 6, 1))
        self.assertAlmostEqual(self._kinds()["paychecks"], 40 * CHECK + 3170,
                               places=2)

    def test_the_income_total_does_not_move_with_the_verdict(self):
        w = reporting.income_window(self.conn, TODAY, "all")
        before = w["total"]
        self.assertAlmostEqual(w["kinds"]["paychecks"], before, places=2)
        write_config(self.conn, paycheck_payers={"tailspintoyspayroll": False})
        w = reporting.income_window(self.conn, TODAY, "all")
        self.assertAlmostEqual(w["total"], before, places=2)
        self.assertAlmostEqual(w["kinds"]["other"], 40 * CHECK, places=2)

    def test_each_source_row_carries_its_key_and_the_households_word(self):
        add_txn(self.conn, dt.date(2026, 3, 1), -610.0, "Adatum Stipend",
                primary="INCOME", account="chk")
        write_config(self.conn, paycheck_payers={"adatumstipend": False})
        w = reporting.income_window(self.conn, TODAY, "all")
        rows = dict(zip((s[0] for s in w["sources"]), w["source_paychecks"]))
        self.assertEqual(len(w["source_paychecks"]), len(w["sources"]))
        self.assertEqual(rows["Tailspin Toys Payroll"],
                         {"key": "tailspintoyspayroll", "paycheck": True,
                          "override": None})
        self.assertEqual(rows["Adatum Stipend"],
                         {"key": "adatumstipend", "paycheck": False,
                          "override": "never"})


class ClassifyPayer(unittest.TestCase):
    """The cadence rule on its own."""

    def _run(self, gap, n, name="Litware Payroll", amount=1010.0):
        d0 = dt.date(2025, 1, 3)
        return [(d0 + dt.timedelta(days=gap * i), amount, name)
                for i in range(n)]

    def test_weekly_twice_monthly_and_monthly_runs_are_payroll(self):
        for gap in (7, 14, 15, 30):
            self.assertTrue(paychecks.classify_payer(self._run(gap, 6)), gap)

    def test_too_few_deposits_or_an_irregular_gap_is_not(self):
        self.assertFalse(paychecks.classify_payer(self._run(14, 3)))
        self.assertFalse(paychecks.classify_payer(self._run(45, 8)))

    def test_without_words_that_name_pay_it_is_not(self):
        self.assertFalse(paychecks.classify_payer(
            self._run(14, 10, name="Litware Ltd")))

    def test_a_long_gap_ends_a_run_but_an_earlier_run_still_counts(self):
        rows = self._run(14, 5) + [(dt.date(2026, 6, 1), 1010.0,
                                    "Litware Payroll")]
        self.assertTrue(paychecks.classify_payer(rows))


if __name__ == "__main__":
    unittest.main()
