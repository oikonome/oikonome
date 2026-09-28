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

    def test_split_parts_follow_the_rename(self):
        """A hand-split part under the renamed category must carry the new
        name; left behind, the old category keeps its share of the charge
        in every rollup after the rename said it was gone."""
        from oikonome.engine import splits
        add_raw_txn(self.conn, "t-split", "2025-07-01", 100, "WAREHOUSE",
                    primary="GENERAL_MERCHANDISE", raw={})
        splits.set_split(self.conn, "t-split", [
            {"category": "FOOD_AND_DRINK", "amount": 60},
            {"category": "Hobby Farm", "amount": 40}])
        r = data.rename_category(self.conn, "Hobby Farm", "Farm Stuff")
        self.assertEqual(r["splits"], 1)
        self.assertEqual(splits.for_txn(self.conn, "t-split"), [
            {"category": "FOOD_AND_DRINK", "amount": 60},
            {"category": "Farm Stuff", "amount": 40}])

    def test_split_part_folds_into_a_part_already_under_the_new_name(self):
        """A split never names one category twice: a rename onto a category
        the row already has merges the amounts, and a split that folds to
        one part becomes that category for the whole row."""
        from oikonome.engine import splits
        add_raw_txn(self.conn, "t-3", "2025-07-01", 100, "WAREHOUSE",
                    primary="GENERAL_MERCHANDISE", raw={})
        splits.set_split(self.conn, "t-3", [
            {"category": "FOOD_AND_DRINK", "amount": 50.25},
            {"category": "Hobby Farm", "amount": 20.5},
            {"category": "HOME_IMPROVEMENT", "amount": 29.25}])
        add_raw_txn(self.conn, "t-2", "2025-07-02", 30, "WAREHOUSE",
                    primary="GENERAL_MERCHANDISE", raw={})
        splits.set_split(self.conn, "t-2", [
            {"category": "FOOD_AND_DRINK", "amount": 10},
            {"category": "Hobby Farm", "amount": 20}])
        data.rename_category(self.conn, "Hobby Farm", "FOOD_AND_DRINK")
        self.assertEqual(splits.for_txn(self.conn, "t-3"), [
            {"category": "FOOD_AND_DRINK", "amount": 70.75},
            {"category": "HOME_IMPROVEMENT", "amount": 29.25}])
        self.assertEqual(splits.for_txn(self.conn, "t-2"), [])
        self.assertEqual(
            self.conn.execute(
                "SELECT category_override FROM transactions WHERE id='t-2'"
            ).fetchone()["category_override"], "FOOD_AND_DRINK")

    def test_a_split_merged_by_a_rename_stays_the_persons_answer(self):
        """A rename that folds a split down to one part leaves the whole row
        in that category as a person's own answer (a manual pin, kind
        'user'). Written as a bare override instead, the next bill pass
        reads the row as unclaimed and restamps the bill's category over
        it, and the split it replaced is already gone."""
        import datetime as dt
        from oikonome.engine import bills, splits
        from .util import TODAY, add_bill, add_txn, write_config
        write_config(self.conn)
        tid = add_txn(self.conn, TODAY - dt.timedelta(days=3), 80.00,
                      "PINEWOOD TELECOM", merchant="Pinewood Telecom",
                      primary="GENERAL_SERVICES")
        mid = self.conn.execute(
            "INSERT INTO merchants (name, name_source) "
            "VALUES ('Pinewood Telecom','layer1') RETURNING id").fetchone()["id"]
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                          (mid, tid))
        add_bill(self.conn, "Pinewood Telecom", 80.00, frequency="MONTHLY",
                 next_due=TODAY, merchant="pinewood telecom",
                 txn_category="RENT_AND_UTILITIES",
                 merchants=["Pinewood Telecom"])
        bills.apply_txn_categories(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT override_source FROM transactions WHERE id=%s",
            (tid,)).fetchone()["override_source"], "bill")
        splits.set_split(self.conn, tid, [
            {"category": "Home Internet", "amount": 50},
            {"category": "Home Phone", "amount": 30}])

        data.rename_category(self.conn, "Home Phone", "Home Internet")
        self.assertEqual(splits.for_txn(self.conn, tid), [])
        bills.apply_txn_categories(self.conn)

        r = self.conn.execute(
            """SELECT t.category_override, t.override_source, m.category,
                      m.bill_id, m.transaction_id IS NOT NULL AS pinned
                 FROM transactions t
                 LEFT JOIN manual_categories m ON m.transaction_id = t.id
                WHERE t.id=%s""", (tid,)).fetchone()
        self.assertEqual(r["category_override"], "Home Internet")
        self.assertEqual(r["override_source"], "user")
        self.assertTrue(r["pinned"])
        self.assertEqual(r["category"], "Home Internet")
        self.assertIsNone(r["bill_id"])

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
