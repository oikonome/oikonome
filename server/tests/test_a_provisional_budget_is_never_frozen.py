"""The loader's unsaved first-month budget guess is never frozen into a
closed month, nor offered as "apply my current budget" to past months.

Under a month of history the settings loader fills in a provisional
budget seed it never saves — the app's guess, not a plan the household
made. The month-close freeze, the backfill and the history card's
"budget set" all read the loaded settings, so a household that never set
a budget had its first closed month judged against that guess, and was
offered to stamp the guess over every month of its ledger.
"""

import datetime as dt
import unittest
import uuid

from oikonome.engine import budget
from oikonome.jobs import worker

from .util import _ensure_db, add_txn, make_db, seed_accounts

TODAY = dt.date.today()
MONTH_START = TODAY.replace(day=1)
LAST_MONTH_END = MONTH_START - dt.timedelta(days=1)


def _short_ledger(conn):
    """Spend from ten days before this month began through yesterday: a
    month of history that includes no complete closed month."""
    day = MONTH_START - dt.timedelta(days=10)
    while day < TODAY:
        add_txn(conn, day, 40.0, "CORNER GROCER", primary="FOOD_AND_DRINK")
        add_txn(conn, day, 60.0, "HARDWARE BARN")
        day += dt.timedelta(days=1)


class ProvisionalBudgetEngineTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _short_ledger(self.conn)
        # the premise: the loader carries a guess, and nothing is saved
        cfg = budget.load_config(self.conn)
        self.assertGreater((cfg.get("food_monthly") or 0)
                           + (cfg.get("other_monthly") or 0), 0)
        self.assertFalse(budget.budgets_set(self.conn))

    def _snapshots(self):
        return self.conn.execute(
            "SELECT count(*) AS n FROM budget_snapshots").fetchone()["n"]

    def test_the_month_close_does_not_freeze_the_guess(self):
        self.assertFalse(budget.snapshot_month(self.conn, LAST_MONTH_END))
        self.assertFalse(worker._close_last_month(self.conn))
        self.assertEqual(self._snapshots(), 0)

    def test_the_backfill_does_not_stamp_the_guess_on_past_months(self):
        self.assertEqual(budget.backfill_snapshots(self.conn, TODAY), 0)
        self.assertEqual(self._snapshots(), 0)

    def test_a_saved_budget_is_still_frozen(self):
        budget.save_config(self.conn, {**budget.load_config(self.conn),
                                       "food_monthly": 1200,
                                       "other_monthly": 1800})
        self.assertTrue(budget.snapshot_month(self.conn, LAST_MONTH_END))


class ProvisionalBudgetHistoryApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from fastapi.testclient import TestClient

        from oikonome.db import tenancy
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"prov-{uuid.uuid4().hex[:8]}@x.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            _short_ledger(conn)
        finally:
            conn.close()

    def test_the_history_card_does_not_offer_the_guess(self):
        r = self.client.get("/api/budget/snapshots").json()
        self.assertFalse(r["budget_set"])
        b = self.client.post("/api/budget/snapshots/backfill").json()
        self.assertEqual(b["backfilled"], 0)


if __name__ == "__main__":
    unittest.main()
