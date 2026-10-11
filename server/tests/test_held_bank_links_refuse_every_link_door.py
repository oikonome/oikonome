"""While the bank-link hold stands, every door that hands bank logins to
the household's Plaid or MX client refuses, and says why.

A new Plaid link and a Plaid re-auth both send the bank login to the same
client, so both are refused at the one place every Plaid link starts; the
MX widget is refused before an MX user is created. A hold armed while a
Plaid link is already open is caught where the Item is written: a new
Item is handed back to Plaid (released at that client), and an Item the
household already held is never removed by the refusal."""

import os
import unittest
from unittest import mock

import httpx

from oikonome.sync import base as sync_base
from oikonome.sync import mx as mx_mod
from oikonome.sync import plaid as plaid_mod
from oikonome.web import pages

from .bank_link_hold_fixtures import arm_hold, save_household_keys
from .util import make_db


def _owner(conn):
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]
    return {"tenant_id": tid, "role": "owner", "user_id": 0,
            "email": "owner@example.dev"}


class HeldLinkDoors(unittest.TestCase):

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""})
        self._env.start()
        self.conn = make_db()
        save_household_keys(self.conn)

    def tearDown(self):
        self.conn.close()
        self._env.stop()

    def _start_refused(self, item_id=""):
        fake = mock.MagicMock()
        with mock.patch.object(plaid_mod.Client, "for_tenant",
                               return_value=fake):
            with self.assertRaises(ValueError) as caught:
                pages._start_plaid_session(_owner(self.conn),
                                           item_id=item_id)
        fake.create_hosted_link.assert_not_called()
        self.assertIn("on hold", str(caught.exception))
        self.assertIn("Plaid", str(caught.exception))

    def test_a_new_plaid_link_is_refused(self):
        arm_hold(self.conn)
        self._start_refused()

    def test_a_plaid_reauth_is_refused(self):
        sync_base.upsert_item(self.conn, "it-held", "plaid", "Test Bank",
                              "access-sandbox-held")
        arm_hold(self.conn)
        self._start_refused(item_id="it-held")

    def test_the_mx_widget_is_refused_before_any_mx_call(self):
        arm_hold(self.conn)
        calls = []

        def refuse(request):
            calls.append((request.method, request.url.path))
            return httpx.Response(500)
        with self.assertRaises(ValueError) as caught:
            mx_mod.connect_widget_url(self.conn,
                                      transport=httpx.MockTransport(refuse))
        self.assertEqual(calls, [])
        self.assertIn("MX", str(caught.exception))
        self.assertIn("on hold", str(caught.exception))

    def _link_with_hold_armed_mid_exchange(self, item_id):
        client = plaid_mod.Client.for_tenant(self.conn)
        removed = []

        def exchange(_tok):
            arm_hold(self.conn)          # the recovery lands mid-link
            return {"item_id": item_id, "access_token": "access-new"}
        client.exchange_public_token = exchange
        client.remove_item = lambda tok: removed.append(tok)
        with mock.patch.object(plaid_mod.Client, "for_tenant",
                               return_value=client):
            with self.assertRaises(ValueError) as caught:
                plaid_mod.link_item(self.conn, "public-x", "Test Bank")
        self.assertIn("on hold", str(caught.exception))
        return removed

    def test_a_link_finishing_under_a_new_hold_hands_its_item_back(self):
        removed = self._link_with_hold_armed_mid_exchange("it-new")
        self.assertEqual(removed, ["access-new"])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM items WHERE id='it-new'").fetchone())

    def test_a_reauth_finishing_under_a_new_hold_keeps_the_item(self):
        sync_base.upsert_item(self.conn, "it-old", "plaid", "Test Bank",
                              "access-old")
        removed = self._link_with_hold_armed_mid_exchange("it-old")
        self.assertEqual(removed, [])
        self.assertEqual(sync_base.get_access_token(self.conn, "it-old"),
                         "access-old")

    def test_with_no_hold_a_new_link_still_starts(self):
        fake = mock.MagicMock()
        fake.create_hosted_link.return_value = {
            "link_token": "link-sandbox-1", "hosted_link_url": "https://x"}
        with mock.patch.object(plaid_mod.Client, "for_tenant",
                               return_value=fake):
            out = pages._start_plaid_session(_owner(self.conn))
        self.assertEqual(out["kind"], "add")
        fake.create_hosted_link.assert_called_once()
        pages._PLAID_LINK.pop("link-sandbox-1", None)


if __name__ == "__main__":
    unittest.main()
