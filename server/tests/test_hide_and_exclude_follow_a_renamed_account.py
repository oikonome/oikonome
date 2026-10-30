"""Hide and exclude must not report success for an account a reconnect
adoption is renaming.

An adoption copies the account row to its new id, repoints every child
row and deletes the old one, all under a lock on the old row. A Hide or
Exclude pressed in that window addresses the old id. If the door checks
the id without the lock and writes afterwards, the write lands on a row
that is gone (hide) or on a list the adoption already rewrote (exclude),
and the person is told the account is hidden or excluded while the
renamed account goes on counting toward spend. The door must wait for
the rename and then answer 404, and the renamed account must keep the
state it had.
"""

import os
import threading
import time
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db, seed_accounts, write_config


class HideAndExcludeDuringRenameTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"rename-race-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def setUp(self):
        self.old = f"old-{uuid.uuid4().hex[:6]}"
        self.new = f"new-{uuid.uuid4().hex[:6]}"
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                "INSERT INTO accounts (id, item_id, name, type, subtype, "
                "balance_current) VALUES (%s,'it1','Race Checking',"
                "'depository','checking',100)", (self.old,))
        finally:
            conn.close()

    def _race(self, path, body):
        """Hold the old row as an adoption does, fire the request, then
        rename the account and commit. Returns the response."""
        racer = tenancy.tenant_connect(self.tid)
        self.addCleanup(racer.close)
        out = {}
        with racer.transaction():
            racer.execute("SELECT 1 FROM accounts WHERE id=%s FOR UPDATE",
                          (self.old,))
            t = threading.Thread(
                target=lambda: out.setdefault(
                    "r", self.client.post(path, json=body)))
            t.start()
            time.sleep(0.6)            # let the request reach the row
            racer.execute(
                "INSERT INTO accounts (id, item_id, name, type, subtype, "
                "balance_current, user_removed_at) "
                "SELECT %s, item_id, name, type, subtype, balance_current, "
                "user_removed_at FROM accounts WHERE id=%s",
                (self.new, self.old))
            racer.execute("DELETE FROM accounts WHERE id=%s", (self.old,))
        t.join(timeout=15)
        self.assertFalse(t.is_alive(), "request never finished")
        return out["r"]

    def test_hiding_an_account_mid_rename_is_a_404_not_hidden(self):
        r = self._race(f"/api/accounts/{self.old}/hidden", {"hidden": True})
        self.assertEqual(r.status_code, 404, r.text)
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                "SELECT user_removed_at FROM accounts WHERE id=%s",
                (self.new,)).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row["user_removed_at"])

    def test_excluding_an_account_mid_rename_is_a_404_not_excluded(self):
        r = self._race("/api/accounts/exclude",
                       {"account_id": self.old, "excluded": True})
        self.assertEqual(r.status_code, 404, r.text)
        conn = tenancy.tenant_connect(self.tid)
        try:
            cur = budget.load_config(conn).get("excluded_accounts") or []
        finally:
            conn.close()
        self.assertNotIn(self.old, cur)
        self.assertNotIn(self.new, cur)

    def test_a_present_account_still_hides_and_excludes(self):
        r = self.client.post(f"/api/accounts/{self.old}/hidden",
                             json={"hidden": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["hidden"])
        r = self.client.post("/api/accounts/exclude",
                             json={"account_id": self.old, "excluded": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["excluded"])


if __name__ == "__main__":
    unittest.main()
