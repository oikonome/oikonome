"""Wizard backend: background sync with live progress
(job_progress row polled by the Sync step), per-account history coverage,
explicit wizard step-state marks, and simplefin-org sync_log stamps."""

import time
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.sync import base as sync_base
from oikonome.sync import simplefin

from .test_simplefin import _transport
from .util import _ensure_db, add_txn, make_db


class SimplefinOrgLogSyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_mask_extracted_from_account_name(self):
        # SimpleFIN carries no mask field; the last-4 lives in the NAME —
        # without it, cross-provider link suggestions can never fire
        sync_base.upsert_item(self.conn, "sfin-main", "simplefin", "SimpleFIN",
                              "https://u:p@bridge.test/simplefin")
        payload = {"accounts": [
            {"id": "acc-1", "name": "Everyday Checking (4242)",
             "currency": "USD", "balance": "100",
             "org": {"name": "Acme Bank"}, "transactions": []},
        ]}
        import httpx as _hx
        import json as _json
        simplefin.sync(self.conn, "sfin-main",
                       "https://u:p@bridge.test/simplefin",
                       transport=_hx.MockTransport(
                           lambda r: _hx.Response(200,
                                                  text=_json.dumps(payload))))
        self.assertEqual(self.conn.execute(
            "SELECT mask FROM accounts WHERE id='acc-1'"
        ).fetchone()["mask"], "4242")

    def test_archived_org_child_stays_disconnected(self):
        sync_base.upsert_item(self.conn, "sfin-main", "simplefin", "SimpleFIN",
                              "https://u:p@bridge.test/simplefin")
        simplefin.sync(self.conn, "sfin-main",
                       "https://u:p@bridge.test/simplefin",
                       transport=_transport())
        kid = self.conn.execute(
            "SELECT id FROM items WHERE aggregator='simplefin-org' "
            "ORDER BY id LIMIT 1").fetchone()["id"]
        self.conn.execute(
            "UPDATE items SET status='archived' WHERE id=%s", (kid,))
        n_before = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions t JOIN accounts a "
            "ON a.id=t.account_id WHERE a.item_id=%s", (kid,)).fetchone()["n"]
        simplefin.sync(self.conn, "sfin-main",
                       "https://u:p@bridge.test/simplefin",
                       transport=_transport())
        # not resurrected, no new rows for its accounts
        self.assertEqual(self.conn.execute(
            "SELECT status FROM items WHERE id=%s", (kid,)
        ).fetchone()["status"], "archived")
        n_after = self.conn.execute(
            "SELECT COUNT(*) AS n FROM transactions t JOIN accounts a "
            "ON a.id=t.account_id WHERE a.item_id=%s", (kid,)).fetchone()["n"]
        self.assertEqual(n_before, n_after)

    def test_org_children_get_sync_log_rows(self):
        # with only the bridge stamped, every SimpleFIN bank reads
        # "never synced" forever (last_ok is grouped per item)
        sync_base.upsert_item(self.conn, "sfin-main", "simplefin", "SimpleFIN",
                              "https://u:p@bridge.test/simplefin")
        simplefin.sync(self.conn, "sfin-main",
                       "https://u:p@bridge.test/simplefin",
                       transport=_transport())
        kids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM items WHERE aggregator='simplefin-org'")]
        self.assertTrue(kids)
        for kid in kids:
            self.assertIsNotNone(self.conn.execute(
                "SELECT 1 FROM sync_log WHERE item_id=%s AND error IS NULL",
                (kid,)).fetchone(), f"no sync_log row for {kid}")


class WizardApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"wiz-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def _conn(self):
        return tenancy.tenant_connect(self.tid)

    def test_wizard_steps_roundtrip_and_validation(self):
        r = self.client.post("/api/settings",
                             json={"wizard_steps": {"connect": "done"}})
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/settings",
                             json={"wizard_steps": {"sync": "skipped"}})
        self.assertEqual(r.status_code, 200)
        ob = self.client.get("/api/onboarding").json()
        # merge semantics: both marks survive independent posts
        self.assertEqual(ob["wizard_steps"],
                         {"connect": "done", "sync": "skipped"})
        # null clears a mark
        self.client.post("/api/settings",
                         json={"wizard_steps": {"sync": None}})
        self.assertEqual(self.client.get("/api/onboarding").json()
                         ["wizard_steps"], {"connect": "done"})
        # unknown step / bad state rejected
        self.assertEqual(self.client.post("/api/settings", json={
            "wizard_steps": {"bogus": "done"}}).status_code, 400)
        self.assertEqual(self.client.post("/api/settings", json={
            "wizard_steps": {"connect": "maybe"}}).status_code, 400)

    def test_onboarding_counts_connections_not_children(self):
        conn = self._conn()
        try:
            sync_base.upsert_item(conn, "sfin-main", "simplefin", "SimpleFIN",
                                  "https://u:p@bridge.test/simplefin")
            simplefin.sync(conn, "sfin-main",
                           "https://u:p@bridge.test/simplefin",
                           transport=_transport())
        finally:
            conn.close()
        ob = self.client.get("/api/onboarding").json()
        # the bridge is ONE connection; its per-bank children don't inflate
        self.assertEqual(ob["connections"], 1)

    def test_history_coverage(self):
        from .util import seed_accounts
        conn = self._conn()
        try:
            seed_accounts(conn)
            add_txn(conn, "2026-01-05", 20, "OLD", account="chk")
            add_txn(conn, "2026-07-01", 30, "NEW", account="chk")
        finally:
            conn.close()
        cov = self.client.get("/api/history/coverage").json()["accounts"]
        chk = next(a for a in cov if a["id"] == "chk")
        self.assertEqual(chk["earliest"], "2026-01-05")
        self.assertEqual(chk["latest"], "2026-07-01")
        self.assertGreaterEqual(chk["transactions"], 2)
        # accounts with no history report null bounds, not a crash
        card = next(a for a in cov if a["id"] == "card")
        self.assertIsNone(card["earliest"])

    def test_background_sync_progress_lifecycle(self):
        from oikonome.jobs import worker

        def fake_sync(tid, since_days=30, progress=None):
            progress("item", {"id": "i1", "name": "Bank A",
                              "status": "running", "transactions": 0})
            progress("item", {"id": "i1", "name": "Bank A",
                              "status": "ok", "transactions": 42})
            progress("categorize", {"done": 5, "total": 5})
            return {"i1": "ok:42"}

        with mock.patch.object(worker, "sync_tenant", side_effect=fake_sync):
            r = self.client.post("/api/jobs/sync/start")
            self.assertTrue(r.json()["started"])
            deadline = time.time() + 10
            st = {}
            while time.time() < deadline:
                st = self.client.get("/api/jobs/sync/status").json()
                if st["state"] in ("done", "error"):
                    break
                time.sleep(0.1)
        self.assertEqual(st["state"], "done")
        items = st["progress"]["items"]
        self.assertEqual(items, [{"id": "i1", "name": "Bank A",
                                  "status": "ok", "transactions": 42}])
        self.assertEqual(st["progress"]["categorize"],
                         {"done": 5, "total": 5})
        self.assertTrue(st["progress"]["ok"])

    def test_disconnect_plaid_releases_item_and_archives(self):
        from unittest import mock as _mock

        from oikonome.sync import plaid as plaid_mod
        conn = self._conn()
        try:
            sync_base.upsert_item(conn, "plaid-x", "plaid", "Chase",
                                  "access-token-x")
        finally:
            conn.close()
        fake = _mock.MagicMock()
        with _mock.patch.object(plaid_mod.Client, "for_tenant",
                                return_value=fake):
            r = self.client.post(
                "/api/connections/plaid-x/disconnect").json()
        self.assertTrue(r["ok"] and r["plaid_released"])
        fake.remove_item.assert_called_once_with("access-token-x")
        conn = self._conn()
        try:
            self.assertEqual(conn.execute(
                "SELECT status FROM items WHERE id='plaid-x'"
            ).fetchone()["status"], "archived")
        finally:
            conn.close()

    def test_second_start_while_running_is_refused(self):
        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO job_progress (id, state, progress)
                   VALUES ('sync','running','{}'::jsonb)
                   ON CONFLICT (tenant_id, id) DO UPDATE SET
                       state='running', started_at=now(), updated_at=now()""")
        finally:
            conn.close()
        r = self.client.post("/api/jobs/sync/start").json()
        self.assertFalse(r["started"])
        # ... but a STALE running row (died worker) is reclaimed
        conn = self._conn()
        try:
            conn.execute("UPDATE job_progress SET updated_at="
                         "now() - interval '20 minutes' WHERE id='sync'")
        finally:
            conn.close()
        from oikonome.jobs import worker
        with mock.patch.object(worker, "sync_tenant", return_value={}):
            r = self.client.post("/api/jobs/sync/start").json()
        self.assertTrue(r["started"])


if __name__ == "__main__":
    unittest.main()
