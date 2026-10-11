"""A connection's break (and its recovery) is announced once, whichever
door saw it first.

The hourly sweep sends the "connection needs attention" mail and push, and
it used to decide there was an edge by comparing what this pass saw with
the item status it read at the start of the same pass. But every door that
pulls a bank writes that status itself — a bank-webhook pull, a one-bank
refresh. When one of those met the expired login first, it stamped
'error:ITEM_LOGIN_REQUIRED' and sent nothing; the next sweep then read the
error as the OLD status, saw the same error again, found no edge, and the
household was never told. A recovery seen first by another door was lost
the same way.

Pinned here: the sweep compares against what it last ANNOUNCED for the
item, not against whatever the row happened to say when the pass began.
"""

import unittest
from unittest import mock

from oikonome.jobs import worker
from oikonome.sync import plaid

from .util import accept_recipient_invite, make_db, write_config


def _login_expired(conn, item_id):
    # what plaid.sync does on every door: stamp the status, then raise
    conn.execute("UPDATE items SET status='error:ITEM_LOGIN_REQUIRED' "
                 "WHERE id=%s", (item_id,))
    raise plaid.PlaidError({"error_code": "ITEM_LOGIN_REQUIRED",
                            "error_type": "ITEM_ERROR",
                            "error_message": "the login is no longer valid"},
                           400)


def _healthy(conn, item_id):
    conn.execute("UPDATE items SET status='ok' WHERE id=%s", (item_id,))
    return {"added": 0}


class AnotherDoorSawItFirstTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, email_recipients=["me@x.dev"])
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        accept_recipient_invite(self.tid, "me@x.dev")
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token, status) VALUES "
            "('pl-1','plaid','Demo Bank','enc-tok','ok')")

    def tearDown(self):
        self.conn.close()

    def _sweep(self, pull):
        with mock.patch.object(worker.plaid, "sync", side_effect=pull), \
             mock.patch.object(worker.plaid, "sync_products",
                               return_value={"liabilities": 0,
                                             "holdings": 0}), \
             mock.patch.dict("os.environ",
                             {"OIKONOME_SMTP_HOST": "smtp.example.com"}), \
             mock.patch("oikonome.web.report.send") as send, \
             mock.patch("oikonome.notify.push.send_tenant"), \
             mock.patch("oikonome.notify.push_native.send_tenant") as native:
            worker.sync_tenant(self.tid)
        return send, native

    def _other_door(self, pull):
        """A pull that is not the sweep — the bank's webhook, say. It
        writes the item's status and announces nothing."""
        try:
            pull(self.conn, "pl-1")
        except plaid.PlaidError:
            pass

    def test_a_break_seen_first_by_a_webhook_pull_is_still_announced(self):
        send, _ = self._sweep(_healthy)                 # healthy, nothing
        send.assert_not_called()
        self._other_door(_login_expired)
        send, native = self._sweep(_login_expired)
        send.assert_called_once()
        self.assertTrue(send.call_args.args[0].startswith("⚠️"))
        native.assert_called_once()
        send, native = self._sweep(_login_expired)      # and only once
        send.assert_not_called()
        native.assert_not_called()

    def test_a_recovery_seen_first_by_a_webhook_pull_is_still_announced(self):
        send, _ = self._sweep(_login_expired)           # break announced
        send.assert_called_once()
        self._other_door(_healthy)
        send, _ = self._sweep(_healthy)
        send.assert_called_once()
        self.assertTrue(send.call_args.args[0].startswith("✅"))
        send, _ = self._sweep(_healthy)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
