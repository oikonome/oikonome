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



class LockHoldersThatDoNotSyncAnswerTheNudge(unittest.TestCase):
    """Disconnects, reconnects and one-bank pulls hold the same lock but
    never make the full pass a turned-away sync is waiting for. Once they
    let go, the waiting sync is queued — or it waits for the hourly sweep."""

    setUp = SyncNudgeTests.setUp
    _pending = SyncNudgeTests._pending

    def _queued(self):
        return mock.patch.object(worker, "_enqueue_tenant_sync")

    def _turn_a_sync_away(self):
        self.assertEqual(worker.sync_tenant(self.tid),
                         {"_status": "already-running"})

    def test_a_sync_turned_away_by_a_lock_holder_is_queued_after_it(self):
        from oikonome.sync import base as sync_base
        with self._queued() as queued:
            with sync_base.tenant_sync_lock(self.conn, self.tid) as held:
                self.assertTrue(held)
                self._turn_a_sync_away()
                queued.assert_not_called()   # not while the lock is held
            queued.assert_called_once_with(self.tid)
        self.assertEqual(self._pending(), 0, "the request was not consumed")

    def test_a_nested_holder_answers_only_when_the_lock_is_let_go(self):
        """A bulk import holds the lock and takes it again for each file.
        The inner release only lowers the count, so answering there queues
        a sync that finds the lock still held and bounces back — once per
        file. The answer comes once, from the release that frees it."""
        from oikonome.sync import base as sync_base
        with self._queued() as queued:
            with sync_base.tenant_sync_lock(self.conn, self.tid) as outer:
                self.assertTrue(outer)
                for _file in range(3):
                    with sync_base.tenant_sync_lock(self.conn,
                                                    self.tid) as inner:
                        self.assertTrue(inner)
                        self._turn_a_sync_away()
                    queued.assert_not_called()
                self.assertEqual(self._pending(), 1,
                                 "an inner release consumed the request")
            queued.assert_called_once_with(self.tid)
        self.assertEqual(self._pending(), 0, "the request was not consumed")

    def test_a_holder_with_no_request_waiting_queues_nothing(self):
        from oikonome.sync import base as sync_base
        with self._queued() as queued:
            with sync_base.tenant_sync_lock(self.conn, self.tid):
                pass
        queued.assert_not_called()

    def test_the_disconnect_door_answers_a_sync_it_turned_away(self):
        from oikonome.sync import base as sync_base
        from oikonome.web import api

        def disconnect(conn, item_id):
            self._turn_a_sync_away()
            return {"plaid_released": False, "note": ""}

        with self._queued() as queued, \
                mock.patch.object(sync_base, "disconnect_item", disconnect):
            api._disconnect_under_sync_lock(self.conn, self.tid, "it1")
        queued.assert_called_once_with(self.tid)

    def test_the_webhook_pull_answers_a_sync_it_turned_away(self):
        """The webhook job pulls one item; the full sync it turned away
        wanted all of them."""
        def pull(conn, item_id):
            self._turn_a_sync_away()
            return {}

        with self._queued() as queued, \
                mock.patch.object(worker.plaid, "sync", pull):
            worker._sync_item_body(self.tid, "it1")
        queued.assert_called_once_with(self.tid)
        self.assertEqual(self._pending(), 0)


    def test_a_one_bank_sync_now_answers_a_sync_it_turned_away(self):
        from oikonome.web import pages
        self.conn.execute("UPDATE items SET aggregator='plaid' "
                          "WHERE id='it1'")

        def pull(conn, item_id):
            self._turn_a_sync_away()
            return {"added": 1}

        user = {"tenant_id": self.tid, "user_id": "u", "role": "owner",
                "email": "o@example.dev"}
        with self._queued() as queued, \
                mock.patch.object(pages.plaid, "sync", pull), \
                mock.patch.object(pages.plaid, "sync_products"), \
                mock.patch.object(pages, "_categorize_after_pull_async"):
            outcome, _ = pages._account_sync(user, "it1")
        self.assertEqual(outcome, "ok")
        queued.assert_called_once_with(self.tid)

    _hold_the_lock = SyncNudgeTests._hold_the_lock
    _write_nudge = SyncNudgeTests._write_nudge

    def _unlock_then_report_discarded(self):
        """Release the lock for real (a leaked lock would poison the pool
        for later tests) but answer as release_lock does when the unlock
        failed and the connection was dropped."""
        real = tenancy.release_lock

        def release(conn, key):
            real(conn, key)
            return True
        return mock.patch.object(tenancy, "release_lock", release)

    def test_a_refused_restore_leaves_the_nudge_for_the_real_holder(self):
        """A restore that never got the lock is not the holder; claiming
        the request would leave the sync that does hold it nothing to
        answer when it lets go."""
        from oikonome.sync import restore_job
        self._hold_the_lock()
        self._write_nudge()
        with self._queued() as queued:
            restore_job._run(self.tid, b"not read")
        queued.assert_not_called()
        self.assertEqual(self._pending(), 1, "the refused restore took it")

    def test_a_restore_holding_the_lock_answers_a_sync_it_turned_away(self):
        from oikonome.sync import restore, restore_job
        self._write_nudge()
        with self._queued() as queued, mock.patch.object(
                restore, "restore_zip", side_effect=ValueError("bad file")):
            restore_job._run(self.tid, b"not read")
        queued.assert_called_once_with(self.tid)
        self.assertEqual(self._pending(), 0)

    def test_a_restore_whose_unlock_dropped_the_connection_claims_nothing(self):
        from oikonome.sync import restore, restore_job
        self._write_nudge()
        with mock.patch.object(worker, "answer_sync_nudge") as answer, \
                self._unlock_then_report_discarded(), mock.patch.object(
                    restore, "restore_zip", side_effect=ValueError("bad")):
            restore_job._run(self.tid, b"not read")
        answer.assert_not_called()

    def test_the_reap_answers_a_sync_it_turned_away(self):
        from oikonome.jobs import reaper
        self._write_nudge()
        with self._queued() as queued, \
                mock.patch.object(reaper, "reap", return_value={}):
            reaper.reap_tenant(self.tid)
        queued.assert_called_once_with(self.tid)

    def test_a_reap_whose_unlock_dropped_the_connection_claims_nothing(self):
        from oikonome.jobs import reaper
        self._write_nudge()
        with mock.patch.object(worker, "answer_sync_nudge") as answer, \
                self._unlock_then_report_discarded(), \
                mock.patch.object(reaper, "reap", return_value={}):
            reaper.reap_tenant(self.tid)
        answer.assert_not_called()

    def test_the_queued_sync_is_the_full_tenant_sync(self):
        """What is queued is the job that pulls every bank, and a queue that
        is down is logged rather than failing the door that asked."""
        pool = mock.AsyncMock()

        class _Inline:
            def __init__(self, target, **kw):
                self.target = target

            def start(self):
                self.target()

        with mock.patch.object(worker.threading, "Thread", _Inline), \
                mock.patch("arq.create_pool", mock.AsyncMock(return_value=pool)):
            worker._enqueue_tenant_sync(self.tid)
        pool.enqueue_job.assert_awaited_once_with("sync_one", self.tid)
        with mock.patch.object(worker.threading, "Thread", _Inline), \
                mock.patch("arq.create_pool",
                           mock.AsyncMock(side_effect=OSError("down"))), \
                self.assertLogs("oikonome", level="WARNING"):
            worker._enqueue_tenant_sync(self.tid)


if __name__ == "__main__":
    unittest.main()
