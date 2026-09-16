"""Server-side API gaps behind the SPA.

1. GET /api/bills grows `archived` — active=0 bills for the archived
   section (restore/delete-forever buttons).
2. GET /api/today/full grows the This-month's-plan block (plan_surplus,
   income, avg_bills, variable_scale, irregular_bills), overdue_unpaid,
   the pace_nocards forecast series, and MTD-by-category — all straight
   from budget.month_status / todayview.build_context / forecast.build.
3. GET /api/bills bill rows carry this month's occurrence/envelope
   stats (Paid x/y; envelope Used/Chgs/Paid).
4. GET /api/bills bill rows carry the ledger-fit hint (💡): fit summary,
   fit_diverges, and the dismissed flag (rejection memory).
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_bill, add_txn, seed_accounts, write_config


def _make_client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


def _signup(client: TestClient, prefix: str) -> str:
    client.post("/api/signup", data={
        "email": f"{prefix}-{uuid.uuid4().hex[:8]}@example.dev",
        "password": "correct-horse-battery"})
    return client.get("/api/me").json()["tenant_id"]


class TodayFullGapTests(unittest.TestCase):
    """Gap 2: the This-month's-plan / overdue / pace_nocards / MTD keys."""

    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.tid = _signup(cls.client, "tfg")
        cls.today = dt.date.today()
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn, budgeted_income_monthly=6000)
            # monthly + yearly bill, both due today and unpaid → this month's
            # schedule (720) and the overdue list; yearly is "irregular"
            add_bill(conn, "Water Utility", 120, frequency="MONTHLY",
                     next_due=cls.today.isoformat())
            add_bill(conn, "Annual Insurance", 600, frequency="YEARLY",
                     next_due=cls.today.isoformat())
            add_txn(conn, cls.today, 45.0, "Corner Store")
        finally:
            conn.close()

    def _config(self, **over):
        conn = tenancy.tenant_connect(self.tid)
        try:
            write_config(conn, **over)
        finally:
            conn.close()

    def test_plan_block_and_overdue_unpaid(self):
        self._config(budgeted_income_monthly=6000)
        r = self.client.get("/api/today/full").json()
        self.assertEqual(r["income"], 6000)
        fixed = r["buckets"]["fixed"]["month_budget"]
        self.assertAlmostEqual(fixed, 720.0, places=2)
        # plan_surplus is a STANDING metric: it
        # balances on the EVENED yearly bill average — the same number the
        # budget planner uses — never this month's schedule, so saving the
        # planner always lands surplus ≈ 0. Heavy months surface in
        # irregular_bills / the forecast instead.
        self.assertAlmostEqual(
            r["plan_surplus"], 6000 - r["avg_bills"] - r["variable_budget"],
            places=2)
        self.assertGreater(r["avg_bills"], 0)
        self.assertLess(r["avg_bills"], fixed)   # July carries the annual hit
        self.assertIsNone(r["variable_scale"])   # dynamic budget off
        self.assertIn("annual",
                      [i["label"] for i in r["irregular_bills"]
                       if i["payee"] == "Annual Insurance"])
        # both bills due today, nothing posted → [payee, amount, due] rows
        self.assertIn(["Water Utility", 120.0, self.today.isoformat()],
                      r["overdue_unpaid"])
        self.assertIn(["Annual Insurance", 600.0, self.today.isoformat()],
                      r["overdue_unpaid"])
        self.assertAlmostEqual(r["fixed_unpaid_due"], 720.0, places=2)

    def test_mtd_by_category_and_forecast_scenarios(self):
        self._config(budgeted_income_monthly=6000)
        r = self.client.get("/api/today/full").json()
        self.assertIn(["GENERAL MERCHANDISE", 45.0], r["mtd"])
        self.assertEqual(r["amazon_subs"], [])
        self.assertEqual(r["amazon_total"], 0)
        fc = r["forecast"]
        self.assertIsNotNone(fc)
        # the product exposes exactly TWO card scenarios — pay all
        # cards now and autopay statement. The engine's full-balance and
        # no-cards series stay internal (email prose reads them directly).
        for scen in ("pace_now", "pace_stmt"):
            for key in ("series", "min", "min_date", "negative_date", "end"):
                self.assertIn(key, fc[scen])
            self.assertIsInstance(fc[scen]["series"], list)
        self.assertNotIn("pace", fc)
        self.assertNotIn("pace_nocards", fc)

    def test_variable_scale_when_income_is_tight(self):
        # dynamic budget on, income barely over the bill schedule → variable
        # budgets compress (shrink-only) and the scale block explains it
        self._config(budgeted_income_monthly=1500,
                     dynamic_variable_budget=True)
        r = self.client.get("/api/today/full").json()
        sc = r["variable_scale"]
        self.assertIsNotNone(sc)
        self.assertAlmostEqual(sc["static_total"], 2000.0, places=2)
        self.assertAlmostEqual(sc["available"], 1500 - 720.0, places=2)
        self.assertAlmostEqual(r["variable_budget"], 780.0, places=2)
        # standing surplus uses STATIC budgets and evened bills — it reports
        # the plan itself over-committed (income 1500 < budgets 2000 alone),
        # which the month-specific compression above merely papers over
        self.assertAlmostEqual(
            r["plan_surplus"], 1500 - r["avg_bills"] - 2000.0, places=2)
        self.assertLess(r["plan_surplus"], 0)


