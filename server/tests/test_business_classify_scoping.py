"""One entity's classify endpoint must not touch another entity's
transactions. business_txn_class is keyed by (tenant, txn_id) alone, so
without a route-level ownership check Entity A's URL could classify — and,
because the write is an upsert, rewrite — Entity B's rows, including those
of an ARCHIVED (read-only) entity whose own endpoint would refuse the
write with a 409."""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


class ClassifyIsScopedToTheUrlEntity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"bizscope-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        cls.ea = cls.client.post("/api/business/entities", json={
            "name": "Acme LLC", "structure": "sole_prop"}).json()["id"]
        cls.eb = cls.client.post("/api/business/entities", json={
            "name": "Beta LLC", "structure": "sole_prop"}).json()["id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # Beta's transaction, assigned per-row (the same ownership
            # notion books._BIZ_TXN uses: row override first, else the
            # account's assignment)
            conn.execute(
                """INSERT INTO transactions (id, account_id, date, amount,
                       name, entity_id, pending, removed)
                   VALUES ('scope-b', 'chk', '2026-07-11', 40.0,
                           'BETA COST', %s, 0, 0)""", (cls.eb,))
        finally:
            conn.close()

    def _classify(self, entity_id, txn_id="scope-b"):
        return self.client.post(
            f"/api/business/entities/{entity_id}/transactions/{txn_id}/class",
            json={"bucket": "operating"})

    def test_own_entity_classifies_its_transaction(self):
        self.assertEqual(self._classify(self.eb).status_code, 200)

    def test_another_entitys_url_is_refused(self):
        r = self._classify(self.ea)
        self.assertEqual(r.status_code, 404, r.text)

    def test_a_transaction_of_no_entity_is_refused(self):
        r = self._classify(self.eb, txn_id="no-such-txn")
        self.assertEqual(r.status_code, 404, r.text)


if __name__ == "__main__":
    unittest.main()
