"""Replacing or clearing BYO provider keys must not strand live connections.

A Plaid access token only works with the client that minted it, and an MX
user belongs to the MX client (and environment) that created it. So:

* rotating the MX API key under the same client keeps the stored MX user —
  dropping it would make the next sync create an empty user and leave every
  linked bank behind at MX;
* moving MX to another client or environment, or clearing the MX keys, is
  refused while live MX connections exist; once nothing is live, the old MX
  user (and the bank data MX holds for it) is deleted while its keys work;
* replacing the Plaid keys with another client or environment is refused
  while a live Item still holds a token, like clearing them.
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


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app

    def setUp(self):
        try:
            from oikonome.web import security
            security._limiter._hits.clear()
        except Exception:
            pass
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={
            "email": f"pkk-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = self._conn()
        try:
            seed_accounts(conn)
        finally:
            conn.close()

    def _conn(self):
        return tenancy.tenant_connect(self.tid)

    def _config(self):
        conn = self._conn()
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    def _item(self, item_id, aggregator, token, *, archived=False):
        conn = self._conn()
        try:
            sync_base.upsert_item(conn, item_id, aggregator, "Test Bank", token)
            conn.execute(
                "UPDATE items SET aggregator=%s, status=%s WHERE id=%s",
                (aggregator, "archived" if archived else "ok", item_id))
        finally:
            conn.close()


class MxKeysTests(_Base):
    def setUp(self):
        super().setUp()
        with mock.patch("oikonome.sync.mx.validate", return_value=True):
            r = self.client.post("/api/accounts/mx/keys", json={
                "password": "correct-horse-battery",
                "client_id": "c1", "api_key": "k1", "env": "sandbox"})
        self.assertEqual(r.status_code, 200, r.text)
        conn = self._conn()
        try:
            with budget.config_txn(conn) as cfg:
                cfg["mx_user_guid"] = "USR-1"
        finally:
            conn.close()

    def _save(self, client_id, env="sandbox"):
        with mock.patch("oikonome.sync.mx.validate", return_value=True), \
                mock.patch("oikonome.sync.mx.release_tenant_user",
                           return_value="deleted") as rel:
            r = self.client.post("/api/accounts/mx/keys", json={
                "password": "correct-horse-battery",
                "client_id": client_id, "api_key": "k2", "env": env})
        return r, rel

    def test_rotating_the_key_keeps_the_mx_user(self):
        self._item("mx-m1", "mx", None)
        r, rel = self._save("c1")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._config().get("mx_user_guid"), "USR-1")
        rel.assert_not_called()

    def test_moving_client_or_env_is_refused_while_a_bank_is_live(self):
        self._item("mx-m1", "mx", None)
        for cid, env in (("c2", "sandbox"), ("c1", "production")):
            r, rel = self._save(cid, env)
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("Test Bank", r.json()["detail"])
            rel.assert_not_called()
        cfg = self._config()
        self.assertEqual((cfg.get("mx_client_id"), cfg.get("mx_user_guid")),
                         ("c1", "USR-1"))

    def test_moving_with_nothing_live_releases_the_old_user(self):
        self._item("mx-m1", "mx", None, archived=True)
        r, rel = self._save("c2")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([str(c.args[0]) for c in rel.call_args_list], [self.tid])
        self.assertNotIn("mx_user_guid", self._config())

    def test_clear_is_refused_while_a_bank_is_live(self):
        self._item("mx-m1", "mx", None)
        with mock.patch("oikonome.sync.mx.release_tenant_user") as rel:
            r = self.client.delete("/api/accounts/mx/keys")
        self.assertEqual(r.status_code, 400, r.text)
        rel.assert_not_called()
        self.assertEqual(self._config().get("mx_user_guid"), "USR-1")

    def test_clear_deletes_the_mx_user_before_the_keys_go(self):
        with mock.patch("oikonome.sync.mx.release_tenant_user",
                        return_value="deleted") as rel:
            r = self.client.delete("/api/accounts/mx/keys")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mx_user"], "deleted")
        self.assertEqual([str(c.args[0]) for c in rel.call_args_list], [self.tid])
        cfg = self._config()
        for k in ("mx_client_id", "mx_api_key", "mx_env", "mx_user_guid"):
            self.assertNotIn(k, cfg)


class PlaidReplaceKeysTests(_Base):
    def setUp(self):
        super().setUp()
        conn = self._conn()
        try:
            with budget.config_txn(conn) as cfg:
                cfg["plaid_client_id"] = "cid-a"
                cfg["plaid_secret"] = "secret-a"
                cfg["plaid_env"] = "production"
        finally:
            conn.close()

    def _replace(self, cid, env="production"):
        with mock.patch("oikonome.web.api.plaid_validate",
                        return_value={"env": env}):
            return self.client.post("/api/accounts/plaid/keys", json={
                "password": "correct-horse-battery",
                "client_id": cid, "secret": "secret-b"})

    def test_another_client_is_refused_while_an_item_is_live(self):
        self._item("it1", "plaid", "access-live")
        for cid, env in (("cid-b", "production"), ("cid-a", "sandbox")):
            r = self._replace(cid, env)
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("Test Bank", r.json()["detail"])
        self.assertEqual(self._config().get("plaid_client_id"), "cid-a")

    def test_rotating_the_secret_of_the_same_client_is_allowed(self):
        self._item("it1", "plaid", "access-live")
        r = self._replace("cid-a")
        self.assertEqual(r.status_code, 200, r.text)

    def test_another_client_with_nothing_live_is_saved(self):
        r = self._replace("cid-b")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._config().get("plaid_client_id"), "cid-b")


class PlaidEnvKeyItemsTests(_Base):
    """Items linked under the instance's own Plaid keys (no stored client
    id: credentials() falls back to the environment) are bound to that
    client just the same, so household keys may not replace them."""

    ENV = {"OIKONOME_PLAID_CLIENT_ID": "cid-instance",
           "OIKONOME_PLAID_SECRET": "secret-instance",
           "OIKONOME_PLAID_ENV": "production"}

    def _replace(self, cid, env="production"):
        with mock.patch.dict(os.environ, self.ENV), \
                mock.patch("oikonome.web.api.plaid_validate",
                           return_value={"env": env}):
            return self.client.post("/api/accounts/plaid/keys", json={
                "password": "correct-horse-battery",
                "client_id": cid, "secret": "secret-b"})

    def test_household_keys_are_refused_over_live_env_key_items(self):
        self._item("it1", "plaid", "access-live")
        r = self._replace("cid-household")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("Test Bank", r.json()["detail"])
        self.assertIsNone(self._config().get("plaid_client_id"))

    def test_saving_the_instance_client_itself_is_allowed(self):
        self._item("it1", "plaid", "access-live")
        r = self._replace("cid-instance")
        self.assertEqual(r.status_code, 200, r.text)

    def test_with_nothing_live_household_keys_are_saved(self):
        r = self._replace("cid-household")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._config().get("plaid_client_id"),
                         "cid-household")


class PlaidLinkAcrossAKeySwapTests(_Base):
    """A link whose token was exchanged under keys that were replaced
    before its row was written hands the Item back instead of storing a
    token nothing can use."""

    def test_a_link_finishing_after_a_swap_hands_its_item_back(self):
        from oikonome.sync import plaid as plaid_mod
        conn = self._conn()
        try:
            with budget.config_txn(conn) as cfg:
                cfg["plaid_client_id"] = "cid-a"
                cfg["plaid_secret"] = "secret-a"
                cfg["plaid_env"] = "production"
            old = plaid_mod.Client.for_tenant(conn)
            removed = []

            def exchange(_tok):
                # the swap lands while the exchange is in flight
                with budget.config_txn(conn) as cfg:
                    cfg["plaid_client_id"] = "cid-b"
                return {"item_id": "it-race", "access_token": "access-a"}

            old.exchange_public_token = exchange
            old.remove_item = lambda tok: removed.append(tok)
            with mock.patch.object(plaid_mod.Client, "for_tenant",
                                   return_value=old):
                with self.assertRaises(ValueError):
                    plaid_mod.link_item(conn, "public-x", "Test Bank")
            self.assertEqual(removed, ["access-a"])
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM items WHERE id='it-race'").fetchone())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
