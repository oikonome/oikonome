"""A session-level advisory lock TAKE that raises must never hand its
pooled connection back to the pool.

The single-flight doors (sync, categorize, snapshot, bill detection, reap,
nightly, email, stale-collector alerts, webhook item sync, Enrich, restore,
the one-bank pull, the Amazon chain) take `pg_try_advisory_lock` /
`pg_advisory_lock` on a POOLED connection, outside the try whose finally
releases it. A statement timeout or a cancel can land after the server
granted the lock, and a connection inside an aborted transaction refuses
the statement outright; either way nothing unlocks afterwards, and an
ordinary close() would give the next borrower a session that may hold the
household's lock — every later try-lock for it answers "already running"
until the backend dies. `tenancy.take_lock` discards the handle and
re-raises; every pooled take goes through it."""
import contextlib
import pathlib
import re
import time
import unittest
from unittest import mock

import psycopg

from oikonome.db import tenancy

from .util import make_db

_TAKE = re.compile(r"pg_(try_)?advisory_lock(_shared)?\(")


class _FakeConn:
    def __init__(self, exc):
        self.exc = exc
        self.discarded = False

    def execute(self, sql, params=None):
        raise self.exc

    def discard(self):
        self.discarded = True


def _lossy(conn):
    """Every session-level take on `conn` is granted on the server and then
    its reply is lost (a statement timeout landing after the grant)."""
    real = conn._conn.execute

    def execute(sql, *a, **kw):
        if isinstance(sql, str) and _TAKE.search(sql):
            real(sql, *a, **kw)
            raise psycopg.errors.QueryCanceled("statement timeout")
        return real(sql, *a, **kw)

    conn.execute = execute
    return conn


