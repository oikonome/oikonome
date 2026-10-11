"""The bank-link hold never changes which credentials resolve.

An access token or an MX user works only with the client that made it, so
standing the household's keys down in favour of the instance's would fail
every sync of the banks already linked, fire link alerts, and mint new
Items on the wrong account. The hold refuses doors instead: while it
stands, the household's own Plaid and MX credentials still resolve, and a
link refused under it never reaches the slot ledger."""

import os
import unittest
from unittest import mock

from oikonome.sync import mx as mx_mod
from oikonome.sync import plaid as plaid_mod

from .bank_link_hold_fixtures import (MX_CLIENT, PLAID_CLIENT, arm_hold,
                                      save_household_keys)
from .util import make_db


class HoldKeepsCredentials(unittest.TestCase):

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""})
        self._env.start()
        self.conn = make_db()
        save_household_keys(self.conn)
        arm_hold(self.conn)

    def tearDown(self):
        self.conn.close()
        self._env.stop()

    def test_the_households_own_credentials_still_resolve(self):
        cid, _secret, _url = plaid_mod.credentials(self.conn)
        self.assertEqual(cid, PLAID_CLIENT)
        c = mx_mod.creds(self.conn)
        self.assertIsNotNone(c)
        self.assertEqual(c["client_id"], MX_CLIENT)

    def test_a_link_refused_under_the_hold_never_takes_a_slot(self):
        client = plaid_mod.Client.for_tenant(self.conn)
        client.exchange_public_token = lambda _t: {
            "item_id": "it-new", "access_token": "access-new"}
        client.remove_item = lambda _t: None
        with mock.patch.object(plaid_mod.Client, "for_tenant",
                               return_value=client), \
                mock.patch.object(plaid_mod, "_record_slot") as slot:
            with self.assertRaises(ValueError):
                plaid_mod.link_item(self.conn, "public-x", "Test Bank")
        slot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
