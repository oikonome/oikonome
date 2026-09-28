"""Hiding an account must never destroy a balance nothing can write back.

Hiding is advertised as reversible: unhide brings the account back. That
holds for an aggregator-fed account because the next sync writes its balance
again, which is why hiding nulls it (a frozen number would otherwise sit
there looking current).

A manual account and a CSV/statement-imported one have no next sync. Nulling
their balance on hide would make hiding a one-way delete of the only copy the
system has: unhide returning an account reading blank, which the rest of the
app treats as closed/empty (a NULL balance files a fresh manual account under
"archived" and makes the cash runway read zero). Both hide doors — the
/hidden toggle and the remove route's hide mode — are pinned here.
"""

from __future__ import annotations

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts


class HiddenAccountBalanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app

    def setUp(self):
        # signup is rate-limited per IP across the suite
        try:
            from oikonome.web import security
            security._limiter._hits.clear()
        except Exception:
            pass
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={
            "email": f"hb-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = self._conn()
        try:
            seed_accounts(conn)
            # the fixture item is a generic 'test' aggregator; make it the
            # live pull connection this contract distinguishes from manual
            conn.execute("UPDATE items SET aggregator='plaid' WHERE id='it1'")
        finally:
            conn.close()

    def _conn(self):
        return tenancy.tenant_connect(self.tid)

    def _balance(self, account_id):
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT balance_current, user_removed_at FROM accounts "
                "WHERE id=%s", (account_id,)).fetchone()
        finally:
            conn.close()
        return row

    def _manual_account(self, balance="1234.56"):
        r = self.client.post("/api/accounts/add", json={
            "name": f"Shoebox {uuid.uuid4().hex[:4]}",
            "kind": "depository/cash", "balance": balance})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["account_id"]

    def test_hiding_a_manual_account_keeps_its_balance(self):
        aid = self._manual_account()
        r = self.client.post(f"/api/accounts/{aid}/hidden",
                             json={"hidden": True})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._balance(aid)
        self.assertIsNotNone(row["user_removed_at"], "it must still be hidden")
        self.assertEqual(float(row["balance_current"]), 1234.56,
                         "nothing will ever refill a manual balance — "
                         "hiding must not be a one-way delete")

    def test_unhiding_a_manual_account_returns_it_with_its_balance(self):
        aid = self._manual_account("900")
        self.client.post(f"/api/accounts/{aid}/hidden", json={"hidden": True})
        r = self.client.post(f"/api/accounts/{aid}/hidden",
                             json={"hidden": False})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._balance(aid)
        self.assertIsNone(row["user_removed_at"])
        self.assertEqual(float(row["balance_current"]), 900.0)

    def test_remove_mode_hide_keeps_a_manual_balance_too(self):
        aid = self._manual_account("42")
        r = self.client.post(f"/api/accounts/{aid}/remove",
                             json={"mode": "hide"})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._balance(aid)
        self.assertIsNotNone(row["user_removed_at"])
        self.assertEqual(float(row["balance_current"]), 42.0)

    def test_hiding_an_aggregator_account_still_nulls_its_balance(self):
        """The other half of the rule: a fed account's stored balance goes
        stale the moment it stops syncing, and the next sync refills it."""
        r = self.client.post("/api/accounts/chk/hidden", json={"hidden": True})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._balance("chk")
        self.assertIsNotNone(row["user_removed_at"])
        self.assertIsNone(row["balance_current"])

    def test_hiding_an_account_on_an_archived_connection_keeps_its_balance(self):
        """A Plaid account whose connection was disconnected (archived,
        token released) will never sync again — exactly like a manual
        account, however live its aggregator name sounds. Nulling that
        balance would be the same permanent loss the manual rule exists
        to prevent, and unhide's "fills in on the next sync" would be a
        promise about a sync that can never happen."""
        conn = self._conn()
        try:
            conn.execute("UPDATE items SET status='archived' WHERE id='it1'")
        finally:
            conn.close()
        r = self.client.post("/api/accounts/chk/hidden", json={"hidden": True})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._balance("chk")
        self.assertIsNotNone(row["user_removed_at"])
        self.assertIsNotNone(row["balance_current"])


if __name__ == "__main__":
    unittest.main()
