"""The per-connection "sync now" door leaves a disconnected connection
disconnected. Disconnecting is an owner decision; syncing is open to
members. A SimpleFIN bridge keeps its token when archived and its pull marks
the item live again, so a member pressing sync on it would silently undo
the owner's disconnect. The hourly sweep and the webhook already skip
archived items; this door must too."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import base as sync_base
from oikonome.sync import plaid, simplefin

from .util import _ensure_db


class ManualSyncLeavesDisconnectedItemsAlone(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        data = {"email": f"ms-{uuid.uuid4().hex[:8]}@example.dev",
                "password": "correct-horse-battery"}
        if os.environ.get("OIKONOME_HOSTED"):
            from oikonome.auth import signup_invites
            admin = tenancy.admin_connect()
            try:
                data["invite"] = signup_invites.mint(admin, data["email"])
            finally:
                admin.close()
        assert cls.client.post("/api/signup", data=data).status_code == 200
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def _archived(self, item_id, aggregator):
        conn = tenancy.tenant_connect(self.tid)
        try:
            sync_base.upsert_item(conn, item_id, aggregator, "Invented Bank",
                                  "https://u:p@bridge.test/simplefin")
            conn.execute("UPDATE items SET status='archived' WHERE id=%s",
                         (item_id,))
        finally:
            conn.close()

    def _status(self, item_id):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute("SELECT status FROM items WHERE id=%s",
                                (item_id,)).fetchone()["status"]
        finally:
            conn.close()

    def test_archived_items_are_not_pulled(self):
        for item_id, agg in (("sfin-arch", "simplefin"),
                             ("pl-arch", "plaid")):
            with self.subTest(aggregator=agg):
                self._archived(item_id, agg)
                with mock.patch.object(simplefin, "sync") as sf, \
                        mock.patch.object(plaid, "sync") as pl:
                    r = self.client.post("/accounts/sync",
                                         data={"item_id": item_id},
                                         follow_redirects=False)
                self.assertEqual(r.status_code, 303)
                self.assertIn("disconnected", r.headers["location"])
                sf.assert_not_called()
                pl.assert_not_called()
                self.assertEqual(self._status(item_id), "archived")


if __name__ == "__main__":
    unittest.main()
