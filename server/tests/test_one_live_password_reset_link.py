"""Minting a password reset burns the older ones — including under a race.

`create_reset` promises in writing that the newest link is the only one that
works, so that someone who reads an older reset mail cannot use it after the
account holder has moved on. Two mints racing each other each burn only the
rows they can see, and neither can see the other's uncommitted insert, so both
links come out live and the promise is silently false exactly when it matters:
a double-submitted form, a retried POST, two tabs."""

import threading
import time
import unittest
import uuid

import psycopg
from psycopg.rows import dict_row

from oikonome.auth import reset
from oikonome.db import tenancy

from .util import _ensure_db


def _control():
    return psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                           autocommit=True)


class _PausingConn:
    """A real connection that stops at the instant the race turns on: after
    the burn has run and before the new row exists. Only the interleaving is
    chosen; the code under test is the real code on a real connection."""

    _BURN = "UPDATE password_resets SET used_at=now()"

    def __init__(self, conn, at_burn):
        self._conn = conn
        self._at = at_burn

    def execute(self, sql, params=None):
        cur = self._conn.execute(sql, params)
        if sql.startswith(self._BURN):
            self._at()
        return cur

    def transaction(self):
        return self._conn.transaction()


class OneLiveResetLink(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        from oikonome.auth import passwords
        self.conn = _control()
        self.addCleanup(self.conn.close)
        admin = tenancy.admin_connect()
        self.addCleanup(admin.close)
        tid = tenancy.create_tenant(admin, f"reset-{uuid.uuid4().hex[:8]}")
        self.uid = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash) "
            "VALUES (%s,%s,%s) RETURNING id",
            (tid, f"reset-{uuid.uuid4().hex[:6]}@example.dev",
             passwords.hash_password("correct-horse-battery"))).fetchone()["id"]

    def _live(self):
        return self.conn.execute(
            "SELECT id FROM password_resets WHERE user_id=%s "
            "AND used_at IS NULL AND expires_at > now()",
            (self.uid,)).fetchall()

    def test_sequential_mints_leave_one_live_link(self):
        reset.create_reset(self.conn, self.uid)
        reset.create_reset(self.conn, self.uid)
        self.assertEqual(len(self._live()), 1)

    def test_two_mints_at_once_leave_one_live_link(self):
        second = {}
        threads = []
        started = threading.Event()

        def run_second():
            conn = _control()
            started.set()
            try:
                second["token"] = reset.create_reset(conn, self.uid)
            except Exception as exc:                     # noqa: BLE001
                second["error"] = exc
            finally:
                conn.close()

        def at_burn():
            t = threading.Thread(target=run_second)
            threads.append(t)
            t.start()
            started.wait(5)
            # let it reach the database and, with the lock in place, block
            # there until this transaction commits
            time.sleep(0.5)

        racer = _control()
        self.addCleanup(racer.close)
        reset.create_reset(_PausingConn(racer, at_burn), self.uid)
        for t in threads:
            t.join(20)
            self.assertFalse(t.is_alive(), "the second mint never finished")
        self.assertNotIn("error", second, f"second mint failed: "
                                          f"{second.get('error')!r}")
        live = self._live()
        self.assertEqual(len(live), 1,
                         f"{len(live)} reset links are live, not 1")
