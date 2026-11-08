"""The unlinked-capital list is reachable on a hosted instance.

Doctor's instance checks are the operator's job on hosted and the apps hide
them, but the unlinked capital list is the household's own records: the
Business page links to it there, so its doors must answer a hosted owner.
Reading needs nothing more than a sign-in; removing is a write, so the
hosted forced-second-factor gate still applies to it like any other.
"""
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config, TODAY

BASE = "/api/doctor/unlinked-movements"


class UnlinkedCapitalOnHosted(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        cls.email = f"unh-{uuid.uuid4().hex[:8]}@example.dev"
        cls.client.post("/api/signup", data={
            "email": cls.email, "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()
        r = cls.client.post("/api/business/entities",
                            json={"name": "Example Kiln Works",
                                  "structure": "sole_prop"})
        assert r.status_code == 200, r.text
        eid = r.json()["id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            txn = add_txn(conn, TODAY, 64.31, "KILN SHELF SUPPLY")
        finally:
            conn.close()
        r = cls.client.post(f"/api/business/entities/{eid}/reimburse",
                            json={"txn_id": txn, "mode": "contribute"})
        assert r.status_code == 200, r.text
        cls.movement = r.json()["id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            # a delete from before deletes took the movement along
            conn.execute("DELETE FROM transactions WHERE id = %s", (txn,))
        finally:
            conn.close()

    def _waive(self, on: bool):
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET second_factor_waived = %s "
                          "WHERE email = %s", (on, self.email))
        finally:
            admin.close()

    def test_a_hosted_owner_lists_and_removes_an_unlinked_movement(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            self.assertTrue(self.client.get("/api/me").json()["hosted"])
            r = self.client.get(BASE)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual([m["id"] for m in r.json()["movements"]],
                             [self.movement])
            # the hosted gates are on: no factor, no write
            self._waive(False)
            self.assertEqual(
                self.client.delete(f"{BASE}/{self.movement}").status_code,
                403)
            self._waive(True)
            try:
                r = self.client.delete(f"{BASE}/{self.movement}")
                self.assertEqual(r.status_code, 200, r.text)
            finally:
                self._waive(False)
            self.assertEqual(self.client.get(BASE).json()["movements"], [])


if __name__ == "__main__":
    unittest.main()
