"""Every door into a sync takes the same per-tenant lock.

`sync_tenant` takes `pg_try_advisory_lock('oikonome:sync:<tenant>')` because
overlapping runs clobber `items.tx_cursor` — each writes the cursor it
started from, so one run's progress is lost and rows are re-pulled or
skipped. The webhook-driven scoped sync was a fifth door and the only one
outside that lock, so a webhook arriving during the hourly sweep did exactly
the damage the lock exists to prevent.
"""

import unittest

from oikonome.db import tenancy
from oikonome.jobs import worker

from .util import make_db


class WebhookSyncLockTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()
        cls.tid = cls.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_a_webhook_sync_skips_while_a_sync_is_running(self):
        holder = tenancy.tenant_connect(self.tid)
        try:
            got = holder.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (f"oikonome:sync:{self.tid}",)).fetchone()["ok"]
            self.assertTrue(got, "the test could not take the lock itself")
            out = worker._sync_item_body(self.tid, "item-whatever")
            self.assertEqual(out, {"skipped": "already-running"})
        finally:
            # explicit — tenant_connect is POOLED, so close() returns the
            # session with the lock still on it
            holder.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                           (f"oikonome:sync:{self.tid}",))
            holder.close()

    def test_it_releases_the_lock_so_the_next_one_can_run(self):
        # nothing holding it: the body runs and reaches the item lookup,
        # which finds nothing and returns 'gone' — the point is that it got
        # past the lock, and that the lock is free afterwards.
        out = worker._sync_item_body(self.tid, "item-nope")
        self.assertEqual(out, {"skipped": "gone"})
        probe = tenancy.tenant_connect(self.tid)
        try:
            free = probe.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (f"oikonome:sync:{self.tid}",)).fetchone()["ok"]
            self.assertTrue(free, "the lock was not released")
        finally:
            probe.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                          (f"oikonome:sync:{self.tid}",))
            probe.close()

    def test_release_lock_frees_the_lock_on_a_live_connection(self):
        key = f"oikonome:sync:{self.tid}"
        holder = tenancy.tenant_connect(self.tid)
        try:
            self.assertTrue(holder.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (key,)).fetchone()["ok"])
            self.assertIs(tenancy.release_lock(holder, key), False)
            probe = tenancy.tenant_connect(self.tid)
            try:
                self.assertTrue(probe.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                    (key,)).fetchone()["ok"], "the lock was not released")
                probe.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                              (key,))
            finally:
                probe.close()
        finally:
            holder.close()

    def test_a_failed_unlock_discards_the_connection(self):
        """When the unlock itself fails the session may still hold the
        lock, so the connection must be DROPPED, never returned to the
        pool — the doors used to call close() here, which is exactly the
        return-to-pool the comment above them says must not happen."""
        class Conn:
            discarded = closed = False

            def execute(self, *a, **kw):
                raise RuntimeError("server closed the connection")

            def discard(self):
                self.discarded = True

            def close(self):
                self.closed = True

        c = Conn()
        with self.assertLogs("oikonome.db.tenancy", level="ERROR"):
            self.assertIs(tenancy.release_lock(c, "oikonome:sync:x"), True)
        self.assertTrue(c.discarded)
        self.assertFalse(c.closed, "discard, not a return-to-pool close")

    def test_a_pooled_connection_is_never_returned_still_locked(self):
        """The reason the unlock is explicit and not left to close().

        `tenant_connect` is pooled: close() resets the scope and hands the
        SESSION back, so an advisory lock on it survives and the next
        borrower inherits a lock nobody meant to hold — after which every
        sync for that tenant is refused forever."""
        import inspect
        src = inspect.getsource(worker._sync_item_body)
        self.assertIn("tenancy.release_lock", src)
        self.assertIn("POOLED", src)


class CategorizePassSingleFlightTests(unittest.TestCase):
    """The sync-time categorize pass has one lock across its three doors
    (hourly sweep, webhook item sync, the ↻ button's background thread).
    Held elsewhere, the webhook's pass is skipped rather than run twice
    over one ledger."""

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()
        cls.tid = cls.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        cls.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, status) "
            "VALUES ('it-cat-lock', 'plaid', 'Lock Bank', 'ok')")

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_webhook_pass_skips_while_the_categorize_lock_is_held(self):
        from unittest import mock
        key = f"oikonome:sync-categorize:{self.tid}"
        holder = tenancy.tenant_connect(self.tid)
        try:
            self.assertTrue(holder.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (key,)).fetchone()["ok"])
            with mock.patch("oikonome.sync.plaid.sync",
                            return_value={"added": 0}), \
                 mock.patch("oikonome.engine.llm_categorize.categorize_new") as cat:
                worker._sync_item_body(self.tid, "it-cat-lock")
            self.assertEqual(cat.call_count, 0, "held lock means skip")
        finally:
            holder.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
            holder.close()
        # and with the lock free the pass runs, and releases the lock after
        with mock.patch("oikonome.sync.plaid.sync", return_value={"added": 0}), \
             mock.patch("oikonome.engine.llm_categorize.categorize_new") as cat:
            worker._sync_item_body(self.tid, "it-cat-lock")
        self.assertEqual(cat.call_count, 1)
        probe = tenancy.tenant_connect(self.tid)
        try:
            self.assertTrue(probe.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (key,)).fetchone()["ok"], "the categorize lock was not released")
            probe.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        finally:
            probe.close()
