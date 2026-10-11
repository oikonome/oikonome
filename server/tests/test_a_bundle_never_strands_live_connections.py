"""A connections bundle cannot swap the aggregator keys out from under live
connections.

A Plaid access token and an MX user only work with the client (and
environment) that made them. The dedicated key doors refuse a swap while
connections made under the current keys are live; a bundle carrying other
keys must be refused the same way, or every live bank is left syncing
nothing and still billing, with nothing left to release it by. A live MX
user's guid is never replaced by the bundle's, and the import runs under
the link lock and the sync lock the key doors take.
"""

import os
import unittest
from unittest import mock

from oikonome.db import crypto
from oikonome.engine import budget
from oikonome.sync import base as sync_base
from oikonome.sync import connections_bundle as cb

from .util import make_db, write_config


def _payload(**config):
    return {"oikonome_config": cb.FORMAT, "config": config, "secrets": {},
            "items": []}


class BundleKeepsLiveConnections(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def test_other_plaid_keys_are_refused_while_a_plaid_item_is_live(self):
        write_config(self.conn, plaid_client_id="cid-A",
                     plaid_secret=crypto.encrypt(self.conn, "s"),
                     plaid_env="production")
        sync_base.upsert_item(self.conn, "pl1", "plaid", "Bank", "tok-A")
        with self.assertRaises(ValueError):
            cb.apply_payload(self.conn, _payload(plaid_client_id="cid-B"))
        self.assertEqual(budget.load_config(self.conn)["plaid_client_id"],
                         "cid-A")

    def test_the_same_plaid_client_still_imports(self):
        write_config(self.conn, plaid_client_id="cid-A",
                     plaid_env="production")
        sync_base.upsert_item(self.conn, "pl1", "plaid", "Bank", "tok-A")
        cb.apply_payload(self.conn, _payload(plaid_client_id="cid-A",
                                             plaid_env="production",
                                             smtp_host="smtp.example.net"))
        self.assertEqual(budget.load_config(self.conn)["smtp_host"],
                         "smtp.example.net")

    def test_other_mx_keys_are_refused_while_an_mx_member_is_live(self):
        write_config(self.conn, mx_client_id="mx-A", mx_env="production",
                     mx_user_guid="USR-live")
        sync_base.upsert_item(self.conn, "mx1", "mx", "Bank", None)
        with self.assertRaises(ValueError):
            cb.apply_payload(self.conn, _payload(mx_client_id="mx-B"))

    def test_a_live_mx_user_keeps_its_guid(self):
        write_config(self.conn, mx_client_id="mx-A", mx_env="production",
                     mx_user_guid="USR-live")
        sync_base.upsert_item(self.conn, "mx1", "mx", "Bank", None)
        cb.apply_payload(self.conn, _payload(mx_client_id="mx-A",
                                             mx_user_guid="USR-other"))
        self.assertEqual(budget.load_config(self.conn)["mx_user_guid"],
                         "USR-live")

    def test_the_import_takes_the_link_lock(self):
        from oikonome.sync import plaid
        with mock.patch.object(plaid, "link_lock") as lock:
            cb.apply_payload(self.conn, _payload(smtp_host="smtp.example.net"))
        lock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
