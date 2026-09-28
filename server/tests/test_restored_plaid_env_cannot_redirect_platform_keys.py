"""A tenant's plaid_env applies only to the tenant's own keys.

A restored plaid_env=sandbox describes the keys that came with it. On an
instance using platform keys it must not send the platform's production
keys to sandbox.plaid.com.
"""

import os
import unittest
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
