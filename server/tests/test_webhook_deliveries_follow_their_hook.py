"""A webhook's queued deliveries go with it.

Nothing in the database links a delivery to its webhook, and the sender
only picks up a delivery whose webhook still exists and is on. So a
delivery queued for a webhook that has been deleted is never sent, and as
long as it reads 'pending' the retention never ages it out: it would sit
in the table for good. The queue must not add one for a hook that is gone,
and the nightly prune must sweep any that a delete racing an emit left.
"""

import unittest
import uuid
from unittest import mock

from oikonome.engine.compat import jsonb
from oikonome.notify import webhooks

from .util import make_db


class DeliveriesFollowTheirHookTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.hook = str(self.conn.execute(
            "INSERT INTO webhooks (url, name, secret, events) VALUES "
            "('https://hooks.example.dev/in', 'test', 'x', %s) RETURNING id",
            (["test.ping"],)).fetchone()["id"])

    def _deliveries(self, hook_id):
        return self.conn.execute(
            "SELECT count(*) AS n FROM webhook_deliveries WHERE webhook_id=%s",
            (hook_id,)).fetchone()["n"]

    def test_a_hook_deleted_mid_emit_gets_no_delivery(self):
        def delete_then_encode(v):
            # the owner deletes the hook after emit has read the list
            webhooks.delete(self.conn, self.hook)
            return jsonb(v)

        with mock.patch.object(webhooks, "jsonb", delete_then_encode):
            self.assertEqual(webhooks.emit(self.conn, "test.ping", {}), 0)
        self.assertEqual(self._deliveries(self.hook), 0)

    def test_prune_sweeps_a_pending_delivery_whose_hook_is_gone(self):
        gone = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO webhook_deliveries (webhook_id, event, payload) "
            "VALUES (%s, 'test.ping', '{}')", (gone,))
        webhooks.emit(self.conn, "test.ping", {})     # a live hook's row
        self.assertEqual(webhooks.prune(self.conn), 1)
        self.assertEqual(self._deliveries(gone), 0)
        self.assertEqual(self._deliveries(self.hook), 1,
                         "prune took a live hook's pending delivery")

    def test_a_refused_delivery_does_not_undo_the_callers_transaction(self):
        """emit never raises, so a door calls it inside its own transaction.
        A payload the database refuses (a NUL in a label is valid JSON but
        not valid jsonb) must fail on its own: left to abort the caller's
        transaction, the caller's COMMIT would roll back the change the
        event was announcing."""
        with self.conn.transaction():
            self.conn.execute(
                "UPDATE webhooks SET name='renamed' WHERE id=%s", (self.hook,))
            self.assertEqual(webhooks.emit(
                self.conn, "test.ping", {"label": "a\x00b"}), 0)
            self.assertFalse(webhooks.wanted(self.conn, "no.such.event"))
        self.assertEqual(self.conn.execute(
            "SELECT name FROM webhooks WHERE id=%s",
            (self.hook,)).fetchone()["name"], "renamed")
        self.assertEqual(self._deliveries(self.hook), 0)


if __name__ == "__main__":
    unittest.main()
