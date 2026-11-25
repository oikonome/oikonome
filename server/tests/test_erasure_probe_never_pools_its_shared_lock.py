"""The erasure probe a refused push runs (`erasure.being_deleted(tid, conn)`)
takes the push writer lock SHARED on the door's POOLED tenant connection
and releases it again. If that release fails, the session must not go
back to the pool still holding the shared lock: an erasure takes the
same key exclusive and would wait on it forever. The handle is
discarded instead, as `tenancy.release_lock` does for exclusive locks.

The TAKE is held to the same rule: a take that raises (a statement
timeout or a cancel can land after the server granted the lock, and a
connection inside an aborted transaction refuses every statement) leaves
the session's lock state unknown, so the handle is discarded and the
probe answers from the control plane instead of crashing."""
import unittest

import psycopg

from oikonome import erasure
from oikonome.db import tenancy

from .util import make_db


class ErasureProbeNeverPoolsItsSharedLock(unittest.TestCase):

    def setUp(self):
        self.probe = make_db()
        self.addCleanup(self.probe.close)
        self.tid = self.probe.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def test_a_failed_shared_unlock_discards_the_pooled_connection(self):
        conn = tenancy.tenant_connect(self.tid)
        real = conn._conn.execute

        def execute(sql, *a, **kw):
            if "pg_advisory_unlock_shared" in sql:
                raise psycopg.OperationalError("unlock could not be sent")
            return real(sql, *a, **kw)

        conn.execute = execute
        try:
            erasure.being_deleted(self.tid, conn)
        except psycopg.OperationalError:
            pass
        del conn.execute
        self.assertTrue(getattr(conn, "_discarded", False),
                        "the connection went back to the pool still "
                        "holding the shared push lock")
        conn.close()

    def test_the_probe_still_answers_from_the_control_plane(self):
        conn = tenancy.tenant_connect(self.tid)
        real = conn._conn.execute

        def execute(sql, *a, **kw):
            if "pg_advisory_unlock_shared" in sql:
                raise psycopg.OperationalError("unlock could not be sent")
            return real(sql, *a, **kw)

        conn.execute = execute
        try:
            self.assertIs(erasure.being_deleted(self.tid, conn), False)
        finally:
            del conn.execute
            conn.close()

    def test_a_take_that_raises_discards_and_answers_from_the_control_plane(
            self):
        conn = tenancy.tenant_connect(self.tid)
        real = conn._conn.execute

        def execute(sql, *a, **kw):
            if "pg_try_advisory_lock_shared" in sql:
                # granted on the server, then the reply is lost
                real(sql, *a, **kw)
                raise psycopg.errors.QueryCanceled("statement timeout")
            return real(sql, *a, **kw)

        conn.execute = execute
        try:
            answer = erasure.being_deleted(self.tid, conn)
        finally:
            del conn.execute
        self.assertIs(answer, False)
        self.assertTrue(getattr(conn, "_discarded", False),
                        "the connection went back to the pool after a "
                        "take whose outcome is unknown")
        conn.close()

    def test_a_connection_in_a_failed_transaction_is_discarded(self):
        conn = tenancy.tenant_connect(self.tid)
        raw = conn._conn
        raw.execute("BEGIN")
        with self.assertRaises(psycopg.errors.DivisionByZero):
            raw.execute("SELECT 1/0")
        try:
            answer = erasure.being_deleted(self.tid, conn)
        finally:
            if not getattr(conn, "_discarded", False):
                raw.execute("ROLLBACK")
        self.assertIs(answer, False)
        self.assertTrue(getattr(conn, "_discarded", False),
                        "a connection in an aborted transaction went back "
                        "to the pool")
        conn.close()


if __name__ == "__main__":
    unittest.main()
