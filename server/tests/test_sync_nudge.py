"""A sync turned away by the lock must not cost the tenant an hour.

The per-tenant sync lock is single-flight and SKIP-not-retry: a second
sync finds it held, returns "already-running" and goes away. Skipping is
right — waiting behind a multi-minute pull holds a worker slot — but the
REASON the second sync existed does not go away with it. A Plaid webhook
fires because there is something new; a fresh bank link wants its first
pull; a person pressing ↻ is watching. Left alone, all three wait for
the next hourly sweep, and the run in flight may already have passed the
item's cursor, so "the run already going covers it" is not true.

So the skipped caller leaves a nudge and the holder answers it: one more
pass before it releases the lock. These pin the protocol end to end —
the skip records it, the holder makes exactly one extra pass, the request
is consumed so nobody answers it twice, and a request that was already
satisfied cannot buy every later sweep a second pass for ever.
"""

import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.jobs import worker

from .util import make_db


class SyncNudgeTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.key = f"oikonome:sync:{self.tid}"

    def _pending(self) -> int:
        return len(self.conn.execute(
            "SELECT 1 FROM job_progress WHERE id='sync-nudge'").fetchall())

    def _write_nudge(self):
        worker._nudge_sync(self.conn)

    def _hold_the_lock(self):
        """Take the tenant sync lock on a connection of its own, the way a
        run in flight holds it. tenant_connect is POOLED, so the unlock is
        explicit — close() would hand the session back still locked."""
        holder = tenancy.tenant_connect(self.tid)
        self.assertTrue(holder.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (self.key,)).fetchone()["ok"], "the test could not take the lock")

        def _release():
            holder.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                           (self.key,))
            holder.close()
        self.addCleanup(_release)

    def test_a_skipped_sync_asks_the_run_in_flight_for_another_pass(self):
        self._hold_the_lock()
        self.assertEqual(worker.sync_tenant(self.tid),
                         {"_status": "already-running"})
        self.assertEqual(self._pending(), 1,
                         "the skipped sync left no request, so its reason "
                         "for syncing waits for the hourly sweep")

    def test_a_skipped_webhook_sync_asks_too(self):
        """The webhook door skips on the same lock, and it is the door with
        the strongest claim: Plaid only calls it because something new
        landed."""
        self._hold_the_lock()
        self.assertEqual(worker._sync_item_body(self.tid, "item-whatever"),
                         {"skipped": "already-running"})
        self.assertEqual(self._pending(), 1)

    def test_the_holder_answers_a_nudge_with_one_more_pass(self):
        passes = []

        def pass_(conn, tenant_id, since_days, progress, results, **kw):
            passes.append(tenant_id)
            if len(passes) == 1:
                # a sync arrives while this pass is running and is skipped
                self._write_nudge()
            return 0

        with mock.patch.object(worker, "_sync_pass", pass_):
            worker.sync_tenant(self.tid)
        self.assertEqual(len(passes), 2,
                         "the request recorded mid-pull was not answered")
        self.assertEqual(self._pending(), 0,
                         "the request was not consumed — the next sweep "
                         "would answer it a second time")

    def test_one_extra_pass_and_no_more(self):
        """A holder that kept chasing nudges would never release the lock on
        a busy tenant. A request arriving during the EXTRA pass is left for
        the next sweep."""
        passes = []

        def pass_(conn, tenant_id, since_days, progress, results, **kw):
            passes.append(tenant_id)
            self._write_nudge()          # every pass is nudged again
            return 0

        with mock.patch.object(worker, "_sync_pass", pass_):
            worker.sync_tenant(self.tid)
        self.assertEqual(len(passes), 2)

    def test_an_uncontended_sync_makes_exactly_one_pass(self):
        passes = []

        def pass_(conn, tenant_id, since_days, progress, results, **kw):
            passes.append(tenant_id)
            return 0

        with mock.patch.object(worker, "_sync_pass", pass_):
            worker.sync_tenant(self.tid)
        self.assertEqual(len(passes), 1)

    def test_a_request_the_pull_already_answers_is_not_paid_for_twice(self):
        """A nudge left over from before this run started is satisfied by
        the pull about to happen. Left standing it would hand every later
        sweep a second pass, for ever, for a tenant nobody is waiting on."""
        self._write_nudge()
        passes = []

        def pass_(conn, tenant_id, since_days, progress, results, **kw):
            passes.append(tenant_id)
            return 0

        with mock.patch.object(worker, "_sync_pass", pass_):
            worker.sync_tenant(self.tid)
        self.assertEqual(len(passes), 1)
        self.assertEqual(self._pending(), 0)

    def test_the_nudge_row_is_not_the_sync_progress_row(self):
        """The UI reads job_progress id='sync' to say whether a sync is
        running; a request must not look like one, or the header chip
        reports a pull that is not happening."""
        self._write_nudge()
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM job_progress WHERE id='sync'").fetchone())


if __name__ == "__main__":
    unittest.main()
