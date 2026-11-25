"""An OIKONOME_PLAID_ENV that names no Plaid environment is refused by every
reader of it, never read as production, and a release still goes through.

plaid.instance_env is the one reading of the instance's Plaid environment:
unset or empty is production, a known name is itself, anything else is
refused with the allowed values. A reader that spells its own fallback
instead sends the platform's keys to production on a typo (the webhook's
key fetch), or compares a raw empty value against "production" and calls
the same keys a different client (the connections bundle). A disconnect is
a release, and a release never waits on a configuration typo.
"""

import os
import unittest
import uuid
from unittest import mock

from oikonome.sync import base as sync_base
from oikonome.sync import connections_bundle as cb
from oikonome.sync import plaid
from oikonome.web import plaid_webhook

from .util import make_db, write_config

_PLATFORM = {"OIKONOME_PLAID_CLIENT_ID": "platform",
             "OIKONOME_PLAID_SECRET": "s", "OIKONOME_HOSTED": ""}


def _payload(**config):
    return {"oikonome_config": cb.FORMAT, "config": config, "secrets": {},
            "items": []}


class WebhookKeyFetch(unittest.TestCase):
    def test_an_unrecognised_env_fetches_no_key(self):
        with mock.patch.dict(os.environ, {**_PLATFORM,
                                          "OIKONOME_PLAID_ENV": "Sandbox"}):
            self.assertIsNone(plaid_webhook._platform_creds())

    def test_unset_and_empty_keep_production(self):
        for env in (None, ""):
            with self.subTest(env=env), mock.patch.dict(os.environ,
                                                        _PLATFORM):
                os.environ.pop("OIKONOME_PLAID_ENV", None)
                if env is not None:
                    os.environ["OIKONOME_PLAID_ENV"] = env
                self.assertEqual(plaid_webhook._platform_creds()[2],
                                 plaid.ENV_URLS["production"])


class BundleKeyComparison(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def test_an_empty_env_is_production_when_the_keys_are_compared(self):
        """The household linked under the platform's keys; a bundle naming
        those same keys in production moves nothing."""
        sync_base.upsert_item(self.conn, "pl1", "plaid", "Bank", "tok-A")
        with mock.patch.dict(os.environ, {**_PLATFORM,
                                          "OIKONOME_PLAID_ENV": ""}):
            cb.apply_payload(self.conn, _payload(
                plaid_client_id="platform", plaid_env="production"))

    def test_an_unrecognised_env_refuses_a_bundle_carrying_plaid_keys(self):
        with mock.patch.dict(os.environ, {**_PLATFORM,
                                          "OIKONOME_PLAID_ENV": "Sandbox"}):
            with self.assertRaises(ValueError) as cm:
                cb.apply_payload(self.conn,
                                 _payload(plaid_client_id="cid-B"))
        self.assertIn("OIKONOME_PLAID_ENV", str(cm.exception))


class DisconnectRelease(unittest.TestCase):
    def test_a_disconnect_still_releases_under_an_unrecognised_env(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        item = f"pl-disc-{uuid.uuid4().hex[:10]}"
        sync_base.upsert_item(conn, item, "plaid", "Q Bank", "access-disc")
        seen = []

        def remove_item(client, token):
            seen.append((client.base_url, token))
            return {}
        with mock.patch.dict(os.environ, {**_PLATFORM,
                                          "OIKONOME_PLAID_ENV": "development"}), \
                mock.patch.object(plaid.Client, "remove_item", remove_item), \
                mock.patch.object(plaid, "stamp_ledger_removed"):
            out = sync_base.disconnect_item(conn, item)
        self.assertTrue(out["plaid_released"])
        self.assertEqual(seen, [(plaid.ENV_URLS["production"],
                                 "access-disc")])


if __name__ == "__main__":
    unittest.main()
