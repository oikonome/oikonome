"""Every Today, lens, budget-snapshot, activity, alert and calendar door is
called with a household that fills its reply, and the reply matches the
shape it documents (web/schemas/today.py).

These replies carry the figures people read most, so the household here
is built to reach the parts of each shape a thin fixture leaves empty: a
paycheck, a bill paid this month and one gone overdue, an envelope, two
carve-out budgets (one under Food, one under Other), a savings goal with
money moving into it, several months of history, and a live alert. The
conformance hook (tests/api_conformance.py) validates each reply.
"""

from __future__ import annotations

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from . import api_conformance
from .util import _ensure_db, add_bill, add_txn, seed_accounts, write_config

PW = "correct-horse-battery"
TODAY = dt.date.today()
FIRST = TODAY.replace(day=1)
PREV = (FIRST - dt.timedelta(days=1)).replace(day=1)

CUSTOM_BUCKETS = [
    {"name": "Coffee", "parent": "food", "monthly": 60,
     "merchants": ["Copper Kettle"]},
    {"name": "Pets", "parent": "other", "monthly": 100,
     "merchants": ["Chewy"]},
]
GOAL = {"name": "Trip", "target": 1000, "tokens": ["trip"],
        "monthly_plan": 250, "start_balance": 100, "account_id": "chk"}


def _month_rows(conn, first: dt.date, scale: float = 1.0) -> None:
    day = lambda n: min(first + dt.timedelta(days=n), TODAY)  # noqa: E731
    add_txn(conn, day(0), -2400.0 * scale, "ACME PAYROLL", account="chk",
            primary="INCOME")
    add_txn(conn, day(1), 95.0 * scale, "CITY POWER", merchant="City Power",
            primary="RENT_AND_UTILITIES", account="card")
    add_txn(conn, day(2), 64.2 * scale, "SAFEWAY", merchant="Safeway",
            primary="FOOD_AND_DRINK")
    add_txn(conn, day(3), 18.5 * scale, "COPPER KETTLE CAFE",
            merchant="Copper Kettle", primary="FOOD_AND_DRINK")
    add_txn(conn, day(4), 52.0 * scale, "CHEWY COM", merchant="Chewy",
            primary="GENERAL_MERCHANDISE")
    add_txn(conn, day(5), 140.0 * scale, "ELECTRONICS STORE",
            merchant="Gadget Hut", primary="GENERAL_MERCHANDISE")
    add_txn(conn, day(6), 250.0 * scale, "TRIP SAVINGS", account="chk",
            primary="TRANSFER_OUT")


class TodayAreaShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"today-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=600, other_monthly=500,
                         custom_buckets=CUSTOM_BUCKETS, today_view="detail",
                         savings_goals=[dict(GOAL)])
            back = PREV
            for _ in range(4):
                _month_rows(conn, back, 1.0)
                back = (back - dt.timedelta(days=1)).replace(day=1)
            _month_rows(conn, FIRST, 1.1)
            add_bill(conn, "Acme Payroll", 2400.0, income=True,
                     next_due=FIRST + dt.timedelta(days=14))
            add_bill(conn, "City Power", 95.0, next_due=FIRST
                     + dt.timedelta(days=1), last_seen=FIRST)
            # due before today and never charged: overdue
            add_bill(conn, "Fern Water", 40.0,
                     next_due=max(FIRST, TODAY - dt.timedelta(days=2)))
            add_bill(conn, "Groceries Box", 200.0, bill_type="envelope",
                     next_due=FIRST)
            conn.execute(
                "INSERT INTO alerts_log (kind, severity, message, first_seen,"
                " last_seen) VALUES ('low_balance', 'warn', %s, %s, %s)",
                ("Checking is under $100", TODAY, TODAY))
        finally:
            conn.close()

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _ok(self, method, path, **kw):
        before = sum(api_conformance.validated().values())
        r = self.client.request(method, "/api" + path, **kw)
        self.assertEqual(r.status_code, 200, f"{method} {path}: {r.text}")
        self.assertGreater(sum(api_conformance.validated().values()), before,
                           f"{method} {path}: the reply was not validated")
        return r.json()

    def test_today_live_and_a_past_day(self):
        live = self._ok("GET", "/today/full")
        self.assertTrue(live["is_today"])
        self.assertTrue(live["bill_cards"] or live["plan_rows"])
        self.assertTrue(live["savings_goals"])
        if TODAY.day > 1:
            past = self._ok("GET", f"/today/full?date={FIRST.isoformat()}")
            self.assertFalse(past["is_today"])

    def test_the_glance(self):
        self._ok("GET", "/today/glance")

    def test_the_month_and_year_lenses(self):
        self._ok("GET", f"/lens/month?y={TODAY.year}&m={TODAY.month}")
        self._ok("GET", f"/lens/month?y={PREV.year}&m={PREV.month}")
        self._ok("GET", f"/lens/year?y={TODAY.year}")
        self._ok("GET", f"/lens/year?y={TODAY.year - 1}")

    def test_budget_snapshots_and_suggestions(self):
        self._ok("POST", "/budget/snapshots/backfill", json={})
        self._ok("GET", "/budget/snapshots")
        self._ok("GET", "/budgets/suggest")

    def test_alerts_activity_and_the_calendar(self):
        body = {"kind": "low_balance", "message": "Checking is under $100"}
        self._ok("POST", "/alerts/dismiss", json=body)
        self._ok("GET", "/alerts/history")
        self._ok("POST", "/alerts/restore", json=body)
        self._ok("GET", "/activity")
        self._ok("GET", "/calendar?days=40")


if __name__ == "__main__":
    unittest.main()
