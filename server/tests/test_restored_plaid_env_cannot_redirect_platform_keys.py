"""A tenant's plaid_env applies only to the tenant's own keys.

A restored plaid_env=sandbox describes the keys that came with it. On an
instance using platform keys it must not send the platform's production
keys to sandbox.plaid.com.
"""

import os
import unittest
import uuid
from unittest import mock

from oikonome.sync import plaid

from .util import make_db, write_config


class PlaidEnvTests(unittest.TestCase):
    def test_platform_keys_follow_the_platform_env(self):
        conn = make_db()
        try:
            write_config(conn, plaid_env="sandbox")
            with mock.patch.dict(os.environ, {
                    "OIKONOME_PLAID_CLIENT_ID": "platform",
                    "OIKONOME_PLAID_SECRET": "s",
                    "OIKONOME_PLAID_ENV": "production"}):
                _, _, url = plaid.credentials(conn)
            self.assertEqual(url, plaid.ENV_URLS["production"])
        finally:
            conn.close()

    def test_own_keys_keep_their_own_env(self):
        conn = make_db()
        try:
            write_config(conn, plaid_env="sandbox", plaid_client_id="mine",
                         plaid_secret="s")
            with mock.patch.dict(os.environ, {"OIKONOME_PLAID_ENV":
                                              "production"}):
                _, _, url = plaid.credentials(conn)
            self.assertEqual(url, plaid.ENV_URLS["sandbox"])
        finally:
            conn.close()

    def test_an_unrecognised_instance_env_is_refused_with_the_allowed_values(self):
        """A typo'd or retired env name used to fall back to production
        silently, so sandbox keys went to production.plaid.com and every
        link failed with an unrelated key error."""
        conn = make_db()
        try:
            for bad in ("Sandbox", "development", "'sandbox'"):
                with self.subTest(env=bad), mock.patch.dict(os.environ, {
                        "OIKONOME_PLAID_CLIENT_ID": "platform",
                        "OIKONOME_PLAID_SECRET": "s",
                        "OIKONOME_PLAID_ENV": bad}):
                    with self.assertRaises(plaid.PlaidError) as cm:
                        plaid.credentials(conn)
                    msg = cm.exception.message
                    self.assertIn("production", msg)
                    self.assertIn("sandbox", msg)
            # unset and empty keep the documented default
            for ok in (None, ""):
                env = {"OIKONOME_PLAID_CLIENT_ID": "platform",
                       "OIKONOME_PLAID_SECRET": "s"}
                if ok is not None:
                    env["OIKONOME_PLAID_ENV"] = ok
                with self.subTest(env=ok), mock.patch.dict(os.environ, env):
                    if ok is None:
                        os.environ.pop("OIKONOME_PLAID_ENV", None)
                    _, _, url = plaid.credentials(conn)
                    self.assertEqual(url, plaid.ENV_URLS["production"])
        finally:
            conn.close()

    def test_the_item_ledger_names_the_env_the_platform_keys_used(self):
        """The slot ledger counts production platform Items. A restored
        plaid_env=sandbox must not relabel a production platform Item as
        sandbox there, any more than it may redirect the keys."""
        conn = make_db()
        try:
            write_config(conn, plaid_env="sandbox")
            item_id = f"pl-envledger-{uuid.uuid4().hex[:10]}"
            with mock.patch.dict(os.environ, {
                    "OIKONOME_PLAID_CLIENT_ID": "platform",
                    "OIKONOME_PLAID_SECRET": "s",
                    "OIKONOME_PLAID_ENV": "production"}):
                plaid._record_slot(conn, item_id)
            row = conn.execute(
                "SELECT environment, credential_source FROM "
                "plaid_item_ledger WHERE item_id=%s", (item_id,)).fetchone()
            self.assertEqual((row["environment"], row["credential_source"]),
                             ("production", "platform"))
        finally:
            conn.close()

    def test_an_erasure_still_releases_under_an_unrecognised_env(self):
        """Erasure wipes the rows that hold the only copy of each access
        token, so a release refused over a configuration typo would leave
        the Item billing at Plaid with nothing able to remove it. The
        release uses the documented default; other calls still refuse."""
        from oikonome.sync import base as sync_base
        conn = make_db()
        try:
            tid = conn.execute(
                "SELECT current_setting('app.tenant_id') AS t"
            ).fetchone()["t"]
            item = f"pl-envrel-{uuid.uuid4().hex[:10]}"
            sync_base.upsert_item(conn, item, "plaid", "Q Bank",
                                  "access-production-envrel")
            seen = []

            def remove_item(client, token):
                seen.append((client.base_url, token))
                return {}
            with mock.patch.dict(os.environ, {
                    "OIKONOME_PLAID_CLIENT_ID": "platform",
                    "OIKONOME_PLAID_SECRET": "s",
                    "OIKONOME_PLAID_ENV": "development"}), \
                    mock.patch.object(plaid.Client, "remove_item",
                                      remove_item), \
                    mock.patch.object(plaid, "stamp_ledger_removed"):
                with self.assertRaises(plaid.PlaidError):
                    plaid.credentials(conn)
                n = plaid.release_tenant_items(tid)
            self.assertEqual(n.failed_ids, ())
            self.assertEqual(seen, [(plaid.ENV_URLS["production"],
                                     "access-production-envrel")])
        finally:
            conn.close()
