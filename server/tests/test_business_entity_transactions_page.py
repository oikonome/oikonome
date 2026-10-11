"""The business worksheet list pages when asked, and only when asked.

`GET /api/business/entities/{id}/transactions` with no paging parameters
keeps the reply older clients read: every row under `transactions` and
nothing else. With `limit`/`offset` it returns one page and `total`, the
row count of the whole scope, so an entity with years of history need not
ship all of it to paint one screen.
"""
import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import entities

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config


class EntityTransactionsPage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"bizpage-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cls.eid = entities.create_entity(
                conn, name="Paging Venture LLC", structure="sole_prop")["id"]
            entities.assign_account(conn, "card", cls.eid)
            for i in range(3):
                add_txn(conn, TODAY - dt.timedelta(days=i), 10.0 + i,
                        f"SUPPLY STORE {i}", account="card")
        finally:
            conn.close()

    def _get(self, **params):
        r = self.client.get(
            f"/api/business/entities/{self.eid}/transactions", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_default_reply_is_every_row_in_the_old_shape(self):
        body = self._get()
        self.assertEqual(set(body), {"transactions"})
        self.assertEqual(len(body["transactions"]), 3)

    def test_limit_and_offset_page_with_a_total(self):
        every = [t["id"] for t in self._get()["transactions"]]
        first = self._get(limit=2)
        self.assertEqual(first["total"], 3)
        self.assertEqual([t["id"] for t in first["transactions"]], every[:2])
        rest = self._get(limit=2, offset=2)
        self.assertEqual(rest["total"], 3)
        self.assertEqual([t["id"] for t in rest["transactions"]], every[2:])

    def test_a_limit_out_of_range_is_refused(self):
        r = self.client.get(
            f"/api/business/entities/{self.eid}/transactions",
            params={"limit": 0})
        self.assertNotEqual(r.status_code, 200)
        self.assertLess(r.status_code, 500)


if __name__ == "__main__":
    unittest.main()
