"""A person's edit queued behind a restore answers "busy, try again" within
a few seconds, and writes nothing.

A restore holds the household's storage, pairing and identity locks for
its whole transaction — minutes on a large archive. An edit takes the
pairing lock first, so it queues behind the restore; unbounded, it would
hold one of the instance's few pooled connections the whole time, and a
handful of such edits would take the pool from every other household. A
request door therefore waits only so long (reanchor.bounded_lock_wait)
and gives up with LockNotAvailable, which the web layer answers 409.
Workers, syncs and the restore itself keep the unbounded wait.
"""

from __future__ import annotations

import os
import threading
import time
import unittest
import uuid

import psycopg
from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .lock_order_util import backend_pid, waits_on_a_lock
from .util import _ensure_db, add_txn, seed_accounts

PW = "correct-horse-battery"
# a door that has not given up by now is waiting out the restore
DOOR_BOUND_S = 9.0
# the stand-in restore lets go after this, so a door that waits it out
# returns (and fails the test) instead of hanging the run
RESTORE_HOLDS_S = 15.0


class _RestoreHoldingItsLocks:
    """A connection standing in for a restore: its one transaction holds
    the locks a restore takes before its first row, until released."""

    def __init__(self, tid: str, hold_for: float | None = None):
        self.tid = tid
        self.hold_for = hold_for
        self.holding = threading.Event()
        self.release = threading.Event()
        self.error: BaseException | None = None
        self.pid = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        from oikonome.web.bulk_import import hold_restore_write_locks
        conn = tenancy.tenant_connect(self.tid)
        try:
            with conn.transaction():
                hold_restore_write_locks(conn)
                self.pid = backend_pid(conn)
                self.holding.set()
                self.release.wait(self.hold_for)
        except BaseException as e:                       # noqa: BLE001
            self.error = e
        finally:
            conn.close()

    def __enter__(self):
        self.thread.start()
        assert self.holding.wait(10), "the restore never took its locks"
        return self

    def __exit__(self, *exc):
        self.release.set()
        self.thread.join(30)


class _Household(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.c = TestClient(self.app)
        email = f"busy-{uuid.uuid4().hex[:10]}@example.dev"
        r = self.c.post("/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.c.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            self.row = add_txn(conn, "2026-03-02", 12.0, "CORNER BAKERY")
        finally:
            conn.close()


class AnEditBehindARestoreAnswersBusy(_Household):
    def test_a_note_behind_a_restore_is_refused_busy_and_not_written(self):
        with _RestoreHoldingItsLocks(self.tid, hold_for=RESTORE_HOLDS_S):
            t0 = time.monotonic()
            r = self.c.post(f"/api/transactions/{self.row}/note",
                            json={"note": "written behind the restore"})
            took = time.monotonic() - t0
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json().get("error"), "busy", r.text)
        self.assertLess(took, DOOR_BOUND_S,
                        f"the door waited {took:.1f}s on the restore")
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM transaction_notes WHERE txn_id=%s",
                (self.row,)).fetchone(), "the refused note was written")
        finally:
            conn.close()

    def test_the_same_note_goes_through_once_the_restore_is_done(self):
        with _RestoreHoldingItsLocks(self.tid):
            pass
        r = self.c.post(f"/api/transactions/{self.row}/note",
                        json={"note": "after the restore"})
        self.assertEqual(r.status_code, 200, r.text)


class TheBoundCoversRequestsOnly(_Household):
    def test_the_request_helper_gives_up_and_a_worker_keeps_waiting(self):
        from oikonome.sync import reanchor
        with _RestoreHoldingItsLocks(self.tid):
            conn = tenancy.tenant_connect(self.tid)
            try:
                t0 = time.monotonic()
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    with conn.transaction():
                        with reanchor.bounded_lock_wait(conn, ms=300):
                            reanchor.hold_pairing_lock(conn)
                self.assertLess(time.monotonic() - t0, 3.0)
            finally:
                conn.close()
            # a worker's unbounded wait is still queued well past the bound
            worker = tenancy.tenant_connect(self.tid)
            wpid = backend_pid(worker)
            out: dict = {}

            def run():
                try:
                    with worker.transaction():
                        reanchor.hold_pairing_lock(worker)
                        out["got"] = True
                except BaseException as e:               # noqa: BLE001
                    out["error"] = e
            th = threading.Thread(target=run, daemon=True)
            th.start()
            self.assertTrue(waits_on_a_lock(wpid, "advisory"))
            time.sleep(1.0)
            self.assertTrue(th.is_alive(), out)
        th.join(30)
        worker.close()
        self.assertEqual(out, {"got": True})

    def test_the_wait_before_the_bound_comes_back_after_it(self):
        from oikonome.sync import reanchor
        conn = tenancy.tenant_connect(self.tid)
        try:
            with conn.transaction():
                show = "SELECT current_setting('lock_timeout') AS t"
                before = conn.execute(show).fetchone()["t"]
                reanchor.hold_pairing_lock_for_request(conn)
                self.assertEqual(conn.execute(show).fetchone()["t"], before)
                # a shorter wait already in force is not lengthened
                conn.execute("SET LOCAL lock_timeout = '200ms'")
                with reanchor.bounded_lock_wait(conn):
                    self.assertEqual(conn.execute(show).fetchone()["t"],
                                     "200ms")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
