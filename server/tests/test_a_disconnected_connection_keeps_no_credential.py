"""A disconnected connection keeps no credential, and nothing revives it.

Disconnecting an exchange key or a SimpleFIN bridge means the owner is done
with it: a Coinbase item's token is the exchange's API key, and a SimpleFIN
bridge's is a permanent URL that reads every bank on the bridge. Nothing
reads either again once the connection is archived (it comes back only by
linking anew), so keeping them is a stored secret with no purpose — and the
connections bundle used to seal it into the export file, and an import of
that file switched the connection back on.
"""
import unittest
from pathlib import Path

from oikonome.sync import base, connections_bundle

from .util import make_db, write_config

MIGRATION = (Path(__file__).resolve().parents[1] / "oikonome" / "db"
             / "migrations" / "158_disconnected_items_forget_credentials.sql")


class DisconnectedCredentialTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _item(self, iid, agg, token, status="ok"):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name,"
            " access_token, status) VALUES (%s, %s, 'Test', %s, %s)",
            (iid, agg, token, status))

    def _token(self, iid):
        return self.conn.execute(
            "SELECT access_token FROM items WHERE id=%s",
            (iid,)).fetchone()["access_token"]

    def test_disconnecting_an_exchange_key_forgets_it(self):
        base.upsert_item(self.conn, "cb1", "coinbase", "Coinbase",
                         "organizations/x/apiKeys/y|-----BEGIN KEY-----")
        base.disconnect_item(self.conn, "cb1")
        self.assertIsNone(self._token("cb1"))

    def test_disconnecting_a_bridge_forgets_its_access_url(self):
        base.upsert_item(self.conn, "sf1", "simplefin", "SimpleFIN bridge",
                         "https://user:pass@bridge.example.test/simplefin")
        base.disconnect_item(self.conn, "sf1")
        self.assertIsNone(self._token("sf1"))

    def test_the_bundle_carries_no_disconnected_connection(self):
        base.upsert_item(self.conn, "live", "simplefin", "Bridge",
                         "https://user:pass@bridge.example.test/a")
        # a Plaid item whose release failed keeps its token for the reaper;
        # it is still not a link to hand to another install
        base.upsert_item(self.conn, "gone", "plaid", "Bank", "access-x")
        self.conn.execute("UPDATE items SET status='archived' WHERE id='gone'")
        ids = {it["id"] for it in
               connections_bundle.build_payload(self.conn)["items"]}
        self.assertIn("live", ids)
        self.assertNotIn("gone", ids)

    def test_importing_a_bundle_does_not_switch_a_disconnected_link_on(self):
        base.upsert_item(self.conn, "sf1", "simplefin", "Bridge",
                         "https://user:pass@bridge.example.test/a")
        payload = connections_bundle.build_payload(self.conn)
        base.disconnect_item(self.conn, "sf1")
        connections_bundle.apply_payload(self.conn, payload)
        row = self.conn.execute(
            "SELECT status, access_token FROM items WHERE id='sf1'"
        ).fetchone()
        self.assertEqual((row["status"], row["access_token"]),
                         ("archived", None))

    def test_credentials_already_kept_on_disconnected_items_are_dropped(self):
        self._item("cb-old", "coinbase", "key-material", "archived")
        self._item("sf-old", "simplefin", "https://u:p@h.test/x", "archived")
        self._item("pl-old", "plaid", "access-retry", "archived")
        self._item("plan", "plan_csv", "plan:csv", "archived")
        self._item("cb-live", "coinbase", "key-live")
        self.conn.execute(MIGRATION.read_text())
        self.assertIsNone(self._token("cb-old"))
        self.assertIsNone(self._token("sf-old"))
        # the reaper's retry still needs a failed Plaid release's token;
        # a plan item's token is a marker, not a secret; a live key stays
        self.assertEqual(self._token("pl-old"), "access-retry")
        self.assertEqual(self._token("plan"), "plan:csv")
        self.assertIsNotNone(self._token("cb-live"))


if __name__ == "__main__":
    unittest.main()
