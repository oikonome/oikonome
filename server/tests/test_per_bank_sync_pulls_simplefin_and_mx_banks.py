"""The per-bank "sync now" button pulls SimpleFIN and MX banks.

SimpleFIN accounts hang off per-bank 'simplefin-org' child items (id
`<bridge>:<bank>`) that carry no token, and MX accounts hang off 'mx'
items. Every client draws the button on those banks, so pressing it must
run the pull — through the bridge for SimpleFIN, tenant-wide for MX — and
not answer that a live bank feed is "a manual connection" with nothing
pulled until the hourly sweep.
"""

import unittest
from unittest import mock

from .util import make_db


class PerBankSyncPullsSimplefinAndMx(unittest.TestCase):
    def setUp(self):
        conn = make_db()
        try:
            self.tid = conn.execute(
                "SELECT current_setting('app.tenant_id') AS t"
            ).fetchone()["t"]
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, status)"
                " VALUES ('sf-1', 'simplefin', 'SimpleFIN bridge', 'ok'),"
                " ('sf-1:acme-bank', 'simplefin-org', 'Acme Bank', 'ok'),"
                " ('sf-2', 'simplefin', 'SimpleFIN bridge', 'archived'),"
                " ('sf-2:old-bank', 'simplefin-org', 'Old Bank', 'ok'),"
                " ('mx-mbr-1', 'mx', 'Member Bank', 'ok'),"
                " ('csv-1', 'import', 'Statement Import', 'ok')")
        finally:
            conn.close()
        self.user = {"tenant_id": self.tid, "user_id": None, "role": "owner"}

    def _press(self, item_id):
        from oikonome.jobs import worker
        from oikonome.web import pages
        with mock.patch("oikonome.sync.simplefin.sync",
                        return_value={"transactions": 4}) as sf, \
             mock.patch("oikonome.sync.base.get_access_token",
                        return_value="https://bridge.invalid/x") as tok, \
             mock.patch("oikonome.sync.mx.sync",
                        return_value={"transactions": 2}) as mx, \
             mock.patch.object(worker, "_webhooks_after_sync") as hooks, \
             mock.patch.object(pages, "_categorize_after_pull_async"), \
             mock.patch.object(pages, "_tenant_sync_allowed",
                               return_value=True), \
             mock.patch("oikonome.web.demoguard.deny"):
            outcome, msg = pages._account_sync(self.user, item_id)
        return outcome, msg, sf, tok, mx, hooks

    def test_a_simplefin_bank_is_pulled_through_its_bridge(self):
        outcome, msg, sf, tok, mx, hooks = self._press("sf-1:acme-bank")
        self.assertEqual(outcome, "ok")
        sf.assert_called_once()
        self.assertEqual(sf.call_args.args[1], "sf-1")
        tok.assert_called_once()
        self.assertEqual(tok.call_args.args[1], "sf-1")
        mx.assert_not_called()
        self.assertNotIn("manual connection", msg)
        self.assertIn("Acme Bank", msg)
        self.assertEqual(list(hooks.call_args.args[1]), ["sf-1"])

    def test_an_mx_bank_runs_the_mx_pull(self):
        outcome, msg, sf, _tok, mx, hooks = self._press("mx-mbr-1")
        self.assertEqual(outcome, "ok")
        mx.assert_called_once()
        sf.assert_not_called()
        self.assertNotIn("manual connection", msg)
        hooks.assert_called_once()

    def test_a_bank_under_a_disconnected_bridge_is_not_pulled(self):
        """Pulling the bridge re-marks it live, so a bank under a bridge
        the owner disconnected must not undo the disconnect."""
        outcome, _msg, sf, *_ = self._press("sf-2:old-bank")
        self.assertEqual(outcome, "disconnected")
        sf.assert_not_called()

    def test_an_imported_connection_still_has_nothing_to_sync(self):
        outcome, msg, sf, _tok, mx, hooks = self._press("csv-1")
        self.assertEqual(outcome, "ok")
        self.assertIn("manual connection", msg)
        sf.assert_not_called()
        mx.assert_not_called()
        hooks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
