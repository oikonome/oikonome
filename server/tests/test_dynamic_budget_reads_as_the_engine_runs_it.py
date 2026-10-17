"""The settings view reports the dynamic variable budget the way the
budget engine obeys it: a household whose config never stored the key
runs with dynamic budgeting on, so the view must say on.

Sent as the raw stored value, a missing key arrives as null; the clients
read null as off, show the box unchecked, and post that false back with
the next bucket save, switching the feature off without anyone touching
the toggle. A stored false still reads as false, and a save that does not
name the key leaves it unstored (on)."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db, seed_accounts


def _make_client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


class DynamicBudgetDefaultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.client.post("/api/signup", data={
            "email": f"dyn-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
        finally:
            conn.close()

    def _store(self, cfg: dict) -> None:
        conn = tenancy.tenant_connect(self.tid)
        try:
            budget.save_config(conn, cfg)
        finally:
            conn.close()

    def _stored(self) -> dict:
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute("SELECT config FROM tenant_settings").fetchone()
            return dict(row["config"])
        finally:
            conn.close()

    def test_an_unstored_key_reads_as_on(self):
        # budgets set, the dynamic key never written: the engine runs it on
        self._store({"food_monthly": 600, "other_monthly": 400})
        r = self.client.get("/api/settings")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(r.json()["dynamic_variable_budget"], True)

    def test_a_bucket_save_does_not_switch_it_off(self):
        self._store({"food_monthly": 600, "other_monthly": 400})
        r = self.client.post("/api/settings", json={"food_monthly": 650})
        self.assertEqual(r.status_code, 200, r.text)
        # the save reply is a settings view too, and clients seed from it
        self.assertIs(r.json()["dynamic_variable_budget"], True)
        self.assertIsNot(self._stored().get("dynamic_variable_budget"), False)
        self.assertIs(
            self.client.get("/api/settings").json()["dynamic_variable_budget"],
            True)

    def test_a_stored_off_still_reads_as_off(self):
        self._store({"food_monthly": 600, "other_monthly": 400,
                     "dynamic_variable_budget": False})
        self.assertIs(
            self.client.get("/api/settings").json()["dynamic_variable_budget"],
            False)


if __name__ == "__main__":
    unittest.main()
