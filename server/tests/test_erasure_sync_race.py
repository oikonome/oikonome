"""Tenant erasure must not race an in-flight sync.

The domain tables (items/accounts/transactions/…) carry tenant_id with no
foreign key to tenants — only RLS and the explicit delete sweep scope them —
so a sync that started before a wipe happily upserts rows back AFTER the
erasure commits: permanently resurrected data for a tenant that no longer
exists. Every erasure door therefore blocks on the same per-tenant advisory
lock every sync door try-locks (`oikonome:sync:<tenant>`): an in-flight sync
finishes and releases before the first row is deleted, and no new sync can
start mid-wipe.
"""

import inspect
import os
import threading
import time
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome import erasure
from oikonome.db import tenancy
from oikonome.jobs import worker
from oikonome.web import adminconsole

from .util import _ensure_db, make_db

_RELEASED = {"plaid_items": 0, "released": "none", "mx_user": "none"}


def _tid(conn) -> str:
    return conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]


class _SyncLockHolder:
    """A second DB session pretending to be a sync in flight."""

    def __init__(self, tid: str):
        self.tid = tid
        self.conn = tenancy.admin_connect()

    def acquire(self):
        got = self.conn.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (f"oikonome:sync:{self.tid}",)).fetchone()["ok"]
        assert got, "test could not take the sync lock"

    def release(self):
        self.conn.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                          (f"oikonome:sync:{self.tid}",))

    def close(self):
        self.conn.close()


def _wait_until(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


class PurgeWaitsForSyncTests(unittest.TestCase):
    """The console/worker wipe path blocks behind a held sync lock."""

    def test_purge_blocks_until_the_sync_lock_is_released(self):
        conn = make_db()
        tid = _tid(conn)
        conn.close()
        holder = _SyncLockHolder(tid)
        holder.acquire()
        done = threading.Event()

        def _purge():
            admin = tenancy.admin_connect()
            try:
                with mock.patch.object(erasure, "release_external",
                                       return_value=dict(_RELEASED)):
                    adminconsole._purge_tenant(admin, tid, "owner@x", ip="t")
            finally:
                admin.close()
                done.set()

        t = threading.Thread(target=_purge, daemon=True)
        try:
            t.start()
            # the purge must be WAITING, not finished, while the lock is held
            self.assertFalse(done.wait(1.0),
                             "purge completed while a sync held the lock")
            probe = tenancy.admin_connect()
            try:
                self.assertIsNotNone(probe.execute(
                    "SELECT 1 FROM tenants WHERE id=%s", (tid,)).fetchone(),
                    "rows were deleted while a sync was in flight")
            finally:
                probe.close()
            holder.release()
            self.assertTrue(done.wait(15), "purge never completed")
        finally:
            holder.close()
            t.join(timeout=15)
        probe = tenancy.admin_connect()
        try:
            self.assertIsNone(probe.execute(
                "SELECT 1 FROM tenants WHERE id=%s", (tid,)).fetchone())
            # and the purge released its own lock — the next borrower of a
            # session must not inherit it
            free = probe.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (f"oikonome:sync:{tid}",)).fetchone()["ok"]
            self.assertTrue(free, "the purge left the sync lock held")
            probe.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                          (f"oikonome:sync:{tid}",))
        finally:
            probe.close()

    def test_scheduled_grace_purge_blocks_until_the_lock_is_released(self):
        conn = make_db()
        tid = _tid(conn)
        conn.close()
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE tenants SET status='pending_delete', "
                "delete_after = now() - interval '1 hour' WHERE id=%s", (tid,))
        finally:
            admin.close()
        holder = _SyncLockHolder(tid)
        holder.acquire()
        purged: list = []
        done = threading.Event()

        def _run():
            try:
                with mock.patch.object(erasure, "release_external",
                                       return_value=dict(_RELEASED)):
                    purged.extend(worker.purge_scheduled_deletions())
            finally:
                done.set()

        t = threading.Thread(target=_run, daemon=True)
        try:
            t.start()
            self.assertFalse(done.wait(1.0),
                             "grace purge ran while a sync held the lock")
            holder.release()
            self.assertTrue(done.wait(20), "grace purge never completed")
        finally:
            holder.close()
            t.join(timeout=20)
        self.assertIn(tid, purged)
        probe = tenancy.admin_connect()
        try:
            self.assertIsNone(probe.execute(
                "SELECT 1 FROM tenants WHERE id=%s", (tid,)).fetchone())
        finally:
            probe.close()

    def test_grace_purge_holds_the_lock_for_the_whole_transaction(self):
        """_purge_tenant's own unlock lands BEFORE the worker's enclosing
        commit, so the worker must also take an xact-scoped lock that only
        commit releases — otherwise a sync starting in the unlock→commit
        gap reads the not-yet-deleted rows and resurrects them."""
        src = inspect.getsource(worker.purge_scheduled_deletions)
        self.assertIn("pg_advisory_xact_lock", src)
        self.assertIn("oikonome:sync:", src)


