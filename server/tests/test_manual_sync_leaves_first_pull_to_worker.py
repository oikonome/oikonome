"""A bank's first pull runs on the worker, never behind the per-bank
"sync now" button.

A new Item's first pull brings in up to two years of history and takes
minutes. The worker runs at most a fixed number of those at once, so many
households linking at the same time cannot exhaust the database pool. The
button is shown on every connection, including one that still reads "first
sync pending". If pressing it ran the backfill inline, the backfill would
hold a pooled web connection for minutes, outlive the edge proxy's timeout,
and announce the new bank's recent history to the household's webhooks as
new charges. Pressing it must queue the first-sync job (one id per Item, so
a repeat press adds nothing) and say the history is still arriving."""

import unittest
from unittest import mock

from oikonome.db import tenancy

from .util import make_db


class ManualSyncLeavesFirstPullToWorker(unittest.TestCase):
    def setUp(self):
        conn = make_db()
        try:
            self.tid = conn.execute(
                "SELECT current_setting('app.tenant_id') AS t"
            ).fetchone()["t"]
            # linked a moment ago, no pull finished yet
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, status)"
                " VALUES ('pl-new', 'plaid', 'New Bank', 'ok'),"
                " ('pl-old', 'plaid', 'Old Bank', 'ok')")
            conn.execute("INSERT INTO sync_log (item_id, added) "
                         "VALUES ('pl-old', 0)")
        finally:
            conn.close()
        self.user = {"tenant_id": self.tid, "user_id": None, "role": "owner"}

    def _press(self, item_id):
        from oikonome.jobs import worker
        from oikonome.web import pages
        with mock.patch("oikonome.sync.plaid.sync",
                        return_value={"added": 0}) as pulled, \
             mock.patch("oikonome.sync.plaid.sync_products") as products, \
             mock.patch.object(worker, "queue_first_sync") as queued, \
             mock.patch.object(worker, "_webhooks_after_sync") as hooks, \
             mock.patch.object(pages, "_categorize_after_pull_async"), \
             mock.patch.object(pages, "_tenant_sync_allowed",
                               return_value=True), \
             mock.patch("oikonome.web.demoguard.deny"):
            outcome, msg = pages._account_sync(self.user, item_id)
        return outcome, msg, pulled, products, queued, hooks

    def test_a_never_pulled_bank_is_queued_not_pulled_inline(self):
        outcome, msg, pulled, products, queued, hooks = self._press("pl-new")
        pulled.assert_not_called()
        products.assert_not_called()
        hooks.assert_not_called()
        queued.assert_called_once_with(self.tid, "pl-new")
        self.assertEqual(outcome, "ok")
        self.assertIn("New Bank", msg)
        self.assertIn("still arriving", msg)

    def test_the_answer_never_claims_a_queue_it_cannot_see(self):
        """The first-sync job is queued off the request, so a Redis that is
        down is only logged: the answer must not say the job is queued,
        and names the hourly sync that queues it again."""
        _outcome, msg, *_ = self._press("pl-new")
        self.assertNotIn("is queued", msg)
        self.assertIn("hourly sync", msg)

    def test_a_bank_pulled_before_still_syncs_inline(self):
        outcome, _msg, pulled, _products, queued, _hooks = \
            self._press("pl-old")
        self.assertEqual(outcome, "ok")
        pulled.assert_called_once()
        queued.assert_not_called()

    def test_the_tenant_lock_is_released_after_queueing(self):
        self._press("pl-new")
        conn = tenancy.tenant_connect(self.tid)
        try:
            got = conn.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (f"oikonome:sync:{self.tid}",)).fetchone()["ok"]
            self.assertTrue(got, "the door left the household's sync "
                                 "lock held")
            tenancy.release_lock(conn, f"oikonome:sync:{self.tid}")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
