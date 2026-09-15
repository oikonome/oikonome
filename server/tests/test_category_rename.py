"""Rename free-form / custom categories everywhere they appear."""
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.web import data

from .test_llm_categorize import add_raw_txn, cache
from .util import _ensure_db, make_db, seed_accounts


class CategoryRenameUnitTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        seed_accounts(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_renames_override_primary_rule_and_bucket(self):
        add_raw_txn(self.conn, "t1", "2025-07-01", 12, "BODEGA",
                    raw={}, override="Hobby Farm")
        add_raw_txn(self.conn, "t2", "2025-06-01", 15, "BODEGA",
                    primary="Hobby Farm", raw={})
        cache(self.conn, "BODEGA", "Hobby Farm")
        with budget.config_txn(self.conn) as cfg:
            cfg["custom_buckets"] = [{
                "name": "Hobby Farm", "parent": "other", "monthly": 50,
                "categories": ["Hobby Farm"], "merchants": []}]

        r = data.rename_category(self.conn, "Hobby Farm", "Farm Stuff")
        self.assertEqual(r["overrides"], 1)
        self.assertEqual(r["primaries"], 1)
        self.assertEqual(r["rules"], 1)
        self.assertGreaterEqual(r["buckets"], 1)

        self.assertEqual(
            self.conn.execute(
                "SELECT category_override FROM transactions WHERE id='t1'"
            ).fetchone()["category_override"], "Farm Stuff")
        self.assertEqual(
            self.conn.execute(
                "SELECT category_primary FROM transactions WHERE id='t2'"
            ).fetchone()["category_primary"], "Farm Stuff")
        self.assertEqual(
            self.conn.execute(
                "SELECT category_primary FROM merchant_categories "
                "WHERE merchant='BODEGA'"
            ).fetchone()["category_primary"], "Farm Stuff")
        buckets = budget.load_config(self.conn).get("custom_buckets") or []
        self.assertEqual(buckets[0]["name"], "Farm Stuff")
        self.assertEqual(buckets[0]["categories"], ["Farm Stuff"])

    def test_refuses_standard_plaid_categories(self):
        with self.assertRaises(ValueError) as cm:
            data.rename_category(self.conn, "TRANSFER_OUT", "Xfer")
        self.assertIn("standard Plaid", str(cm.exception))
        with self.assertRaises(ValueError):
            data.rename_category(self.conn, "FOOD_AND_DRINK", "Groceries")
        with self.assertRaises(ValueError):
            data.rename_category(self.conn, "GENERAL_MERCHANDISE", "Shopping")
        # Folding a custom name *into* a Plaid primary is still fine
        add_raw_txn(self.conn, "t-fold", "2025-07-01", 9, "X",
                    raw={}, override="My Snacks")
        r = data.rename_category(self.conn, "My Snacks", "FOOD_AND_DRINK")
        self.assertEqual(r["overrides"], 1)

    def test_noop_when_names_match(self):
        r = data.rename_category(self.conn, "Same", "Same")
        self.assertEqual(r["overrides"], 0)


class CategoryRenameApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"catren-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def test_api_renames_override(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            add_raw_txn(conn, f"t-{uuid.uuid4().hex[:8]}", "2025-07-01", 12,
                        "SHOP", raw={}, override="Old Name")
        finally:
            conn.close()
        r = self.client.post("/api/categories/rename",
                             json={"old": "Old Name", "new": "New Name"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreaterEqual(r.json()["overrides"], 1)


if __name__ == "__main__":
    unittest.main()
