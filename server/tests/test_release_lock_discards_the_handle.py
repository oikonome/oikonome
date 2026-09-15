"""A pooled connection whose advisory unlock failed is DEAD, loudly.

`release_lock` is the one place that knows what to do when
`pg_advisory_unlock` raises: the session may still be holding the
tenant's sync/email lock, so handing it back to the pool would leak that
lock to the next borrower and refuse every later sync for that tenant
until the backend died. It drops the connection instead.

Dropping it is only half the rule. The caller is holding a handle to a
connection that no longer exists, and the single-flight helpers
(`sync_base.tenant_sync_lock` and friends) release inside a `finally` and
then hand control back to a caller that carries on using `conn`. Before
this, that caller's next statement failed with psycopg's "the connection
is closed" — true, unexplained, and attributed to whatever line happened
to run next rather than to the unlock three frames up.

So: the discard is real (the lock it could not release is gone with the
backend session, and the next acquisition succeeds), using the handle
afterwards says exactly what happened, and close() stays a harmless
no-op because every door calls it in its own `finally`.
"""

import unittest

import psycopg

from oikonome.db import tenancy

from .util import make_db


class _UnlockRaises:
    """The failure `release_lock` exists for: the unlock statement itself
    blows up (a connection dropped mid-release, a server that went away),
    so the session goes back to the pool with the lock still on it."""

    def __init__(self, conn):
        self._conn = conn

    def __call__(self, *_a, **_kw):
        raise psycopg.OperationalError("unlock could not be sent")


class ReleaseLockDiscardTests(unittest.TestCase):

    def setUp(self):
        self.probe = make_db()
        self.addCleanup(self.probe.close)
        self.tid = self.probe.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.key = f"oikonome:sync:{self.tid}"

    def _locked_connection(self):
        conn = tenancy.tenant_connect(self.tid)
        self.assertTrue(conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (self.key,)).fetchone()["ok"])
        return conn

    def _lock_is_free(self) -> bool:
        c = tenancy.tenant_connect(self.tid)
        try:
            got = c.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                            (self.key,)).fetchone()["ok"]
            if got:
                c.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                          (self.key,))
            return got
        finally:
            c.close()

    def test_a_failed_unlock_drops_the_connection_and_frees_the_lock(self):
        conn = self._locked_connection()
        conn.execute = _UnlockRaises(conn)
        self.assertIs(tenancy.release_lock(conn, self.key), True,
                      "release_lock did not report the discard")
        del conn.execute
        # the lock it could not release died with the backend session, so
        # the next sync for this tenant is not refused by a lock nobody
        # holds on purpose
        self.assertTrue(self._lock_is_free(),
                        "the advisory lock outlived the discarded handle")

    def test_using_the_discarded_handle_says_so(self):
        conn = self._locked_connection()
        conn.execute = _UnlockRaises(conn)
        tenancy.release_lock(conn, self.key)
        del conn.execute
        with self.assertRaises(tenancy.ConnectionDiscarded) as cm:
            conn.execute("SELECT 1")
        self.assertIn("discarded", str(cm.exception))
        with self.assertRaises(tenancy.ConnectionDiscarded):
            with conn.transaction():
                pass                                       # pragma: no cover
        # the psycopg surface reached through __getattr__ is just as dead
        with self.assertRaises(tenancy.ConnectionDiscarded):
            conn.cursor()

    def test_close_after_a_discard_is_still_a_no_op(self):
        """Every door releases in a `finally` and then closes. That close
        must not become the thing that raises, or the discard turns a
        handled failure into an unhandled one."""
        conn = self._locked_connection()
        conn.execute = _UnlockRaises(conn)
        tenancy.release_lock(conn, self.key)
        del conn.execute
        conn.close()
        conn.close()

    def test_a_control_connection_is_the_same(self):
        """The control-plane handle duck-types the same surface and is
        discarded by the same helper — the email sweep holds its lock on
        one."""
        conn = tenancy.control_connect()
        try:
            conn.execute = _UnlockRaises(conn)
            self.assertIs(tenancy.release_lock(conn, self.key), True)
            del conn.execute
            with self.assertRaises(tenancy.ConnectionDiscarded):
                conn.execute("SELECT 1")
        finally:
            conn.close()

    def test_the_sync_lock_helper_leaves_a_handle_that_cannot_be_used_blind(
            self):
        """End to end through the helper every sync door actually uses:
        `tenant_sync_lock` releases in its own `finally` and yields control
        back with no signal that the connection went with it. The caller's
        next statement must name the cause."""
        from oikonome.sync import base as sync_base

        conn = tenancy.tenant_connect(self.tid)
        try:
            with sync_base.tenant_sync_lock(conn, self.tid) as held:
                self.assertTrue(held)
                conn.execute = _UnlockRaises(conn)
            del conn.execute
            with self.assertRaises(tenancy.ConnectionDiscarded):
                conn.execute("SELECT 1")
        finally:
            conn.close()
        self.assertTrue(self._lock_is_free())


if __name__ == "__main__":
    unittest.main()
