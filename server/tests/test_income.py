"""Confirmed recurring income → monthly-equivalent → seeds the
onboarding 'expected income' suggestion."""

import datetime as dt
import unittest

from oikonome.engine import bills, budget
from oikonome.engine.compat import as_date, as_dict, jsonb

from .util import TODAY, add_txn, make_db, write_config


class IncomeSeedTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_monthly_income_cadence_converted(self):
        # a biweekly $2,400 paycheck + a monthly $1,100 rental income —
        # must equal save_bill's canonical monthly_amount (a mean year of
        # 365.2425 days holds 26.09 biweekly paydays), the same figure the
        # Recurring page shows
        bills.save_bill(self.conn, payee="EMPLOYER PAYROLL", amount=2400,
                            income=True, frequency="WEEKLY", interval=2)
        bills.save_bill(self.conn, payee="RENTAL", amount=1100,
                            income=True, frequency="MONTHLY", interval=1)
        mi = budget.monthly_income(self.conn)
        expected = 2400 * (365.2425 / 14) / 12 + 1100
        self.assertAlmostEqual(mi, round(expected, 2), delta=0.01)

    def test_bills_do_not_count_as_income(self):
        bills.save_bill(self.conn, payee="NETFLIX", amount=16,
                            income=False, frequency="MONTHLY", interval=1)
        self.assertEqual(budget.monthly_income(self.conn), 0.0)

    def test_one_time_income_not_counted(self):
        # a single expected payment is not a monthly stream
        bills.save_bill(self.conn, payee="TAX REFUND", amount=3200,
                            income=True, frequency=None)
        self.assertEqual(budget.monthly_income(self.conn), 0.0)

    def test_income_detected_from_uncategorized_deposits(self):
        # SimpleFIN-shape rows: regular deposits with NO category at all
        # (no aggregator category; the LLM flow-guard never assigns INCOME)
        for i in range(6):
            add_txn(self.conn, TODAY - dt.timedelta(days=14 * i + 3), -2400,
                    "ACME PAYROLL", primary=None, account="chk")
        stats = bills.run(self.conn, today=TODAY)
        income = [p for p in self.conn.execute(
            "SELECT payee, evidence FROM bill_proposals "
            "WHERE kind='add' AND status='pending'").fetchall()
            if (as_dict(p["evidence"]) or {}).get("income")]
        self.assertTrue(income, "no income proposal from bare deposits")
        self.assertGreaterEqual(stats.get("proposed_income", 0), 1)

    def test_biweekly_income_survives_payroll_gap(self):
        # Last hit 23 days ago is past 1.5 cycles (21d), yet real biweekly
        # series often land mid-cycle in the wizard. Noise prefix
        # on the payee must not split the group either.
        for i in range(6):
            add_txn(self.conn, TODAY - dt.timedelta(days=14 * i + 23), -2850,
                    "DIR DEP NORTHWIND MFG PAYROLL", primary="INCOME",
                    account="chk", txn_id=f"dd-{i}")
        bills.run(self.conn, TODAY)
        income = [p for p in self.conn.execute(
            "SELECT payee, amount, frequency, \"interval\", evidence "
            "FROM bill_proposals WHERE kind='add' AND status='pending'"
        ).fetchall()
            if (as_dict(p["evidence"]) or {}).get("income")]
        self.assertTrue(income, "stale biweekly paycheck must still propose")
        self.assertAlmostEqual(income[0]["amount"], 2850.0, delta=1.0)
        # pending proposal seeds step-3 expected-income prefill
        sug = budget.suggest_budgets(self.conn, TODAY)
        self.assertAlmostEqual(sug["suggestions"]["income_monthly"],
                               2850 * (365.2425 / 14) / 12, delta=0.5)

    def test_income_filed_under_a_file_import_label_is_still_detected(self):
        # Deposits a CSV import filed under the file's own "Income" label
        # (before the label was canonicalized) are paycheck candidates too
        for i in range(6):
            add_txn(self.conn, TODAY - dt.timedelta(days=14 * i + 2), -2600,
                    "RIVERBEND CLINIC PAYROLL", primary="Income",
                    account="chk", txn_id=f"lbl-{i}")
        bills.run(self.conn, TODAY)
        income = [p for p in self.conn.execute(
            "SELECT amount, evidence FROM bill_proposals "
            "WHERE kind='add' AND status='pending'").fetchall()
            if (as_dict(p["evidence"]) or {}).get("income")]
        self.assertEqual(len(income), 1)
        self.assertAlmostEqual(income[0]["amount"], 2600.0, delta=1.0)

    def test_income_raise_ladder_proposes_the_current_paycheck(self):
        # Three pay rungs inside six months (a new job's starting pay, a
        # raise, then another): the recent median lands on the MIDDLE rung
        # with two deposits at that size, so a median-anchored core never
        # reaches three hits and the household's pay is never proposed.
        # The latest deposit anchors first: the series is proposed at the
        # current paycheck, biweekly, due on the next payday.
        rungs = [1000.0] * 5 + [1500.0] * 2 + [2000.0] * 4
        for i, amt in enumerate(reversed(rungs)):
            add_txn(self.conn, TODAY - dt.timedelta(days=14 * i + 1), -amt,
                    "ACME HOLDINGS PAYROLL", primary="INCOME",
                    account="chk", txn_id=f"rung-{i}")
        bills.run(self.conn, TODAY)
        income = [p for p in self.conn.execute(
            "SELECT amount, frequency, \"interval\", next_due, evidence "
            "FROM bill_proposals WHERE kind='add' AND status='pending'"
        ).fetchall() if (as_dict(p["evidence"]) or {}).get("income")]
        self.assertEqual(len(income), 1, "the ladder must propose once")
        self.assertAlmostEqual(income[0]["amount"], 2000.0, delta=1.0)
        self.assertEqual((income[0]["frequency"], income[0]["interval"]),
                         ("WEEKLY", 2))
        self.assertEqual(as_date(income[0]["next_due"]),
                         TODAY + dt.timedelta(days=13))

    def test_income_latest_stipend_does_not_replace_the_paycheck(self):
        # The same employer pays a biweekly $2,400 and a monthly $250
        # stipend through one descriptor; detection runs the night after a
        # stipend. The stipend gathers its own core and even fits a monthly
        # cycle, but the paycheck has more recent on-cycle deposits: the
        # series is proposed at the paycheck, not the stipend, and once
        # approved the employer key is taken — so the choice has to be
        # right the first time.
        for i in range(13):
            add_txn(self.conn, TODAY - dt.timedelta(days=14 * i + 3), -2400,
                    "ACME HOLDINGS PAYROLL", primary="INCOME",
                    account="chk", txn_id=f"pay-{i}")
        for i in range(6):
            add_txn(self.conn, TODAY - dt.timedelta(days=30 * i + 1), -250,
                    "ACME HOLDINGS PAYROLL", primary="INCOME",
                    account="chk", txn_id=f"stipend-{i}")
        bills.run(self.conn, TODAY)
        income = [p for p in self.conn.execute(
            "SELECT amount, frequency, \"interval\", evidence "
            "FROM bill_proposals WHERE kind='add' AND status='pending'"
        ).fetchall() if (as_dict(p["evidence"]) or {}).get("income")]
        self.assertEqual(len(income), 1)
        self.assertAlmostEqual(income[0]["amount"], 2400.0, delta=1.0)
        self.assertEqual((income[0]["frequency"], income[0]["interval"]),
                         ("WEEKLY", 2))

    def test_income_quarterly_bonus_on_payday_is_not_the_series(self):
        # A quarterly bonus paid on a payday merges with that day's check
        # into one larger event; eight of those over two years fit a
        # 91-day cycle of their own. Detection the week after a bonus
        # must still propose the biweekly paycheck.
        for i in range(52):
            d = TODAY - dt.timedelta(days=14 * i + 2)
            amt = 3500 if i % 6 == 0 else 2000      # every sixth payday
            add_txn(self.conn, d, -amt, "ACME HOLDINGS PAYROLL",
                    primary="INCOME", account="chk", txn_id=f"q-{i}")
        bills.run(self.conn, TODAY)
        income = [p for p in self.conn.execute(
            "SELECT amount, frequency, \"interval\", evidence "
            "FROM bill_proposals WHERE kind='add' AND status='pending'"
        ).fetchall() if (as_dict(p["evidence"]) or {}).get("income")]
        self.assertEqual(len(income), 1)
        self.assertAlmostEqual(income[0]["amount"], 2000.0, delta=1.0)
        self.assertEqual((income[0]["frequency"], income[0]["interval"]),
                         ("WEEKLY", 2))

    def test_income_bonus_sized_latest_deposit_falls_back_to_the_median(self):
        # The latest deposit is a bonus: no core gathers around it, so the
        # recent median anchors as before and the regular paycheck is
        # proposed at its own size, not the bonus's.
        for i in range(6):
            add_txn(self.conn, TODAY - dt.timedelta(days=14 * i + 15), -3100,
                    "ACME HOLDINGS PAYROLL", primary="INCOME",
                    account="chk", txn_id=f"reg-{i}")
        add_txn(self.conn, TODAY - dt.timedelta(days=1), -12000,
                "ACME HOLDINGS PAYROLL", primary="INCOME", account="chk",
                txn_id="bonus-latest")
        bills.run(self.conn, TODAY)
        income = [p for p in self.conn.execute(
            "SELECT amount, evidence FROM bill_proposals "
            "WHERE kind='add' AND status='pending'").fetchall()
            if (as_dict(p["evidence"]) or {}).get("income")]
        self.assertEqual(len(income), 1)
        self.assertAlmostEqual(income[0]["amount"], 3100.0, delta=1.0)

    def test_income_amount_core_uses_recent_paycheck(self):
        # A pay rise: an older, smaller series would drag the all-history
        # median down and density-kill the biweekly fit. Detection must
        # anchor on the recent, larger series.
        for i in range(10):
            add_txn(self.conn, TODAY - dt.timedelta(days=14 * i + 3), -5800,
                    "NORTHWIND MFG PAYROLL", primary="INCOME",
                    account="chk", txn_id=f"new-{i}")
        for i in range(12):
            add_txn(self.conn, TODAY - dt.timedelta(days=14 * (i + 12) + 3),
                    -3700, "NORTHWIND MFG PAYROLL", primary="INCOME",
                    account="chk", txn_id=f"old-{i}")
        # sparse off-amount noise (bonuses / corrections)
        add_txn(self.conn, TODAY - dt.timedelta(days=40), -25000,
                "NORTHWIND MFG PAYROLL", primary="INCOME", account="chk",
                txn_id="bonus")
        bills.run(self.conn, TODAY)  # fixed TODAY — not date.today()
        income = [p for p in self.conn.execute(
            "SELECT amount, evidence FROM bill_proposals "
            "WHERE kind='add' AND status='pending'").fetchall()
            if (as_dict(p["evidence"]) or {}).get("income")]
        self.assertTrue(income, "raised biweekly series must still propose")
        self.assertAlmostEqual(income[0]["amount"], 5800.0, delta=50.0)

    def test_suggest_income_falls_back_to_pending_proposals(self):
        # nothing approved yet (mid-wizard) → the pending income proposal
        # still seeds the expected-income prefill
        self.conn.execute(
            """INSERT INTO bill_proposals (id, kind, bill_type, payee,
                   amount, frequency, "interval", evidence, status, created_at)
               VALUES ('prop:i','add','occurrence','ACME PAYROLL',2400,
                       'WEEKLY',2,%s,'pending',now())""",
            (jsonb({"income": True}),))
        sug = budget.suggest_budgets(self.conn, TODAY)
        self.assertAlmostEqual(sug["suggestions"]["income_monthly"],
                               2400 * (365.2425 / 14) / 12, delta=0.01)

    def test_suggest_budgets_includes_income(self):
        bills.save_bill(self.conn, payee="PAY", amount=3000,
                            income=True, frequency="MONTHLY", interval=1)
        sug = budget.suggest_budgets(self.conn, TODAY)
        self.assertIn("income_monthly", sug["suggestions"])
        self.assertAlmostEqual(sug["suggestions"]["income_monthly"],
                               budget.monthly_income(self.conn), delta=0.01)


if __name__ == "__main__":
    unittest.main()
