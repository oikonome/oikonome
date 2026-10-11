"""The Settings view names the held Plaid or MX client to the owner, and
to nobody else.

The owner has to see which client the hold is about (its id, environment
and when it was saved) to decide whether it is theirs; client ids are not
secrets and Settings already shows them to the owner. No secret, key or
token ever appears in the record. A member, and the shared demonstration
login, learn that new bank links are held, never which client."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.notify import outbound_pause as op
from oikonome.web import api

from .bank_link_hold_fixtures import (MX_KEY, PLAID_CLIENT, PLAID_SECRET,
                                      arm_hold, save_household_keys)
from .util import _ensure_db, write_config


class HeldKeysInTheSettingsView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self._env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""})
        self._env.start()
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={
            "email": f"held-{uuid.uuid4().hex[:10]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            write_config(conn)
            save_household_keys(conn)
            arm_hold(conn)
        finally:
            conn.close()

    def tearDown(self):
        self._env.stop()

    def test_the_owner_sees_the_held_clients_and_no_secret(self):
        r = self.client.get("/api/settings")
        self.assertEqual(r.status_code, 200, r.text)
        p = r.json()["outbound_paused"]
        self.assertTrue(p["aggregator"])
        keys = {k["provider"]: k for k in p["aggregator_keys"]}
        self.assertEqual(keys["plaid"]["client_id"], PLAID_CLIENT)
        self.assertEqual(keys["plaid"]["env"], "production")
        self.assertIn("mx", keys)
        for k in keys.values():
            self.assertEqual(set(k), {"provider", "client_id", "env",
                                      "saved_at"})
        self.assertNotIn(PLAID_SECRET, r.text)
        self.assertNotIn(MX_KEY, r.text)

    def _view(self, user, hidden):
        conn = tenancy.tenant_connect(self.tid)
        try:
            from oikonome.engine import budget
            view = {"outbound_paused": op.paused(budget.load_config(conn))}
        finally:
            conn.close()
        with mock.patch.object(api.demoguard, "people_hidden",
                               return_value=hidden):
            return api._hide_people(view, user)["outbound_paused"]

    def test_a_member_learns_links_are_held_but_not_which_client(self):
        p = self._view({"role": "member"}, hidden=False)
        self.assertTrue(p["aggregator"])
        self.assertNotIn("aggregator_keys", p)

    def test_the_shared_login_learns_links_are_held_but_not_which_client(self):
        p = self._view({"role": "owner", "second_factor_waived": True},
                       hidden=True)
        self.assertTrue(p["aggregator"])
        self.assertNotIn("aggregator_keys", p)

    def _stored(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            from oikonome.engine import budget
            return budget.load_config(conn)
        finally:
            conn.close()

    def test_the_saved_at_stamp_is_server_written_and_goes_with_the_keys(self):
        """The banner's "saved on" date is the server's: a Settings save
        cannot backdate or postdate it, and clearing the keys drops it."""
        r = self.client.post("/api/settings", json={
            "plaid_keys_saved_at": "2099-01-01T00:00:00+00:00"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotEqual(self._stored().get("plaid_keys_saved_at"),
                            "2099-01-01T00:00:00+00:00")
        conn = tenancy.tenant_connect(self.tid)
        try:
            from oikonome.engine import budget
            with budget.config_txn(conn) as cfg:
                cfg["plaid_keys_saved_at"] = "2026-07-01T00:00:00+00:00"
        finally:
            conn.close()
        r = self.client.delete("/api/accounts/plaid/keys")
        self.assertEqual(r.status_code, 200, r.text)
        cfg = self._stored()
        self.assertNotIn("plaid_client_id", cfg)
        self.assertNotIn("plaid_keys_saved_at", cfg)


if __name__ == "__main__":
    unittest.main()
