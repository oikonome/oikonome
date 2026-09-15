"""Route-level input validation: bad input answers 400/404, never a 500,
and the classify route refuses a write to an archived entity."""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


class RouteInputValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"riv-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute(
                """INSERT INTO transactions (id, account_id, date, amount,
                       name, pending, removed)
                   VALUES ('riv-t','chk','2026-07-11',12.0,'X',0,0)""")
        finally:
            conn.close()

    # --- malformed date query must 400, not 500 ---------------------------
    def test_transactions_bad_date_is_400(self):
        r = self.client.get("/api/transactions?date_from=not-a-date")
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/transactions?date_to=2026-13-99")
        self.assertEqual(r.status_code, 400)
        # a well-formed date still works
        r = self.client.get("/api/transactions?date_from=2026-01-01")
        self.assertEqual(r.status_code, 200)

    # --- out-of-range year must 400, not crash date construction ----------
    def test_estimated_tax_year_out_of_range_is_400(self):
        eid = self.client.post("/api/business/entities", json={
            "name": "Widgets LLC", "structure": "sole_prop"}).json()["id"]
        r = self.client.get(
            f"/api/business/entities/{eid}/estimated-tax?year=9999")
        self.assertEqual(r.status_code, 400)

    # --- malformed entity id on the worksheet must 404, not surface the
    #     Postgres uuid-cast error as a 500 (every sibling already 404s) --
    def test_business_transactions_bad_entity_id_is_404(self):
        r = self.client.get("/api/business/entities/not-a-uuid/transactions")
        self.assertEqual(r.status_code, 404, r.text)

    # --- classify on an archived (read-only) entity must 409 --------------
    def test_classify_on_archived_entity_is_409(self):
        eid = self.client.post("/api/business/entities", json={
            "name": "Archived LLC", "structure": "sole_prop"}).json()["id"]
        # classify verifies the transaction belongs to the URL's entity,
        # so hand this one to the entity first
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("UPDATE transactions SET entity_id=%s "
                         "WHERE id='riv-t'", (eid,))
        finally:
            conn.close()
        # a live entity classifies fine
        ok = self.client.post(
            f"/api/business/entities/{eid}/transactions/riv-t/class",
            json={"bucket": "operating"})
        self.assertEqual(ok.status_code, 200)
        # archive it, then the same write must be rejected
        arch = self.client.post(f"/api/business/entities/{eid}/status",
                                json={"status": "archived"})
        self.assertEqual(arch.status_code, 200)
        blocked = self.client.post(
            f"/api/business/entities/{eid}/transactions/riv-t/class",
            json={"bucket": "operating"})
        self.assertEqual(blocked.status_code, 409)


if __name__ == "__main__":
    unittest.main()
