"""What Plaid asks an integration to get right: identifier retention
(institution_id, link_session_id, request_id), rate-limit retry with
backoff, complete Item release on every tenant-erasure path, token
encryption at rest, and webhook_status surfacing the fix door."""

import json
import os
import unittest
import uuid
from unittest import mock

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from oikonome.db import crypto, tenancy
from oikonome.sync import base as sync_base
from oikonome.sync import plaid

from .util import _ensure_db, make_db, write_config

TXN_PAGE = {"added": [], "modified": [], "removed": [],
            "has_more": False, "next_cursor": "c-end", "request_id": "req-ok"}


def _transport(item_id: str, removes: list | None = None,
               sync_fail: dict | None = None):
    def handler(request):
        path = request.url.path
        if path == "/item/public_token/exchange":
            return httpx.Response(200, text=json.dumps(
                {"item_id": item_id, "access_token": "access-prod-abc"}))
        if path == "/item/get":
            return httpx.Response(200, text=json.dumps(
                {"item": {"institution_id": "ins_1"}}))
        if path == "/institutions/get_by_id":
            return httpx.Response(200, text=json.dumps(
                {"institution": {"name": "Demo Bank"}}))
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps({"accounts": []}))
        if path == "/transactions/sync":
            if sync_fail:
                return httpx.Response(500, text=json.dumps(sync_fail))
            return httpx.Response(200, text=json.dumps(TXN_PAGE))
        if path == "/item/remove":
            if removes is not None:
                removes.append(
                    json.loads(request.content.decode())["access_token"])
            return httpx.Response(200, text=json.dumps(
                {"request_id": "req-rm"}))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class IdentifierRetentionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        self.item_id = f"pl-bp-{uuid.uuid4().hex[:10]}"

    def tearDown(self):
        self.conn.close()

    def test_link_persists_institution_and_link_session_ids(self):
        plaid.link_item(self.conn, "public-x", link_session_id="ls-123",
                        transport=_transport(self.item_id))
        row = self.conn.execute(
            "SELECT institution_id, institution_name, link_session_id "
            "FROM items WHERE id=%s", (self.item_id,)).fetchone()
        self.assertEqual(row["institution_id"], "ins_1")
        self.assertEqual(row["institution_name"], "Demo Bank")
        self.assertEqual(row["link_session_id"], "ls-123")

    def test_request_id_logged_on_success(self):
        plaid.link_item(self.conn, "public-x",
                        transport=_transport(self.item_id))
        row = self.conn.execute(
            "SELECT request_id, error FROM sync_log WHERE item_id=%s "
            "ORDER BY id DESC LIMIT 1", (self.item_id,)).fetchone()
        self.assertIsNone(row["error"])
        self.assertEqual(row["request_id"], "req-ok")

    def test_request_id_logged_on_error(self):
        sync_base.upsert_item(self.conn, self.item_id, "plaid", "Demo",
                              "tok")
        fail = {"error_code": "INSTITUTION_DOWN",
                "error_type": "INSTITUTION_ERROR",
                "error_message": "down", "request_id": "req-broken"}
        with self.assertRaises(plaid.PlaidError):
            plaid.sync(self.conn, self.item_id,
                       transport=_transport(self.item_id, sync_fail=fail))
        row = self.conn.execute(
            "SELECT request_id, error FROM sync_log WHERE item_id=%s "
            "ORDER BY id DESC LIMIT 1", (self.item_id,)).fetchone()
        self.assertEqual(row["request_id"], "req-broken")
        self.assertIn("req-broken", row["error"])   # support asks for it