class RecurringListGapTests(unittest.TestCase):
    """Gaps 1, 3, 4: archived listing, per-bill month stats, 💡 fit hint."""

    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.tid = _signup(cls.client, "rlg")
        cls.today = dt.date.today()
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # occurrence bill, paid this month
            add_bill(conn, "Fitness Club", 50, frequency="MONTHLY",
                     next_due=cls.today.isoformat())
            add_txn(conn, cls.today, 50.0, "Fitness Club")
            # envelope bill: $100/mo pool, two charges → full + $20 overflow
            add_bill(conn, "Coffee Pool", 100, bill_type="envelope",
                     frequency=None)
            add_txn(conn, cls.today, 60.0, "Coffee Pool")
            add_txn(conn, cls.today, 60.0, "Coffee Pool")
            # income bill — no occurrence stats, keys still present
            add_bill(conn, "Acme Payroll", 2000, frequency="WEEKLY",
                     interval=2, income=True,
                     next_due=cls.today.isoformat())
            # configured $15/mo but the ledger shows ~$22.99 weekly → fit
            # diverges, 💡 hint material
            add_bill(conn, "Web Widget", 15, frequency="MONTHLY",
                     next_due=cls.today.isoformat())
            for back in (28, 21, 14, 7, 0):
                add_txn(conn, cls.today - dt.timedelta(days=back), 22.99,
                        "Web Widget")
        finally:
            conn.close()
        # archived bill via the real archive route (soft remove)
        cls.client.post("/api/bills/save", json={
            "payee": "Old Paper", "amount": "9.50", "cadence": "MONTHLY:1"})
        cls.client.post("/api/bills/delete", json={"payee": "Old Paper"})

    def _bills(self):
        r = self.client.get("/api/bills").json()
        return r, {b["payee"]: b for b in r["bills"]}

    def test_archived_listing(self):
        r, bills = self._bills()
        self.assertNotIn("Old Paper", bills)
        row = next(a for a in r["archived"] if a["payee"] == "Old Paper")
        self.assertAlmostEqual(row["amount"], -9.5)   # signed, as stored
        self.assertEqual(row["frequency"], "EVERY_MONTH")
        self.assertTrue(row["synced_at"])             # ISO timestamp
        # restore lights the bill back up and empties the archive
        self.client.post("/api/bills/restore", json={"payee": "Old Paper"})
        r2, bills2 = self._bills()
        self.assertIn("Old Paper", bills2)
        self.assertEqual([a for a in r2["archived"]
                          if a["payee"] == "Old Paper"], [])
        self.client.post("/api/bills/delete", json={"payee": "Old Paper"})

    def test_occurrence_stats(self):
        _, bills = self._bills()
        gym = bills["Fitness Club"]
        self.assertEqual(gym["occurrences"], 1)
        self.assertEqual(gym["paid"], 1)              # Paid 1/1
        self.assertAlmostEqual(gym["paid_amount"], 50.0, places=2)
        self.assertIsNone(gym["used"])                # not an envelope
        self.assertIsNone(gym["next_unpaid"])
        # income bill: runs the occurrence engine on the deposit side —
        # occurrences expand, nothing posted so
        # nothing received; test_recurring_income_received covers the
        # received path
        pay = bills["Acme Payroll"]
        self.assertGreaterEqual(pay["occurrences"], 1)
        self.assertEqual(pay["paid"], 0)
        self.assertIsNone(pay["used"])

    def test_envelope_stats(self):
        _, bills = self._bills()
        env = bills["Coffee Pool"]
        self.assertEqual(env["occurrences"], 0)
        self.assertAlmostEqual(env["used"], 100.0, places=2)      # Used (cap)
        self.assertAlmostEqual(env["overflow"], 20.0, places=2)   # over +$20
        self.assertEqual(env["paid"], 2)                          # Chgs
        self.assertAlmostEqual(env["paid_amount"], 120.0, places=2)  # Paid $

    def test_fit_hint_and_dismissal_memory(self):
        _, bills = self._bills()
        w = bills["Web Widget"]
        self.assertIsNotNone(w["fit"])
        self.assertEqual(w["fit"]["kind"], "occurrence")
        self.assertAlmostEqual(w["fit"]["amount"], 22.99, places=2)
        self.assertTrue(w["fit"]["short"].startswith("~$"))
        self.assertTrue(w["fit_diverges"])            # $15 config vs ~$23 fit
        self.assertFalse(w["hint_dismissed"])
        # a bill the ledger agrees with raises no hint noise
        self.assertFalse(bills["Fitness Club"]["fit_diverges"])
        # dismiss → flag flips in the listing; restore → hint returns
        self.client.post("/api/bills/hint",
                         json={"payee": "Web Widget", "action": "dismiss"})
        _, bills = self._bills()
        self.assertTrue(bills["Web Widget"]["hint_dismissed"])
        self.client.post("/api/bills/hint",
                         json={"payee": "Web Widget", "action": "restore"})
        _, bills = self._bills()
        self.assertFalse(bills["Web Widget"]["hint_dismissed"])


if __name__ == "__main__":
    unittest.main()
