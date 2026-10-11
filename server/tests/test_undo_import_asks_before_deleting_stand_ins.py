"""Undoing an import asks before it deletes rows only a note naming no
feed account holds.

Such a row may be the only copy of a charge an older bank feed set aside;
nothing can check whether a feed still relies on it. So the undo — through
the API and through the form door — changes nothing and answers 409 with
the count, and only the owner's literal `force_kept: true` deletes them:
the same contract as removing the account that holds them.
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import base as sync_base
from oikonome.sync import batches

from .util import _ensure_db

PW = "correct-horse-battery"


class UndoImportAsksFirst(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        email = f"undo-{uuid.uuid4().hex[:8]}@example.dev"
        r = self.client.post("/api/signup", data={"email": email,
                                                  "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name)"
                " VALUES ('files', 'manual', 'Files'),"
                " ('feedit', 'plaid', 'Sample Bank')")
            conn.execute(
                "INSERT INTO accounts (id, item_id, name, type, subtype)"
                " VALUES ('filechk', 'files', 'Checking (file)',"
                " 'depository', 'checking')")
            self.batch = batches.create(conn, "csv", "filechk", "a.csv")
            sync_base.upsert_transactions(conn, batches.tag(conn, [
                sync_base.Transaction(
                    id="csv:noted", account_id="filechk",
                    date=dt.date(2025, 2, 3), amount=18.0,
                    name="MAPLE DINER")], self.batch))
            batches.finish(conn, self.batch, 1)
            # a feed set its copy aside without noting which account
            sync_base.mark_stood_in(conn, {"csv:noted": []}, "feed")
        finally:
            conn.close()

    def _exists(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute("SELECT 1 FROM transactions"
                                " WHERE id = 'csv:noted'").fetchone() \
                is not None
        finally:
            conn.close()

    def _member(self):
        from oikonome.auth import passwords
        email = f"mem-{uuid.uuid4().hex[:8]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role,"
                " verified_at) VALUES (%s, %s, %s, 'member', now())",
                (self.tid, email, passwords.hash_password(PW)))
        finally:
            admin.close()
        c = TestClient(self.app)
        r = c.post("/login", data={"email": email, "password": PW},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        return c

    def test_the_api_asks_then_the_owner_forces(self):
        r = self.client.post("/api/import/rollback",
                             json={"batch_id": self.batch})
        self.assertEqual(r.status_code, 409, r.text)
        body = r.json()
        self.assertEqual(body["error"], "kept_rows_unconfirmed")
        self.assertEqual(body["kept_transactions"], 1)
        self.assertIn("1 imported row", body["detail"])
        self.assertTrue(self._exists())
        r = self.client.post("/api/import/rollback",
                             json={"batch_id": self.batch, "force_kept": "yes"})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertTrue(self._exists())
        r = self.client.post("/api/import/rollback",
                             json={"batch_id": self.batch, "force_kept": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["deleted"], 1)
        self.assertFalse(self._exists())

    def test_a_member_cannot_force(self):
        m = self._member()
        r = m.post("/api/import/rollback",
                   json={"batch_id": self.batch, "force_kept": True})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertTrue(self._exists())
        # asking is fine: the member is told, and nothing changes
        r = m.post("/api/import/rollback", json={"batch_id": self.batch})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertTrue(self._exists())

    def test_a_member_is_not_offered_the_retry_only_an_owner_may_send(self):
        """The owner's 409 asks "undo anyway?" and invites force_kept; a
        member sending that retry is refused as owner-only, so the member's
        409 is a plain note under its own error value — the count, that
        nothing was undone, and who can finish it — with no such offer."""
        m = self._member()
        for door, kw in (("/api/import/rollback",
                          {"json": {"batch_id": self.batch}}),
                         ("/import/rollback",
                          {"data": {"batch_id": self.batch}})):
            r = m.post(door, follow_redirects=False, **kw)
            self.assertEqual(r.status_code, 409, r.text)
            body = r.json()
            self.assertEqual(body["error"], "kept_rows", door)
            self.assertEqual(body["kept_transactions"], 1)
            self.assertNotIn("anyway", body["detail"])
            self.assertIn("owner", body["detail"])
        self.assertTrue(self._exists())

    def test_the_form_door_has_the_same_contract(self):
        r = self.client.post("/import/rollback",
                             data={"batch_id": self.batch},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json()["error"], "kept_rows_unconfirmed")
        self.assertEqual(r.json()["kept_transactions"], 1)
        self.assertTrue(self._exists())
        r = self.client.post("/import/rollback",
                             data={"batch_id": self.batch,
                                   "force_kept": "true"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertFalse(self._exists())
