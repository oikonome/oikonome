"""Setup is complete once its steps exist: accounts, a ledger, bills, budgets
and a primary checking account, whether or not the wizard's last step was
pressed, so Settings → Wizards does not offer "Finish setup" for it. The
onboarding read decides this once, using the Today checklist's own steps,
and dismissing the wizard still counts as complete."""
import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import TODAY, _ensure_db, add_bill, add_txn, seed_accounts


class SetupComplete(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"setup-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def _onb(self):
        r = self.client.get("/api/onboarding")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_a_fresh_household_is_not_complete(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
            cfg.pop("wizard_done", None)
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        # runs before the test below fills the household in (name order)
        body = self._onb()
        self.assertEqual(body["accounts"], 0)
        self.assertFalse(body["setup_complete"])

    def test_every_step_in_place_is_complete_without_the_wizards_mark(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            add_txn(conn, TODAY - dt.timedelta(days=3), 12.5, "Pine Grocer")
            add_bill(conn, "Pine Power", 80.0)
            cfg = budget.load_config(conn)
            cfg.update({"food_monthly": 600, "other_monthly": 900,
                        "checking_account_id": "chk",
                        "wizard_done": False, "wizard_steps": {}})
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        body = self._onb()
        self.assertFalse(body["wizard_done"])
        self.assertTrue(body["setup_complete"])


if __name__ == "__main__":
    unittest.main()
