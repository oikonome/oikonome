"""Two different clocks, and the UI must be able to tell them apart.

`sync_log.ran_at` is when WE last read the aggregator's copy of an item.
`items.bank_updated_at` is when the BANK last refreshed that copy. A sync
reads the copy and cannot make the bank send more, so a pull that adds
nothing is the ordinary case — and without the second clock it is
indistinguishable from a broken connection.
"""

import json
import unittest

import httpx

from oikonome.sync import base as sync_base
from oikonome.sync import plaid

from .util import make_db, write_config

ACCOUNTS = {"accounts": [{"account_id": "chk", "name": "Checking",
                          "type": "depository", "subtype": "checking",
                          "balances": {"current": 100.0}}],
            "item": {"item_id": "it-p"}}
ITEM = {"item": {"item_id": "it-p"},
        "status": {"transactions": {
            "last_successful_update": "2026-08-20T13:49:00Z",
            "last_failed_update": "2026-08-15T05:05:31Z"}}}
EMPTY_SYNC = {"added": [], "modified": [], "removed": [],
              "next_cursor": "c1", "has_more": False,
              "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE"}


def _transport(item=ITEM, item_status=200):
    def handler(request):
        path = request.url.path
        if path == "/accounts/get":
            return httpx.Response(200, text=json.dumps(ACCOUNTS))
        if path == "/transactions/sync":
            return httpx.Response(200, text=json.dumps(EMPTY_SYNC))
        if path == "/item/get":
            return httpx.Response(item_status, text=json.dumps(item))
        return httpx.Response(404, text="{}")
    return httpx.MockTransport(handler)


class BankUpdateClockTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, plaid_client_id="cid", plaid_secret="sec",
                     plaid_env="sandbox")
        sync_base.upsert_item(self.conn, "it-p", "plaid", "Demo",
                              "access-prod-abc")

    def tearDown(self):
        self.conn.close()

    def _stamp(self):
        return self.conn.execute(
            "SELECT bank_updated_at, bank_failed_at FROM items "
            "WHERE id='it-p'").fetchone()

    def test_sync_records_when_the_bank_last_fed_the_aggregator(self):
        plaid.sync(self.conn, "it-p", transport=_transport())
        row = self._stamp()
        self.assertIsNotNone(row["bank_updated_at"])
        self.assertEqual(row["bank_updated_at"].strftime("%Y-%m-%d %H:%M"),
                         "2026-08-20 13:49")
        self.assertEqual(row["bank_failed_at"].strftime("%Y-%m-%d"),
                         "2026-08-15")

    def test_a_sync_that_adds_nothing_still_moves_the_bank_clock(self):
        """The whole point: the quiet sync is the one that needs
        explaining, so it must be the one that refreshes this figure."""
        res = plaid.sync(self.conn, "it-p", transport=_transport())
        self.assertEqual(res["added"], 0)
        self.assertIsNotNone(self._stamp()["bank_updated_at"])

    def test_an_unreadable_item_status_never_fails_the_sync(self):
        """A cosmetic timestamp must not turn a successful pull into a
        failed one."""
        res = plaid.sync(self.conn, "it-p",
                         transport=_transport(item={"error_code": "X",
                                                    "error_type": "ITEM_ERROR",
                                                    "error_message": "no"},
                                              item_status=400))
        self.assertEqual(res["added"], 0)
        self.assertIsNone(self._stamp()["bank_updated_at"])
        self.assertEqual(
            self.conn.execute("SELECT status FROM items WHERE id='it-p'")
            .fetchone()["status"], "ok")

    def test_an_archived_item_is_never_restamped(self):
        """Disconnect can land while a sync is in flight; archived wins,
        exactly as it does for status."""
        self.conn.execute("UPDATE items SET status='archived' WHERE id='it-p'")
        client = plaid.Client.for_tenant(self.conn, transport=_transport())
        plaid.record_bank_update(self.conn, client, "access-prod-abc", "it-p")
        self.assertIsNone(self._stamp()["bank_updated_at"])

    def test_connections_payload_carries_both_clocks(self):
        from oikonome.web.pages import _connections
        plaid.sync(self.conn, "it-p", transport=_transport())
        row = [r for r in _connections(self.conn) if r["id"] == "it-p"][0]
        self.assertIn("last_ok", row)
        self.assertIsNotNone(row["bank_updated_at"])


if __name__ == "__main__":
    unittest.main()
