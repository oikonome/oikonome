"""Route-level regressions:
- NaN/Infinity poisoning of DOUBLE columns (permanent 500s on later reads)
- archived-entity read-only bypass via account/transaction assignment."""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


class NumericPoisoningRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"poison-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute("INSERT INTO items (id,aggregator) "
                         "VALUES ('manual','manual') "
                         "ON CONFLICT (tenant_id,id) DO NOTHING")
            conn.execute("INSERT INTO accounts (id,item_id,name,type) "
                         "VALUES ('np-manual','manual','Cash','depository')")
            conn.execute(
                """INSERT INTO transactions (id,account_id,date,amount,name,
                       pending,removed)
                   VALUES ('np-t','chk','2026-07-11',12.0,'X',0,0)""")
        finally:
            conn.close()

    # --- NaN balance must not poison /api/accounts -------------------------
    def test_manual_balance_rejects_nan(self):
        r = self.client.post("/accounts/balance",
                             data={"account_id": "np-manual", "balance": "nan"},
                             follow_redirects=False)
        self.assertIn(r.status_code, (200, 303))
        # the poison would surface here: /api/accounts 500s on a NaN row
        acc = self.client.get("/api/accounts")
        self.assertEqual(acc.status_code, 200, acc.text)
        row = next((a for a in acc.json()["accounts"]
                    if a["id"] == "np-manual"), None)
        self.assertIsNotNone(row)
        self.assertIsNone(row["balance_current"])

    # --- NaN reimburse expectation → 400 -----------------------------------
    # (send the raw JSON literal — httpx refuses NaN via json=, but Starlette's
    #  json.loads accepts it, which is exactly the attack surface.)
    _JSON = {"content-type": "application/json"}

    def test_reimburse_flag_rejects_nan(self):
        r = self.client.post("/api/reimburse/np-t/flag",
                             content='{"expected": NaN}', headers=self._JSON)
        self.assertEqual(r.status_code, 400, r.text)

    # --- NaN settings number → 400, not a 500 ------------------------------
    def test_settings_rejects_nan_number(self):
        r = self.client.post("/api/settings",
                             content='{"food_monthly": Infinity}',
                             headers=self._JSON)
        self.assertEqual(r.status_code, 400, r.text)

    # --- assigning an account/txn INTO an archived entity → 409 ------------
    def test_assign_into_archived_entity_blocked(self):
        eid = self.client.post("/api/business/entities", json={
            "name": "Widgets LLC", "structure": "sole_prop"}).json()["id"]
        self.assertEqual(self.client.post(
            f"/api/business/entities/{eid}/status",
            json={"status": "archived"}).status_code, 200)
        acc = self.client.post("/api/accounts/np-manual/entity",
                              json={"entity_id": eid})
        self.assertEqual(acc.status_code, 409, acc.text)
        txn = self.client.post("/api/transactions/np-t/entity",
                              json={"entity_id": eid})
        self.assertEqual(txn.status_code, 409, txn.text)
        # clearing an assignment must still work on an archived entity
        clr = self.client.post("/api/transactions/np-t/entity",
                              json={"entity_id": None})
        self.assertEqual(clr.status_code, 200, clr.text)


if __name__ == "__main__":
    unittest.main()
