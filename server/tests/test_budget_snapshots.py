"""Budget history: closed months are judged against the budget frozen FOR
them, and backfill is the owner's explicit opt-in to retroactive judgment
for months that predate snapshots — rows stay source='backfill' so the
history card can say which verdicts were chosen after the fact. The
/api/budget/snapshots pair feeds the Budget page's card."""

import datetime as dt
import unittest
import uuid

from oikonome.engine import budget
from oikonome.web import lenses

from .util import (TODAY, add_bill, add_txn, make_db, seed_accounts,
                   write_config)


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=3000, other_monthly=3100)

    def tearDown(self):
        self.conn.close()

    def test_backfill_freezes_only_missing_closed_months(self):
        add_txn(self.conn, dt.date(2026, 3, 5), 50.0, "FIRST")
        budget.snapshot_month(self.conn, dt.date(2026, 6, 1))  # June frozen
        n = budget.backfill_snapshots(self.conn, TODAY)
        self.assertEqual(n, 3)                    # Mar, Apr, May
        self.assertEqual(budget.snapshot_months(self.conn, 2026),
                         {3, 4, 5, 6})            # July (current) untouched
        rows = {r["month"]: r["source"] for r in self.conn.execute(
            "SELECT month, source FROM budget_snapshots WHERE year=2026")}
        self.assertEqual(rows[3], "backfill")
        self.assertEqual(rows[6], "close")        # never relabeled
        # the year grid now judges the backfilled months
        y = lenses.year_summary(self.conn, 2026, today=TODAY)
        self.assertEqual([c["status"] for c in y["cells"][2:7]],
                         ["final"] * 4 + ["projected"])

    def test_backfill_is_idempotent(self):
        add_txn(self.conn, dt.date(2026, 5, 5), 50.0, "FIRST")
        self.assertEqual(budget.backfill_snapshots(self.conn, TODAY), 2)
        self.assertEqual(budget.backfill_snapshots(self.conn, TODAY), 0)

    def test_malformed_snapshot_is_absent_not_a_crash(self):
        """A snapshot whose budgets aren't numbers (a restored ZIP is the
        one door that bypasses the live save's validation) must read as
        no-snapshot — never a TypeError inside the lens month math."""
        from oikonome.engine.compat import jsonb
        from oikonome.web import lenses
        self.conn.execute(
            "INSERT INTO budget_snapshots (year, month, config) "
            "VALUES (2026, 6, %s)",
            (jsonb({"food_monthly": "x", "other_monthly": 5}),))
        self.assertIsNone(budget.snapshot_config(self.conn, 2026, 6))
        # both lenses render (live-config fallback), no exception
        r = lenses.month_summary(self.conn, 2026, 6, today=TODAY)
        self.assertEqual(r["budget_source"], "live")
        y = lenses.year_summary(self.conn, 2026, today=TODAY)
        self.assertEqual(y["cells"][5]["status"], "final")

    def test_backfill_without_ledger_or_budget_is_a_noop(self):
        self.assertEqual(budget.backfill_snapshots(self.conn, TODAY), 0)
        write_config(self.conn, food_monthly=0, other_monthly=0)
        add_txn(self.conn, dt.date(2026, 3, 5), 50.0, "FIRST")
        self.assertEqual(budget.backfill_snapshots(self.conn, TODAY), 0)


