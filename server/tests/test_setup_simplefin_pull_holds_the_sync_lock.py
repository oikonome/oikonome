"""The setup wizard's first SimpleFIN pull runs under the household's sync
lock, like every other connect door.

The bridge item is visible to the worker as soon as it is written. Without
the lock a scheduled sync can start a second deep pull of the same item
beside the wizard's, both writing the same accounts and transactions from
different snapshots. The wizard waits for a held lock rather than giving
up (it has no "try again" to answer with), connects only once it holds the
lock, and reports a failed connect — retried from the Accounts page — if
the lock never frees.
"""

import unittest
import uuid
from unittest import mock

import psycopg
from psycopg.rows import dict_row

from oikonome.db import tenancy
from oikonome.web import setup

from .util import _ensure_db


class SetupSimplefinTakesTheSyncLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        admin = tenancy.admin_connect()
        try:
            self.tid = str(tenancy.create_tenant(
                admin, f"sflock-{uuid.uuid4().hex[:8]}"))
            admin.commit()
        finally:
            admin.close()
        self.conn = tenancy.tenant_connect(self.tid)
        self.addCleanup(self.conn.close)
        self.other = psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                                     autocommit=True)
        self.addCleanup(self.other.close)
        self.key = f"oikonome:sync:{self.tid}"

    def _lock_free_elsewhere(self) -> bool:
        got = self.other.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (self.key,)).fetchone()["ok"]
        if got:
            self.other.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                               (self.key,))
        return got

    def test_it_waits_for_a_held_lock_and_connects_holding_it(self):
        self.other.execute("SELECT pg_advisory_lock(hashtext(%s))",
                           (self.key,))
        seen = {}

        def connect(conn, token, transport=None):
            seen["free_elsewhere"] = self._lock_free_elsewhere()
            return {"accounts": 0}

        def release(_s):
            self.other.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                               (self.key,))

        with mock.patch.object(setup, "connect_simplefin",
                               side_effect=connect) as c, \
                mock.patch("oikonome.jobs.worker.answer_sync_nudge"):
            setup._connect_simplefin_locked(self.conn, self.tid, "tok", None,
                                            sleep=release, wait_s=5)
        c.assert_called_once()
        self.assertFalse(seen["free_elsewhere"],
                         "the pull ran without holding the sync lock")
        self.assertTrue(self._lock_free_elsewhere(), "lock not given back")

    def test_a_lock_that_never_frees_fails_the_connect_without_pulling(self):
        self.other.execute("SELECT pg_advisory_lock(hashtext(%s))",
                           (self.key,))
        self.addCleanup(self.other.execute,
                        "SELECT pg_advisory_unlock(hashtext(%s))", (self.key,))
        with mock.patch.object(setup, "connect_simplefin") as c, \
                self.assertRaises(RuntimeError):
            setup._connect_simplefin_locked(self.conn, self.tid, "tok", None,
                                            sleep=lambda s: None, wait_s=0)
        c.assert_not_called()


if __name__ == "__main__":
    unittest.main()
