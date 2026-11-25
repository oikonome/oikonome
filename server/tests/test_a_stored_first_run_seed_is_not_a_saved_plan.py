"""Once a finished month is on the ledger, the first read of the settings
stores the app's first-run budget seed in the settings row — and the
onboarding read itself makes that read. Every reader of the row then sees
food and everything-else figures, so `budgets_set` holds before the
household has chosen anything. The setup wizard must not take that seed
for a plan the household saved: it would open on the machine's guess,
skip history's category rows and let Continue leave a step where nothing
was saved. `budgets_saved_by_user` is that answer, and only a person's
save of the plan makes it true. A plan saved before the mark existed
still counts, through the wizard's own step mark or a part of the plan
only a person writes."""
import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db, add_txn, seed_accounts


class FirstRunSeedIsNotASavedPlan(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        self.client = TestClient(self.appmod.app)
        self.client.post("/api/signup", data={
            "email": f"seed-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        # four months of steady spend: the seed is no longer provisional
        today = dt.date.today()
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            for months_back in range(4):
                d = today - dt.timedelta(days=12 + 31 * months_back)
                add_txn(conn, d, 140.0, "Pine Grocer",
                        primary="FOOD_AND_DRINK")
                add_txn(conn, d, 260.0, "Cedar Hardware",
                        primary="HOME_IMPROVEMENT")
        finally:
            conn.close()

    def _stored(self) -> dict:
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                "SELECT config FROM tenant_settings").fetchone()
            return dict(row["config"]) if row else {}
        finally:
            conn.close()

    def _onb(self) -> dict:
        r = self.client.get("/api/onboarding")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_a_stored_seed_reads_as_no_plan_saved(self):
        body = self._onb()
        stored = self._stored()
        # the read stored the seed: every reader of the row sees a budget
        self.assertGreater(stored.get("food_monthly") or 0, 0)
        self.assertTrue(body["budgets_set"])
        # …and none of it is the household's
        self.assertFalse(body["budgets_saved_by_user"])
        self.assertFalse(self._onb()["budgets_saved_by_user"])

    def test_a_planner_save_reads_as_saved(self):
        self._onb()
        r = self.client.post("/api/settings", json={
            "food_monthly": 450, "other_monthly": 700,
            "custom_buckets": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self._onb()["budgets_saved_by_user"])

    def test_an_unrelated_save_is_not_a_saved_plan(self):
        self._onb()
        r = self.client.post("/api/settings", json={"theme": "dark"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self._onb()["budgets_saved_by_user"])

    def test_a_plan_saved_before_the_mark_still_counts(self):
        self._onb()
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["wizard_steps"] = {"budgets": "done"}
        finally:
            conn.close()
        self.assertTrue(self._onb()["budgets_saved_by_user"])
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["wizard_steps"] = {}
                cfg["custom_buckets"] = [
                    {"name": "Fuel", "parent": "other", "monthly": 90,
                     "categories": ["TRANSPORTATION"], "merchants": []}]
        finally:
            conn.close()
        self.assertTrue(self._onb()["budgets_saved_by_user"])


if __name__ == "__main__":
    unittest.main()
