"""Changing a rule's category on the Rules page changes that rule only.

The page hands over the rule's stored key, which is often not a merchant
name the ledger displays. The merchant-history page's bulk write widens its
input to a token family — every descriptor sharing its words — so a rule
keyed on a chain's bare name, sent through that write, would recategorize
and teach the chain's other merchants as well: a store edit moving the
fuel charges with it."""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .test_llm_categorize import add_raw_txn, cache
from .util import _ensure_db, seed_accounts


class RuleEditTouchesOnlyItsRuleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={
            "email": f"rule-{uuid.uuid4().hex[:10]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            add_raw_txn(conn, "w-1", "2026-06-01", 120, "EXAMPLEMART WHSE #123",
                        primary="FOOD_AND_DRINK", raw={})
            add_raw_txn(conn, "g-1", "2026-06-02", 45, "EXAMPLEMART GAS #123",
                        primary="TRANSPORTATION", raw={})
            cache(conn, "EXAMPLEMART", "FOOD_AND_DRINK")
        finally:
            conn.close()

    def test_a_rule_edit_leaves_other_merchants_alone(self):
        r = self.client.post("/api/rules/set", json={
            "merchant": "EXAMPLEMART", "category": "GENERAL_MERCHANDISE"})
        self.assertEqual(r.status_code, 200, r.text)
        conn = tenancy.tenant_connect(self.tid)
        try:
            gas = conn.execute(
                "SELECT category_primary FROM transactions WHERE id='g-1'"
            ).fetchone()["category_primary"]
            self.assertEqual(gas, "TRANSPORTATION")
            taught = {x["merchant"]: x["category_primary"] for x in conn.execute(
                "SELECT merchant, category_primary FROM merchant_categories")}
            self.assertEqual(taught.get("EXAMPLEMART"), "GENERAL_MERCHANDISE")
            self.assertNotIn("EXAMPLEMART GAS #123", taught)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
