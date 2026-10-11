"""Two-connection helpers for lock-order tests.

A lock-order test runs two real writers on two connections to one
household, parks one of them at the point where it holds the locks the
other needs, and lets them collide. When the two take a pair of locks in
opposite orders Postgres finds the cycle after deadlock_timeout and kills
one side; when they agree, one simply waits for the other. The count of
deadlocks the database has seen is the honest witness either way, because
the side that is killed is sometimes a best-effort step whose own
savepoint swallows the error.
"""

from __future__ import annotations

import threading
import time

from oikonome.db import tenancy


def tenant_of(conn) -> str:
    return conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]


def backend_pid(conn) -> int:
    return conn.execute("SELECT pg_backend_pid() AS p").fetchone()["p"]


def deadlocks() -> int:
    admin = tenancy.admin_connect()
    try:
        admin.execute("SELECT pg_stat_clear_snapshot()")
        return admin.execute(
            "SELECT deadlocks FROM pg_stat_database"
            " WHERE datname = current_database()").fetchone()["deadlocks"]
    finally:
        admin.close()


def waits_on_a_lock(pid: int, locktype: str | None = None,
                    timeout: float = 10.0) -> bool:
    """True once backend `pid` is queued behind a lock (of `locktype`,
    when given) that another backend holds."""
    admin = tenancy.admin_connect()
    try:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if admin.execute(
                    "SELECT 1 FROM pg_locks WHERE pid = %s AND NOT granted"
                    + (" AND locktype = %s" if locktype else ""),
                    (pid, locktype) if locktype else (pid,)).fetchone():
                return True
            time.sleep(0.02)
        return False
    finally:
        admin.close()


def deadlocks_since(before: int, settle: float = 5.0) -> int:
    """Deadlocks counted after `before`. A backend flushes its statistics
    about a second after it goes idle, so the count is polled for a while
    before it is believed to be unchanged."""
    end = time.monotonic() + settle
    while True:
        after = deadlocks()
        if after != before or time.monotonic() >= end:
            return after - before
        time.sleep(0.1)


class Side(threading.Thread):
    """Run `fn(conn)` on its own connection to the household, recording
    its result or the exception it raised."""

    def __init__(self, tid: str, fn):
        super().__init__(daemon=True)
        self.conn = tenancy.tenant_connect(tid)
        # a safety net only: a cycle is broken by the deadlock detector in
        # about a second, and nothing in these tests should wait longer
        self.conn.execute("SET lock_timeout = '15s'")
        self.pid = backend_pid(self.conn)
        self.fn = fn
        self.result = None
        self.error: BaseException | None = None

    def run(self):
        try:
            self.result = self.fn(self.conn)
        except BaseException as e:                       # noqa: BLE001
            self.error = e

    def finish(self, timeout: float = 30.0):
        self.join(timeout)
        try:
            # session-level, and the connection is pooled: reset it so the
            # fuse does not ride back into the pool to the next test
            self.conn.execute("RESET lock_timeout")
            self.conn.close()
        except Exception:                                # noqa: BLE001
            pass
        return not self.is_alive()


class Gate:
    """A pause point patched into one writer: it says when it got there
    and waits until it is told to go on."""

    def __init__(self):
        self.reached = threading.Event()
        self.go = threading.Event()

    def _pause(self):
        self.reached.set()
        if not self.go.wait(30):
            raise RuntimeError("the test never released the gate")

    def wrap(self, real):
        """Pause right after `real` returns."""
        def paused(*a, **kw):
            out = real(*a, **kw)
            self._pause()
            return out
        return paused

    def before(self, real, *, call: int = 1):
        """Pause just before the `call`-th call of `real`."""
        seen = [0]

        def paused(*a, **kw):
            seen[0] += 1
            if seen[0] == call:
                self._pause()
            return real(*a, **kw)
        return paused
