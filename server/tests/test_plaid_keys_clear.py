"""DELETE /api/accounts/plaid/keys must not strand Plaid Items.

A Plaid access token is only meaningful to the client_id that minted it.
Clearing a tenant's BYO keys while a live connection still holds a token
therefore destroys the only thing that could ever call /item/remove for it:
the Item keeps its subscription (and its bill) at Plaid forever, and no
later disconnect can release it. The door refuses instead, naming the
connections to disconnect first; a connection already archived while still
holding a token gets one last best-effort release on the way out.
"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.sync import base as sync_base

from .util import _ensure_db, seed_accounts


class PlaidKeysClearTests(unittest.TestCase):
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
            "email": f"pk-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = self._conn()
        try:
            seed_accounts(conn)
            with budget.config_txn(conn) as cfg:
                cfg["plaid_client_id"] = "cid-byo"
                cfg["plaid_secret"] = "secret-byo"
                cfg["plaid_env"] = "sandbox"
        finally:
            conn.close()

    def _conn(self):
        return tenancy.tenant_connect(self.tid)

    def _plaid_item(self, item_id, token, *, archived=False):
        conn = self._conn()
        try:
            sync_base.upsert_item(conn, item_id, "plaid", "Test Bank", token)
            conn.execute(
                "UPDATE items SET aggregator='plaid', status=%s WHERE id=%s",
                ("archived" if archived else "ok", item_id))
        finally:
            conn.close()

    def _config(self):
        conn = self._conn()
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    def test_clearing_keys_is_refused_while_a_live_item_holds_a_token(self):
        self._plaid_item("it1", "access-token-live")
        r = self.client.delete("/api/accounts/plaid/keys")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("Test Bank", r.json()["detail"])
        cfg = self._config()
        self.assertEqual(cfg.get("plaid_client_id"), "cid-byo",
                         "a refused clear must leave the keys in place")
        self.assertTrue(cfg.get("plaid_secret"))

    def test_keys_clear_when_nothing_live_depends_on_them(self):
        r = self.client.delete("/api/accounts/plaid/keys")
        self.assertEqual(r.status_code, 200, r.text)
        cfg = self._config()
        for k in ("plaid_client_id", "plaid_secret", "plaid_env"):
            self.assertNotIn(k, cfg)

    def test_archived_item_holding_a_token_is_released_before_the_keys_go(self):
        """A disconnect whose /item/remove failed keeps its token so the
        reaper can retry. Clearing the keys ends the reaper's ability to
        retry, so this is the last moment the Item can be released."""
        from oikonome.sync import plaid as plaid_mod
        self._plaid_item("it1", "access-token-stale", archived=True)
        fake = mock.MagicMock()
        with mock.patch.object(plaid_mod.Client, "for_tenant",
                               return_value=fake):
            r = self.client.delete("/api/accounts/plaid/keys")
        self.assertEqual(r.status_code, 200, r.text)
        fake.remove_item.assert_called_once_with("access-token-stale")
        self.assertEqual(r.json()["released"], 1)
        self.assertEqual(r.json()["stranded"], 0)
        conn = self._conn()
        try:
            tok = conn.execute(
                "SELECT access_token FROM items WHERE id='it1'").fetchone()
        finally:
            conn.close()
        self.assertIsNone(tok["access_token"])
        self.assertNotIn("plaid_client_id", self._config())

    def _remove_item_probe(self, seen):
        """A fake /item/remove that records, at the moment it is called,
        whether the per-tenant link lock is free and whether the request's
        own connection sits inside an open transaction. Plaid can take up
        to two minutes per call; neither may be held across it."""
        import psycopg
        from psycopg import pq

        from oikonome.sync import plaid as plaid_mod
        captured = {}
        fake = mock.MagicMock()

        def for_tenant(conn):
            captured["conn"] = conn
            return fake

        def remove(token):
            with psycopg.connect(tenancy.ADMIN_DSN, autocommit=True) as c:
                free = c.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                    (f"oikonome:instcap:{self.tid}",)).fetchone()[0]
                if free:
                    c.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                              (f"oikonome:instcap:{self.tid}",))
            seen.append({
                "lock_free": free,
                "idle": captured["conn"].info.transaction_status
                == pq.TransactionStatus.IDLE})
        fake.remove_item.side_effect = remove
        return mock.patch.object(plaid_mod.Client, "for_tenant",
                                 side_effect=for_tenant)

    def test_release_calls_run_outside_the_link_lock_and_transaction(self):
        """A slow Plaid must not keep the household's link lock held (a
        bank link finishing meanwhile would wait on it, pinning a pooled
        connection) nor a pooled connection idle inside a transaction."""
        self._plaid_item("it1", "access-token-stale", archived=True)
        seen = []
        with self._remove_item_probe(seen):
            r = self.client.delete("/api/accounts/plaid/keys")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["released"], 1)
        self.assertEqual(seen, [{"lock_free": True, "idle": True}])
        self.assertNotIn("plaid_client_id", self._config())

    def test_swapping_keys_releases_outside_the_link_lock_too(self):
        self._plaid_item("it1", "access-token-stale", archived=True)
        seen = []
        with self._remove_item_probe(seen), \
                mock.patch("oikonome.web.api.plaid_validate",
                           return_value={"env": "sandbox"}):
            r = self.client.post("/api/accounts/plaid/keys", json={
                "password": "correct-horse-battery",
                "client_id": "cid-other", "secret": "secret-other"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, [{"lock_free": True, "idle": True}])
        self.assertEqual(self._config().get("plaid_client_id"), "cid-other")

    def test_a_connection_going_live_during_the_release_still_refuses(self):
        """The release runs before the lock, so the live-connection check
        is asked again under it: a bank linked while Plaid was answering
        must still stop the keys from being forgotten."""
        from oikonome.sync import plaid as plaid_mod
        self._plaid_item("it1", "access-token-stale", archived=True)
        fake = mock.MagicMock()
        fake.remove_item.side_effect = (
            lambda token: self._plaid_item("it2", "access-token-new"))
        with mock.patch.object(plaid_mod.Client, "for_tenant",
                               return_value=fake):
            r = self.client.delete("/api/accounts/plaid/keys")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._config().get("plaid_client_id"), "cid-byo")

    def test_a_bank_linked_before_the_release_is_never_released(self):
        """Only the archived Items listed under the link lock are released:
        a bank that lands after the list is taken — Plaid answering slowly
        for the stale one — keeps its token, and the keys stay."""
        from oikonome.sync import plaid as plaid_mod
        from oikonome.web import api
        self._plaid_item("it1", "access-token-stale", archived=True)
        real = api._plaid_straggler_ids

        def listed_then_linked(conn):
            ids = real(conn)
            self._plaid_item("it2", "access-token-new")
            return ids

        fake = mock.MagicMock()
        with mock.patch.object(plaid_mod.Client, "for_tenant",
                               return_value=fake), \
                mock.patch.object(api, "_plaid_straggler_ids",
                                  side_effect=listed_then_linked):
            r = self.client.delete("/api/accounts/plaid/keys")
        self.assertEqual(r.status_code, 400, r.text)
        fake.remove_item.assert_called_once_with("access-token-stale")
        conn = self._conn()
        try:
            tok = conn.execute(
                "SELECT access_token FROM items WHERE id='it2'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(tok["access_token"])
        self.assertEqual(self._config().get("plaid_client_id"), "cid-byo")

    def test_a_failed_release_still_clears_the_keys(self):
        """Releasing is best-effort: a Plaid outage must not wedge the door
        shut on a connection the user already disconnected."""
        from oikonome.sync import plaid as plaid_mod
        self._plaid_item("it1", "access-token-stale", archived=True)
        fake = mock.MagicMock()
        fake.remove_item.side_effect = RuntimeError("plaid down")
        with mock.patch.object(plaid_mod.Client, "for_tenant",
                               return_value=fake):
            r = self.client.delete("/api/accounts/plaid/keys")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["stranded"], 1)
        self.assertNotIn("plaid_client_id", self._config())


if __name__ == "__main__":
    unittest.main()
