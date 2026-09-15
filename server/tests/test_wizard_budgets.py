"""Wizard-budget refinements: carve-out chip candidates + the
monthly-bill figure in /budgets/suggest, and open-ended (plan-only)
savings goals."""

import datetime as dt
import unittest

from oikonome.engine import budget, savings

from .util import add_bill, add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 15)


class BucketCandidateTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_candidates_floored_and_sorted(self):
        # 3 full months of category spend: gas ~$150/mo, entertainment
        # ~$40/mo (below floor), plus food that must not appear
        for mo in (4, 5, 6):
            add_txn(self.conn, dt.date(2026, mo, 10), 150.0, "CHEVRON",
                    primary="TRANSPORTATION", account="chk")
            add_txn(self.conn, dt.date(2026, mo, 12), 40.0, "CINEMARK",
                    primary="ENTERTAINMENT", account="chk")
            add_txn(self.conn, dt.date(2026, mo, 14), 300.0, "KROGER",
                    primary="FOOD_AND_DRINK", account="chk")
        out = budget.suggest_budgets(self.conn, TODAY)
        cands = out["bucket_candidates"]
        names = [c["name"] for c in cands]
        self.assertIn("Transportation", names)
        self.assertNotIn("Entertainment", names)      # under the $75 floor
        self.assertFalse([n for n in names if "Food" in n])
        gas = next(c for c in cands if c["name"] == "Transportation")
        self.assertEqual(gas["monthly"], 150.0)
        self.assertEqual(gas["category"], "TRANSPORTATION")
        self.assertIn("avg_bills_monthly", out)

    def test_existing_bucket_categories_not_re_suggested(self):
        for mo in (4, 5, 6):
            add_txn(self.conn, dt.date(2026, mo, 10), 150.0, "CHEVRON",
                    primary="TRANSPORTATION", account="chk")
        write_config(self.conn, custom_buckets=[
            {"name": "Gas", "parent": "other", "monthly": 150,
             "categories": ["TRANSPORTATION"], "merchants": []}])
        out = budget.suggest_budgets(self.conn, TODAY)
        self.assertFalse([c for c in out["bucket_candidates"]
                          if c["category"] == "TRANSPORTATION"])

    def test_bill_rows_not_candidates(self):
        for mo in (4, 5, 6):
            add_txn(self.conn, dt.date(2026, mo, 1), 1200.00, "NORTHWIND MORTGAGE",
                    primary="RENT_AND_UTILITIES", account="chk")
        add_bill(self.conn, "NORTHWIND MORTGAGE", 1200.00,
                 next_due=dt.date(2026, 8, 1))
        out = budget.suggest_budgets(self.conn, TODAY)
        self.assertFalse([c for c in out["bucket_candidates"]
                          if "Rent" in c["name"]])

    def test_food_includes_mtd_and_sparse_months(self):
        # All grocery spend is in the CURRENT partial month — the old
        # window excluded it, and 2×median(0) dropped any remaining
        # non-zero months → food suggested $0 / blank wizard field.
        add_txn(self.conn, TODAY, 180.0, "KROGER",
                primary="FOOD_AND_DRINK", account="chk")
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 90.0, "SAFEWAY",
                primary="FOOD_AND_DRINK", account="chk")
        # one older sparse hit must not zero the suggestion either
        add_txn(self.conn, dt.date(2026, 4, 10), 40.0, "CORNER COFFEE",
                primary="FOOD_AND_DRINK", account="chk")
        out = budget.suggest_budgets(self.conn, TODAY)
        # non-zero months: Apr $40 + Jul MTD $270 → median ~$150-ish
        self.assertGreaterEqual(out["suggestions"]["food_monthly"], 40)
        self.assertLessEqual(out["suggestions"]["food_monthly"], 300)

    def test_pending_bills_seed_avg_and_exclude_from_other(self):
        # Mid-wizard: bills approved as proposals only — still seed the
        # bill line and keep the mortgage out of variable "other"
        from oikonome.engine.compat import jsonb
        for mo in (4, 5, 6):
            add_txn(self.conn, dt.date(2026, mo, 1), 1200.00, "NORTHWIND MORTGAGE",
                    primary="RENT_AND_UTILITIES", account="chk")
            add_txn(self.conn, dt.date(2026, mo, 14), 300.0, "KROGER",
                    primary="FOOD_AND_DRINK", account="chk")
            add_txn(self.conn, dt.date(2026, mo, 20), 120.0, "CHEVRON",
                    primary="TRANSPORTATION", account="chk")
        self.conn.execute(
            """INSERT INTO bill_proposals (id, kind, bill_type, payee,
                   amount, frequency, "interval", evidence, status, created_at)
               VALUES ('prop:pm','add','occurrence','NORTHWIND MORTGAGE',1200.00,
                       'MONTHLY',1,%s,'pending',now())""",
            (jsonb({}),))
        out = budget.suggest_budgets(self.conn, TODAY)
        self.assertAlmostEqual(out["avg_bills_monthly"], 1200.00, delta=1.0)
        self.assertEqual(out["bills_count"], 1)
        # food still suggested; transportation carve-out still a candidate
        self.assertAlmostEqual(out["suggestions"]["food_monthly"], 300.0,
                               delta=1.0)
        self.assertTrue(any(c["category"] == "TRANSPORTATION"
                            for c in out["bucket_candidates"]))
        # rent-shaped history covered by the pending bill → not a carve-out
        self.assertFalse([c for c in out["bucket_candidates"]
                          if "Rent" in c["name"]])

    def test_approved_bill_merchant_excluded_even_when_amount_drifts(self):
        # Utility bill is $100; July charge is $140 — amount gate used to
        # leave the drift in "other" / Rent carve-out (double-count).
        from oikonome.engine import bills as bills_mod
        for mo, amt in ((4, 100.0), (5, 100.0), (6, 100.0), (7, 140.0)):
            add_txn(self.conn, dt.date(2026, mo, 5), amt, "NORTHSIDE POWER",
                    primary="RENT_AND_UTILITIES", account="chk",
                    txn_id=f"pw-{mo}")
        bills_mod.save_bill(self.conn, payee="Northside Power", amount=100.0,
                            frequency="MONTHLY", interval=1,
                            next_due=dt.date(2026, 8, 1), merchant="northside power",
                            source="detected")
        out = budget.suggest_budgets(self.conn, TODAY)
        self.assertFalse([c for c in out["bucket_candidates"]
                          if "Rent" in c["name"]],
                         out["bucket_candidates"])
        # no residual northside power in other
        self.assertEqual(out["suggestions"]["other_monthly"], 0.0)


class OpenEndedGoalTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_plan_only_goal_reduces_surplus_without_pct(self):
        cfg = {"savings_goals": [{"name": "Savings", "target": 0,
                                  "monthly_plan": 1200.0, "tokens": [],
                                  "start_balance": 0}]}
        self.assertEqual(savings.monthly_plan_total(cfg), 1200.0)
        p = savings.progress(self.conn, cfg, TODAY)[0]
        self.assertIsNone(p["pct"])
        self.assertIsNone(p["eta"])                    # never "funded ✓"
        self.assertIsNone(p["on_pace"])

    def test_milestones_silent_without_target(self):
        cfg = {"savings_goals": [{"name": "Savings", "target": 0,
                                  "monthly_plan": 500.0, "tokens": [],
                                  "start_balance": 5000}]}
        msgs, _ = savings.check_milestones(self.conn, cfg, TODAY)
        self.assertEqual(msgs, [])


if __name__ == "__main__":
    unittest.main()