class AccountDeleteWaitsForSyncTests(unittest.TestCase):
    """The self-serve CCPA door blocks behind a held sync lock, and takes
    the tenant off 'active' before waiting so the hourly sweep can never
    pick it up again."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def test_delete_blocks_until_the_sync_lock_is_released(self):
        client = TestClient(self.appmod.app)
        pw = "correct-horse-battery"
        email = f"race-{uuid.uuid4().hex[:10]}@example.dev"
        r = client.post("/api/signup", data={"email": email, "password": pw})
        self.assertEqual(r.status_code, 200, r.text)
        admin = tenancy.admin_connect()
        try:
            tid = str(admin.execute(
                "SELECT tenant_id FROM users WHERE email=%s",
                (email,)).fetchone()["tenant_id"])
        finally:
            admin.close()
        holder = _SyncLockHolder(tid)
        holder.acquire()
        done = threading.Event()
        status: list = []

        def _delete():
            try:
                with mock.patch.object(erasure, "release_external",
                                       return_value=dict(_RELEASED)):
                    status.append(client.post("/api/account/delete",
                                              data={"password": pw}))
            finally:
                done.set()

        t = threading.Thread(target=_delete, daemon=True)
        try:
            t.start()
            probe = tenancy.admin_connect()
            try:
                # while the delete waits on the lock, the tenant must
                # already be off 'active' (committed — autocommit conn), so
                # the worker's sweep query excludes it immediately
                self.assertTrue(_wait_until(lambda: probe.execute(
                    "SELECT status FROM tenants WHERE id=%s",
                    (tid,)).fetchone()["status"] == "pending_delete"),
                    "tenant was not taken off 'active' before the wait")
                self.assertFalse(done.wait(1.0),
                                 "delete completed while a sync held the lock")
                self.assertIsNotNone(probe.execute(
                    "SELECT 1 FROM tenants WHERE id=%s", (tid,)).fetchone(),
                    "rows were deleted while a sync was in flight")
            finally:
                probe.close()
            holder.release()
            self.assertTrue(done.wait(20), "account delete never completed")
        finally:
            holder.close()
            t.join(timeout=20)
        self.assertEqual(status[0].status_code, 200, status[0].text)
        probe = tenancy.admin_connect()
        try:
            self.assertIsNone(probe.execute(
                "SELECT 1 FROM tenants WHERE id=%s", (tid,)).fetchone())
        finally:
            probe.close()


class LockKeyContractTests(unittest.TestCase):
    """Every erasure door and every sync door must hash the SAME key —
    a drifted prefix silently voids the whole coordination."""

    def test_all_doors_share_the_sync_lock_key(self):
        import oikonome.web.app as appmod
        from oikonome.web import pages
        sources = {
            "worker.sync_tenant": inspect.getsource(worker.sync_tenant),
            "worker.purge_scheduled_deletions":
                inspect.getsource(worker.purge_scheduled_deletions),
            "app.account_delete": inspect.getsource(appmod.account_delete),
            "adminconsole._purge_tenant":
                inspect.getsource(adminconsole._purge_tenant),
            "pages sync door": inspect.getsource(pages.account_sync),
        }
        for name, src in sources.items():
            self.assertIn("oikonome:sync:", src,
                          f"{name} does not use the shared sync lock key")


if __name__ == "__main__":
    unittest.main()