class FrozenScheduleTests(unittest.TestCase):
    """A closed month's fixed-bill picture must not move when the live
    schedule is edited later. The snapshot freezes the active bill rows
    beside the budget; the lenses expand a closed month's schedule from
    those rows, so a bill edit (or an envelope cap change) after the month
    closed cannot re-judge it. Snapshots that predate bill freezing keep
    the old live-table behaviour, flagged bills_source='live'."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=3000, other_monthly=3100)

    def tearDown(self):
        self.conn.close()

    def _freeze_june_with_fitness(self, amount=50.0):
        add_bill(self.conn, "Fitness", amount, next_due=dt.date(2026, 6, 10))
        add_txn(self.conn, dt.date(2026, 6, 10), amount, "FITNESS")
        self.assertTrue(budget.snapshot_month(self.conn,
                                              dt.date(2026, 6, 15)))

    def test_closed_month_keeps_its_frozen_schedule(self):
        self._freeze_june_with_fitness(50.0)
        add_bill(self.conn, "Fitness", 80.0, next_due=dt.date(2026, 6, 10))
        r = lenses.month_summary(self.conn, 2026, 6, today=TODAY)
        self.assertEqual(r["bills_source"], "snapshot")
        self.assertEqual(r["bills"]["planned"], 50.0)

    def test_pre_freeze_snapshot_falls_back_to_live_schedule(self):
        self._freeze_june_with_fitness(50.0)
        self.conn.execute("UPDATE budget_snapshots SET bills = NULL "
                          "WHERE year = 2026 AND month = 6")
        add_bill(self.conn, "Fitness", 80.0, next_due=dt.date(2026, 6, 10))
        r = lenses.month_summary(self.conn, 2026, 6, today=TODAY)
        self.assertEqual(r["bills_source"], "live")
        self.assertEqual(r["bills"]["planned"], 80.0)

    def test_envelope_cap_change_does_not_reach_the_frozen_month(self):
        add_bill(self.conn, "Dining Pool", 200.0, bill_type="envelope")
        add_txn(self.conn, dt.date(2026, 6, 8), 60.0, "DINING POOL")
        self.assertTrue(budget.snapshot_month(self.conn,
                                              dt.date(2026, 6, 15)))
        add_bill(self.conn, "Dining Pool", 400.0, bill_type="envelope")
        r = lenses.month_summary(self.conn, 2026, 6, today=TODAY)
        env = {e["payee"]: e for e in r["bills"]["envelopes"]}
        self.assertEqual(env["Dining Pool"]["monthly"], 200.0)

    def test_snapshot_carries_the_schedule_totals(self):
        self._freeze_june_with_fitness(50.0)
        row = self.conn.execute(
            "SELECT bills FROM budget_snapshots "
            "WHERE year = 2026 AND month = 6").fetchone()
        # recurring_load's evened-out number (the Bills page headline), not
        # a re-derivation: the bill's floor is June, so 7 of 12 months carry
        # an occurrence
        self.assertGreater(row["bills"]["bills_monthly"], 0)
        self.assertEqual(len(row["bills"]["rows"]), 1)
        # backfill freezes the schedule too, under the same opt-in label
        add_txn(self.conn, dt.date(2026, 5, 5), 25.0, "EARLIER")
        n = budget.backfill_snapshots(self.conn, TODAY)
        self.assertGreater(n, 0)
        for r in self.conn.execute(
                "SELECT bills FROM budget_snapshots WHERE source='backfill'"):
            self.assertIsInstance(r["bills"], dict)

    def test_malformed_frozen_rows_read_as_no_schedule(self):
        """A restored ZIP is the door junk arrives through: a bills blob
        that isn't row-shaped must degrade to the live-table fallback,
        never crash the lens."""
        from oikonome.engine.compat import jsonb
        add_bill(self.conn, "Fitness", 80.0, next_due=dt.date(2026, 6, 10))
        self.conn.execute(
            "INSERT INTO budget_snapshots (year, month, config, bills) "
            "VALUES (2026, 6, %s, %s)",
            (jsonb({"food_monthly": 3000, "other_monthly": 3100}),
             jsonb({"rows": "not-a-list"})))
        self.assertIsNone(budget.snapshot_bill_rows(self.conn, 2026, 6))
        r = lenses.month_summary(self.conn, 2026, 6, today=TODAY)
        self.assertEqual(r["bills_source"], "live")
        # one malformed ROW is dropped; the good row still counts
        self.conn.execute(
            "UPDATE budget_snapshots SET bills = %s "
            "WHERE year = 2026 AND month = 6",
            (jsonb({"rows": [{"payee": "Fitness", "amount": -50.0,
                              "merchant": None, "category": None,
                              "raw": {"dueOn": "2026-06-10",
                                      "recurrence": {"freq": "MONTHLY",
                                                     "interval": 1}}},
                             {"payee": 7}]}),))
        rows = budget.snapshot_bill_rows(self.conn, 2026, 6)
        self.assertEqual([r["payee"] for r in rows], ["Fitness"])


class HistoryApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        from .util import _ensure_db
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from fastapi.testclient import TestClient

        from oikonome.db import tenancy
        from oikonome.web.app import app
        cls.client = TestClient(app)
        r = cls.client.post("/api/signup",
                            data={"email": f"hist-{uuid.uuid4().hex[:8]}@x.dev",
                                  "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=3000, other_monthly=3100)
            # ledger floor exactly two closed months back (API uses the
            # real today), mid-month so month arithmetic can't wobble
            today = dt.date.today()
            y, m = today.year, today.month
            for _ in range(2):
                y, m = (y - 1, 12) if m == 1 else (y, m - 1)
            add_txn(conn, dt.date(y, m, 15), 50.0, "OLD SPEND")
        finally:
            conn.close()

    def test_history_then_backfill_round_trip(self):
        r = self.client.get("/api/budget/snapshots").json()
        self.assertEqual(r["months"], [])
        self.assertEqual(r["missing"], 2)
        self.assertTrue(r["budget_set"])
        b = self.client.post("/api/budget/snapshots/backfill").json()
        self.assertEqual(b["backfilled"], 2)
        r2 = self.client.get("/api/budget/snapshots").json()
        self.assertEqual(r2["missing"], 0)
        self.assertEqual(len(r2["months"]), 2)
        top = r2["months"][0]                      # newest first
        self.assertEqual(top["variable_budget"], 6100.0)
        self.assertEqual(top["source"], "backfill")
        self.assertIn("captured_at", top)
        # the schedule freezes alongside (no bills in this tenant → $0/mo)
        self.assertEqual(top["bills_monthly"], 0.0)