class RateLimitRetryTests(unittest.TestCase):
    RL = {"error_code": "RATE_LIMIT", "error_type": "RATE_LIMIT_EXCEEDED",
          "error_message": "slow down", "request_id": "req-rl"}

    def _client(self, fail_times: int, calls: list):
        def handler(request):
            calls.append(request.url.path)
            if len(calls) <= fail_times:
                return httpx.Response(429, text=json.dumps(self.RL))
            return httpx.Response(200, text=json.dumps(
                {"ok": True, "request_id": "req-fine"}))
        return plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                            transport=httpx.MockTransport(handler))

    def test_transient_rate_limit_retried_with_backoff(self):
        calls, sleeps = [], []
        client = self._client(1, calls)
        with mock.patch.object(plaid.Client, "_sleep",
                               staticmethod(sleeps.append)):
            out = client.post("/accounts/balance/get", {"access_token": "t"})
        self.assertTrue(out["ok"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [1.0])

    def test_persistent_rate_limit_raises_after_bounded_retries(self):
        calls, sleeps = [], []
        client = self._client(99, calls)
        with mock.patch.object(plaid.Client, "_sleep",
                               staticmethod(sleeps.append)):
            with self.assertRaises(plaid.PlaidError) as ctx:
                client.post("/accounts/balance/get", {"access_token": "t"})
        self.assertEqual(ctx.exception.type, "RATE_LIMIT_EXCEEDED")
        self.assertEqual(len(calls), 3)              # 1 try + 2 retries
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_other_errors_do_not_retry(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(400, text=json.dumps(
                {"error_code": "ITEM_LOGIN_REQUIRED",
                 "error_type": "ITEM_ERROR", "error_message": "re-auth"}))
        client = plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                              transport=httpx.MockTransport(handler))
        with self.assertRaises(plaid.PlaidError):
            client.post("/accounts/balance/get", {"access_token": "t"})
        self.assertEqual(len(calls), 1)


class TokenAtRestTests(unittest.TestCase):
    def test_access_token_stored_encrypted_under_master_key(self):
        mk = os.environ.get("OIKONOME_MASTER_KEY")
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        try:
            conn = make_db()
            try:
                item_id = f"pl-enc-{uuid.uuid4().hex[:10]}"
                sync_base.upsert_item(conn, item_id, "plaid", "Demo",
                                      "access-secret-tok")
                raw = conn.execute(
                    "SELECT access_token FROM items WHERE id=%s",
                    (item_id,)).fetchone()["access_token"]
                self.assertTrue(raw.startswith(crypto.PREFIX))
                self.assertNotIn("access-secret-tok", raw)
                self.assertEqual(sync_base.get_access_token(conn, item_id),
                                 "access-secret-tok")
            finally:
                conn.close()
        finally:
            if mk is None:
                os.environ.pop("OIKONOME_MASTER_KEY", None)
            else:
                os.environ["OIKONOME_MASTER_KEY"] = mk


class ReleaseOnErasureTests(unittest.TestCase):
    """A wiped tenant must leave no live access_token at Plaid — that is
    also what stops per-Item billing."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _patch_client(self, removes: list):
        def for_tenant(conn, transport=None):
            return plaid.Client("cid", "sec", plaid.ENV_URLS["sandbox"],
                                transport=_transport("x", removes=removes))
        return mock.patch.object(plaid.Client, "for_tenant",
                                 staticmethod(for_tenant))

    def test_release_covers_live_and_archived_items(self):
        conn = make_db()
        try:
            tid = conn.execute(
                "SELECT current_setting('app.tenant_id') AS t"
            ).fetchone()["t"]
            write_config(conn, plaid_client_id="cid", plaid_secret="sec")
            a = f"pl-rel-{uuid.uuid4().hex[:8]}"
            b = f"pl-rel-{uuid.uuid4().hex[:8]}"
            sync_base.upsert_item(conn, a, "plaid", "A", "tok-a")
            sync_base.upsert_item(conn, b, "plaid", "B", "tok-b")
            conn.execute("UPDATE items SET status='archived' WHERE id=%s",
                         (b,))
        finally:
            conn.close()
        removes: list = []
        with self._patch_client(removes):
            released = plaid.release_tenant_items(tid)
        self.assertEqual(released, 2)
        self.assertEqual(sorted(removes), ["tok-a", "tok-b"])

    def test_account_delete_route_releases_items(self):
        client = TestClient(self.appmod.app)
        pw = "correct-horse-battery"
        email = f"plbp-{uuid.uuid4().hex[:10]}@example.dev"
        r = client.post("/api/signup", data={"email": email, "password": pw})
        self.assertEqual(r.status_code, 200, r.text)
        with mock.patch.object(plaid, "release_tenant_items",
                               return_value=1) as rel:
            r = client.post("/api/account/delete", data={"password": pw})
        self.assertEqual(r.status_code, 200, r.text)
        rel.assert_called_once()

    def test_admin_tenant_delete_releases_items(self):
        token = "test-admin-token-" + "y" * 32
        os.environ["OIKONOME_ADMIN_TOKEN"] = token
        try:
            conn = make_db()
            try:
                tid = conn.execute(
                    "SELECT current_setting('app.tenant_id') AS t"
                ).fetchone()["t"]
            finally:
                conn.close()
            client = TestClient(self.appmod.app)
            r = client.post("/admin/console/login", data={"token": token},
                            follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            with mock.patch.object(plaid, "release_tenant_items",
                                   return_value=0) as rel:
                r = client.post("/admin/console/tenant-delete",
                                data={"tenant_id": tid, "confirm": tid,
                                      "mode": "immediate"})
            self.assertEqual(r.status_code, 200, r.text)
            rel.assert_called_once_with(tid)
            admin = tenancy.admin_connect()
            try:
                self.assertIsNone(admin.execute(
                    "SELECT 1 FROM tenants WHERE id=%s", (tid,)).fetchone())
            finally:
                admin.close()
        finally:
            os.environ.pop("OIKONOME_ADMIN_TOKEN", None)


class WebhookStatusSurfacesFixDoorTests(unittest.TestCase):
    def test_connections_effective_status_folds_webhook_state(self):
        from oikonome.web.pages import _connections
        conn = make_db()
        try:
            item_id = f"pl-eff-{uuid.uuid4().hex[:10]}"
            sync_base.upsert_item(conn, item_id, "plaid", "Demo", "tok")
            rows = {r["id"]: r["status"] for r in _connections(conn)}
            self.assertEqual(rows[item_id], "ok")
            conn.execute(
                "UPDATE items SET webhook_status='pending_expiration' "
                "WHERE id=%s", (item_id,))
            rows = {r["id"]: r["status"] for r in _connections(conn)}
            self.assertEqual(rows[item_id], "pending_expiration")
            # the sync loop's own error outranks the webhook flag
            conn.execute(
                "UPDATE items SET status='error:ITEM_LOGIN_REQUIRED' "
                "WHERE id=%s", (item_id,))
            rows = {r["id"]: r["status"] for r in _connections(conn)}
            self.assertEqual(rows[item_id], "error:ITEM_LOGIN_REQUIRED")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