class TakeLockHelper(unittest.TestCase):

    def test_a_raised_take_discards_and_propagates(self):
        c = _FakeConn(psycopg.errors.QueryCanceled("cancel"))
        with self.assertRaises(psycopg.errors.QueryCanceled):
            tenancy.take_lock(c, "SELECT pg_try_advisory_lock(1) AS ok")
        self.assertTrue(c.discarded)

    def test_a_take_that_succeeds_returns_its_row_and_keeps_the_conn(self):
        db = make_db()
        tid = db.execute("SELECT current_setting('app.tenant_id') AS t"
                         ).fetchone()["t"]
        db.close()
        conn = tenancy.tenant_connect(tid)
        try:
            row = tenancy.take_lock(
                conn, "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                ("synthetic-key",))
            self.assertIs(row["ok"], True)
            self.assertFalse(getattr(conn, "_discarded", False))
            tenancy.release_lock(conn, "synthetic-key")
        finally:
            conn.close()

    def test_a_take_inside_a_failed_transaction_discards(self):
        db = make_db()
        tid = db.execute("SELECT current_setting('app.tenant_id') AS t"
                         ).fetchone()["t"]
        db.close()
        conn = tenancy.tenant_connect(tid)
        raw = conn._conn
        raw.execute("BEGIN")
        with self.assertRaises(psycopg.errors.DivisionByZero):
            raw.execute("SELECT 1/0")
        try:
            with self.assertRaises(psycopg.errors.InFailedSqlTransaction):
                tenancy.take_lock(
                    conn, "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                    ("synthetic-key",))
        finally:
            if not getattr(conn, "_discarded", False):
                raw.execute("ROLLBACK")
        self.assertTrue(getattr(conn, "_discarded", False),
                        "a connection in an aborted transaction went back "
                        "to the pool")
        conn.close()


class EveryPooledDoorDiscardsOnARaisedTake(unittest.TestCase):

    def setUp(self):
        db = make_db()
        self.tid = db.execute("SELECT current_setting('app.tenant_id') AS t"
                              ).fetchone()["t"]
        db.close()

    def _conn(self):
        conn = _lossy(tenancy.tenant_connect(self.tid))
        self.pid = conn._conn.info.backend_pid
        return conn

    def _assert_dropped(self, conn):
        self.assertTrue(getattr(conn, "_discarded", False),
                        "the door pooled a connection whose take raised")
        # and the lock the server granted died with the session
        probe = tenancy.tenant_connect(self.tid)
        try:
            for _ in range(40):
                n = probe.execute(
                    "SELECT count(*) AS n FROM pg_locks WHERE "
                    "locktype='advisory' AND pid=%s",
                    (self.pid,)).fetchone()["n"]
                if not n:
                    break
                time.sleep(0.05)
            self.assertEqual(n, 0)
        finally:
            probe.close()

    def _via_door(self, fn, *a, patches=(), **kw):
        conn = self._conn()
        with contextlib.ExitStack() as st:
            st.enter_context(mock.patch.object(
                tenancy, "tenant_connect", lambda *_a, **_k: conn))
            for p in patches:
                st.enter_context(p)
            try:
                fn(*a, **kw)
            except Exception:                            # noqa: BLE001
                pass
        self._assert_dropped(conn)

    def test_tenant_sync_lock(self):
        from oikonome.sync import base
        conn = self._conn()
        with self.assertRaises(psycopg.errors.QueryCanceled):
            with base.tenant_sync_lock(conn, self.tid):
                self.fail("body ran without the lock")
        self._assert_dropped(conn)

    def test_categorize_lock(self):
        from oikonome.sync import base
        conn = self._conn()
        with self.assertRaises(psycopg.errors.QueryCanceled):
            with base.categorize_lock(conn, self.tid):
                self.fail("body ran without the lock")
        self._assert_dropped(conn)

    def test_bill_detection_try_and_wait(self):
        from oikonome.engine import bills
        for wait in (False, True):
            conn = self._conn()
            with self.assertRaises(psycopg.errors.QueryCanceled):
                bills.run_single_flight(conn, wait=wait)
            self._assert_dropped(conn)

    def test_amazon_chain_lock(self):
        from oikonome.engine import llm_categorize
        conn = self._conn()
        with self.assertRaises(psycopg.errors.QueryCanceled):
            with llm_categorize.amazon_chain_lock(conn):
                self.fail("body ran without the lock")
        self._assert_dropped(conn)

    def test_enrich(self):
        from oikonome.sync import plaid
        conn = self._conn()
        with mock.patch.object(plaid, "plaid_product_allowed",
                               lambda *_a: True):
            with self.assertRaises(psycopg.errors.QueryCanceled):
                plaid.enrich_rows(conn)
        self._assert_dropped(conn)

    def test_snapshot(self):
        from oikonome.web import reporting_api
        self._via_door(reporting_api.snapshot_tenant, self.tid)

    def test_reap(self):
        from oikonome.jobs import reaper
        self._via_door(reaper.reap_tenant, self.tid)

    def test_worker_sweeps(self):
        from oikonome.jobs import worker
        for fn, a in ((worker.sync_tenant, (self.tid,)),
                      (worker.nightly_tenant, (self.tid,)),
                      (worker.script_alert_tenant, (self.tid,)),
                      (worker._email_if_due, (self.tid,)),
                      (worker._sync_item_body, (self.tid, "it1"))):
            with self.subTest(fn=fn.__name__):
                self._via_door(fn, *a)

    def test_restore(self):
        from oikonome.sync import restore_job
        self._via_door(restore_job._run, self.tid, b"not a zip",
                       patches=(mock.patch.object(
                           restore_job, "_write_progress",
                           lambda *_a, **_k: None),))

    def test_one_bank_pull(self):
        from oikonome.web import pages
        user = {"tenant_id": self.tid, "user_id": "u", "role": "owner"}
        self._via_door(pages._account_sync, user, "it1", patches=(
            mock.patch.object(pages.demoguard, "deny", lambda *_a: None),
            mock.patch.object(pages, "_tenant_sync_allowed",
                              lambda *_a: True)))


class TenantSyncLockHoldsAndReleases(unittest.TestCase):

    def test_held_in_the_body_and_gone_after_even_when_the_body_raises(self):
        from oikonome.sync import base
        db = make_db()
        tid = db.execute("SELECT current_setting('app.tenant_id') AS t"
                         ).fetchone()["t"]
        db.close()
        key = f"oikonome:sync:{tid}"
        held = ("SELECT count(*) AS n FROM pg_locks l WHERE "
                "l.locktype='advisory' AND l.granted AND l.objid = "
                "(hashtext(%s)::bigint & 4294967295)::oid")
        conn = tenancy.tenant_connect(tid)
        other = tenancy.tenant_connect(tid)
        try:
            with base.tenant_sync_lock(conn, tid) as got:
                self.assertTrue(got)
                self.assertEqual(other.execute(held, (key,)).fetchone()["n"],
                                 1)
            self.assertEqual(other.execute(held, (key,)).fetchone()["n"], 0)
            with self.assertRaises(ValueError):
                with base.tenant_sync_lock(conn, tid) as got:
                    self.assertTrue(got)
                    raise ValueError("body failed")
            self.assertEqual(other.execute(held, (key,)).fetchone()["n"], 0)
        finally:
            conn.close()
            other.close()


class NoPooledTakeBypassesTheHelper(unittest.TestCase):
    """Source guard: a session-level take in the package goes through
    tenancy.take_lock, unless it runs on an unpooled admin connection (a
    failed take dies with that connection) or is erasure's probe, which
    discards on its own."""

    ROOT = pathlib.Path(__file__).resolve().parents[1] / "oikonome"
    # files whose session-level takes all run on unpooled admin/owner
    # connections, or that handle the discard themselves
    EXEMPT = {
        "db/tenancy.py": "the helper and its docstrings",
        "db/migrate.py": "unpooled admin connection",
        "web/spamdefense.py": "unpooled admin connection (_admin_or)",
        "erasure.py": "discards in _probe_push_lock / _drop",
    }

    def test_every_pooled_take_uses_take_lock(self):
        bad = []
        for path in sorted(self.ROOT.rglob("*.py")):
            rel = path.relative_to(self.ROOT).as_posix()
            if rel in self.EXEMPT:
                continue
            src = path.read_text()
            for m in _TAKE.finditer(src):
                before = src[max(0, m.start() - 160):m.start()]
                if "take_lock(" in before or "admin.execute(" in before[-90:]:
                    continue
                line = src.count("\n", 0, m.start()) + 1
                bad.append(f"{rel}:{line}")
        self.assertEqual(bad, [], "session-level advisory takes on a pooled "
                         "connection outside tenancy.take_lock")


if __name__ == "__main__":
    unittest.main()
