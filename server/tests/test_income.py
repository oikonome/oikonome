"""Confirmed recurring income → monthly-equivalent → seeds the
onboarding 'expected income' suggestion."""

import datetime as dt
import unittest

from oikonome.engine import bills, budget
from oikonome.engine.compat import as_dict, jsonb

from .util import TODAY, add_txn, make_db, write_config


class IncomeSeedTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_monthly_income_cadence_converted(self):
        # a biweekly $2,400 paycheck + a monthly $1,100 rental income —
        # must equal save_bill's canonical monthly_amount (52/2/12 paydays),
        # the same figure the Recurring page shows
        bills.save_bill(self.conn, payee="EMPLOYER PAYROLL", amount=2400,
                            income=True, frequency="WEEKLY", interval=2)
        bills.save_bill(self.conn, payee="RENTAL", amount=1100,
                            income=True, frequency="MONTHLY", interval=1)
        mi = budget.monthly_income(self.conn)
        expected = 2400 * 52 / 2 / 12 + 1100
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
        # Last hit 23 days ago fails the old 1.5×cycle (21d) gate; real
        # biweekly series often land mid-cycle in the wizard. Noise prefix
        # on the payee must not split the group either.
        for i in range(6):
            add_txn(self.conn, TODAY - dt.timedelta(days=14 * i + 23), -3100,
                    "DIR DEP NORTHWIND HEALTH", primary="INCOME",
                    account="chk", txn_id=f"dd-{i}")
        bills.run(self.conn, TODAY)
        income = [p for p in self.conn.execute(
            "SELECT payee, amount, frequency, \"interval\", evidence "
            "FROM bill_proposals WHERE kind='add' AND status='pending'"
        ).fetchall()
            if (as_dict(p["evidence"]) or {}).get("income")]
        self.assertTrue(income, "stale biweekly paycheck must still propose")
        self.assertAlmostEqual(income[0]["amount"], 3100.0, delta=1.0)
        # pending proposal seeds step-3 expected-income prefill
        sug = budget.suggest_budgets(self.conn, TODAY)
        self.assertAlmostEqual(sug["suggestions"]["income_monthly"],
                               3100 * 52 / 2 / 12, delta=0.5)

    def test_income_amount_core_uses_recent_paycheck(self):
        # Multi-year raise ladder: older ~$4k deposits would drag the
        # all-history median down and density-kill the biweekly fit.
        # Detection must anchor on the recent ~$6.6k series.
        for i in range(10):
            add_txn(self.conn, TODAY - dt.timedelta(days=14 * i + 3), -6600,
                    "NORTHWIND HEALTH", primary="INCOME",
                    account="chk", txn_id=f"new-{i}")
        for i in range(12):
            add_txn(self.conn, TODAY - dt.timedelta(days=14 * (i + 12) + 3),
                    -4000, "NORTHWIND HEALTH", primary="INCOME",
                    account="chk", txn_id=f"old-{i}")
        # sparse off-amount noise (bonuses / corrections)
        add_txn(self.conn, TODAY - dt.timedelta(days=40), -40000,
                "NORTHWIND HEALTH", primary="INCOME", account="chk",
                txn_id="bonus")
        bills.run(self.conn, TODAY)  # fixed TODAY — not date.today()
        income = [p for p in self.conn.execute(
            "SELECT amount, evidence FROM bill_proposals "
            "WHERE kind='add' AND status='pending'").fetchall()
            if (as_dict(p["evidence"]) or {}).get("income")]
        self.assertTrue(income, "raised biweekly series must still propose")
        self.assertAlmostEqual(income[0]["amount"], 6600.0, delta=50.0)

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
                               2400 * 52 / 2 / 12, delta=0.01)

    def test_suggest_budgets_includes_income(self):
        bills.save_bill(self.conn, payee="PAY", amount=3000,
                            income=True, frequency="MONTHLY", interval=1)
        sug = budget.suggest_budgets(self.conn, TODAY)
        self.assertIn("income_monthly", sug["suggestions"])
        self.assertAlmostEqual(sug["suggestions"]["income_monthly"],
                               budget.monthly_income(self.conn), delta=0.01)


if __name__ == "__main__":
    unittest.main()
