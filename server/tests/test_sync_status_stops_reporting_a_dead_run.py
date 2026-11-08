"""A sync whose process died leaves job_progress at state='running', and
nothing settles it until the next sweep rewrites the row. The start door and
/api/connections already treat a silent row as dead after the same window;
the status door and the doctor must say the same, or the phone holds its
Sync button disabled over a run that no longer exists.

Silence alone is not death, though: nothing writes the row inside a phase, so
a run in a long categorize pass goes quiet while it still works. The sync's
advisory lock lives exactly as long as its session, so a silent row whose lock
is held is a live run, and every door must keep saying so."""
import datetime as dt
import threading
import time
import unittest
import uuid
from unittest import mock

import psycopg
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.jobs.sync_row import sync_row_live
from oikonome.web import doctor

from .util import _admin_dsn, _ensure_db


def _wait_settled(client, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if client.get("/api/jobs/sync/status").json()["state"] == "done":
            return True
        time.sleep(0.05)
    return False


class DeadSyncRunTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"deadsync-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def _row(self, state, age):
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                """INSERT INTO job_progress (id, state, progress, updated_at)
                   VALUES ('sync', %s, '{"done": 1, "total": 2}'::jsonb,
                           now() - %s::interval)
                   ON CONFLICT (tenant_id, id) DO UPDATE SET
                       state=EXCLUDED.state, progress=EXCLUDED.progress,
                       updated_at=EXCLUDED.updated_at""", (state, age))
        finally:
            conn.close()

    def _doctor_says_running(self):
        return any("sync running" in str(r.get("detail"))
                   for r in doctor.checks(self.tid))

    def test_a_silent_run_settles_as_stopped(self):
        self._row("running", "4 minutes")
        st = self.client.get("/api/jobs/sync/status").json()
        self.assertEqual(st["state"], "error")
        self.assertIn("stopped", st["progress"]["error"])
        # the rest of the snapshot still reads
        self.assertEqual(st["progress"]["total"], 2)

    def test_every_door_agrees_the_run_is_over(self):
        from oikonome.jobs import worker
        self._row("running", "4 minutes")
        self.assertFalse(self.client.get("/api/connections").json()["syncing"])
        self.assertEqual(
            self.client.get("/api/jobs/sync/status").json()["state"], "error")
        # the control the status door now leaves enabled really does start one
        with mock.patch.object(worker, "sync_tenant", return_value={}):
            self.assertTrue(
                self.client.post("/api/jobs/sync/start").json()["started"])
            self.assertTrue(_wait_settled(self.client))

    def test_a_run_inside_the_window_is_still_running(self):
        self._row("running", "1 minute")
        st = self.client.get("/api/jobs/sync/status").json()
        self.assertEqual(st["state"], "running")
        self.assertNotIn("error", st["progress"])
        self.assertTrue(self.client.get("/api/connections").json()["syncing"])
        self.assertTrue(self._doctor_says_running())

    def test_a_settled_run_is_reported_as_it_settled(self):
        self._row("done", "2 hours")
        st = self.client.get("/api/jobs/sync/status").json()
        self.assertEqual(st["state"], "done")
        self.assertNotIn("error", st["progress"])

    def test_the_doctor_does_not_report_a_dead_run_as_running(self):
        self._row("running", "4 minutes")
        self.assertFalse(self._doctor_says_running())

    def _hold_from_another_session(self, dsn=None):
        """The lock as a worker in another process or on another droplet
        holds it: a separate backend, sharing nothing with the web pool."""
        other = psycopg.connect(dsn or tenancy.APP_DSN, autocommit=True)
        self.addCleanup(other.close)
        other.execute("SELECT pg_advisory_lock(hashtext(%s))",
                      (f"oikonome:sync:{self.tid}",))

    def _syncing_per_backfill(self):
        from oikonome.web import api
        conn = tenancy.tenant_connect(self.tid)
        try:
            return api._backfill_status(conn)[0]["syncing"]
        finally:
            conn.close()

    def test_a_quiet_run_still_holding_its_lock_reads_as_running(self):
        """The real worker holds the lock here, so this also proves the
        doors and sync_tenant are talking about the same key."""
        from oikonome.jobs import worker
        working, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)

        def quiet_pass(*a, **k):
            working.set()
            finish.wait(30)
            return 0

        with mock.patch.object(worker, "_sync_pass", quiet_pass):
            run = threading.Thread(target=worker.sync_tenant, args=(self.tid,))
            run.start()
            self.assertTrue(working.wait(10))
            self._row("running", "4 minutes")

            st = self.client.get("/api/jobs/sync/status").json()
            self.assertEqual(st["state"], "running")
            self.assertNotIn("error", st["progress"])
            self.assertTrue(
                self.client.get("/api/connections").json()["syncing"])
            self.assertTrue(self._doctor_says_running())
            self.assertTrue(self._syncing_per_backfill())

            # the claim leaves the live run's row alone
            start = self.client.post("/api/jobs/sync/start").json()
            self.assertFalse(start["started"])
            st = self.client.get("/api/jobs/sync/status").json()
            self.assertEqual(st["progress"]["total"], 2)

            finish.set()
            run.join(10)
        # and when it finishes it is heard: the poll never left "running"
        self.assertEqual(
            self.client.get("/api/jobs/sync/status").json()["state"], "done")

    def test_a_start_turned_away_by_another_lock_holder_runs_once_it_lets_go(self):
        """An import, a disconnect or a restore holds the same lock as a
        sync. While one runs beside a crashed run's leftover row, the start
        door refuses, as it must: no sync could take the lock. But the
        sync the person asked for is still owed, and the holder lets go
        without pulling, so the refusal has to leave the request behind,
        the way sync_tenant does when it finds the lock held."""
        from oikonome.jobs import worker
        from oikonome.sync.base import tenant_sync_lock
        self._row("running", "4 minutes")
        conn = tenancy.tenant_connect(self.tid)
        self.addCleanup(conn.close)
        conn.execute("DELETE FROM job_progress WHERE id='sync-nudge'")
        with mock.patch.object(worker, "_enqueue_tenant_sync") as enqueue:
            with tenant_sync_lock(conn, self.tid) as held:
                self.assertTrue(held)
                start = self.client.post("/api/jobs/sync/start").json()
                self.assertFalse(start["started"])
                enqueue.assert_not_called()
            enqueue.assert_called_once_with(self.tid)

    def test_a_holder_that_lets_go_before_the_ask_lands_does_not_strand_it(self):
        """The holder answers a nudge only after it lets go. One that lets
        go between the door's refusal and its nudge never sees the ask, so
        the door looks again and, with the lock free, starts the sync itself
        rather than leaving a request nobody will answer."""
        import psycopg
        from oikonome.jobs import worker
        other = psycopg.connect(tenancy.APP_DSN, autocommit=True)
        self.addCleanup(other.close)
        other.execute("SELECT pg_advisory_lock(hashtext(%s))",
                      (f"oikonome:sync:{self.tid}",))
        self._row("running", "4 minutes")
        real = worker._nudge_sync

        def let_go_first(conn):
            other.execute("SELECT pg_advisory_unlock_all()")
            real(conn)
        ran = threading.Event()
        with mock.patch.object(worker, "_nudge_sync", let_go_first), \
                mock.patch.object(worker, "sync_tenant",
                                  side_effect=lambda *a, **k: ran.set() or []):
            start = self.client.post("/api/jobs/sync/start").json()
            self.assertTrue(start["started"], start)
            self.assertTrue(ran.wait(10), "the sync never started")
        conn = tenancy.tenant_connect(self.tid)
        self.addCleanup(conn.close)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM job_progress WHERE id='sync-nudge'").fetchone())

    def test_a_holder_that_answers_the_ask_is_not_doubled_by_the_door(self):
        """A holder that lets go after the ask lands answers it by queueing
        the sync itself; the door must then start nothing of its own."""
        from oikonome.jobs import worker
        other = psycopg.connect(tenancy.APP_DSN, autocommit=True)
        self.addCleanup(other.close)
        other.execute("SELECT pg_advisory_lock(hashtext(%s))",
                      (f"oikonome:sync:{self.tid}",))
        self._row("running", "4 minutes")
        real = worker._nudge_sync

        def ask_then_holder_answers(conn):
            real(conn)
            other.execute("SELECT pg_advisory_unlock_all()")
            worker._claim_sync_nudge(conn)        # the holder's answer
        with mock.patch.object(worker, "_nudge_sync", ask_then_holder_answers), \
                mock.patch.object(worker, "sync_tenant") as sync:
            start = self.client.post("/api/jobs/sync/start").json()
        self.assertFalse(start["started"], start)
        sync.assert_not_called()

    def test_a_lock_held_on_another_connection_counts(self):
        self._hold_from_another_session()
        self._row("running", "4 minutes")
        self.assertEqual(
            self.client.get("/api/jobs/sync/status").json()["state"],
            "running")

    def test_the_web_role_sees_a_lock_another_role_holds(self):
        from .util import TEST_DB
        self._hold_from_another_session(_admin_dsn(TEST_DB))
        self._row("running", "4 minutes")
        self.assertEqual(
            self.client.get("/api/jobs/sync/status").json()["state"],
            "running")

    def test_the_same_key_in_another_database_proves_nothing(self):
        """pg_locks spans the whole cluster; a server hosting a second
        instance holds the same numeric key for that instance's run."""
        self._hold_from_another_session(_admin_dsn("postgres"))
        self._row("running", "4 minutes")
        self.assertEqual(
            self.client.get("/api/jobs/sync/status").json()["state"], "error")

    def test_a_dead_rows_lock_gone_means_the_run_is_over(self):
        other = psycopg.connect(tenancy.APP_DSN, autocommit=True)
        other.execute("SELECT pg_advisory_lock(hashtext(%s))",
                      (f"oikonome:sync:{self.tid}",))
        self._row("running", "4 minutes")
        self.assertEqual(
            self.client.get("/api/jobs/sync/status").json()["state"],
            "running")
        # a killed worker: its session ends, and the server drops the lock
        other.close()
        end = time.monotonic() + 5
        while time.monotonic() < end:
            st = self.client.get("/api/jobs/sync/status").json()["state"]
            if st == "error":
                break
            time.sleep(0.05)
        self.assertEqual(st, "error")

    def test_the_lock_is_found_whichever_sign_its_key_hashes_to(self):
        """hashtext is a signed int4 widened to the lock's bigint, and
        pg_locks splits that into two unsigned halves."""
        old = {"state": "running",
               "updated_at": dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(minutes=4)}
        probe = psycopg.connect(tenancy.APP_DSN, autocommit=True)
        self.addCleanup(probe.close)
        found = {}
        while len(found) < 2:
            tid = str(uuid.uuid4())
            sign = probe.execute("SELECT hashtext(%s) < 0",
                                 (f"oikonome:sync:{tid}",)).fetchone()[0]
            found.setdefault(sign, tid)
        for tid in found.values():
            conn = tenancy.tenant_connect(tid)
            try:
                self.assertFalse(sync_row_live(conn, old))
                probe.execute("SELECT pg_advisory_lock(hashtext(%s))",
                              (f"oikonome:sync:{tid}",))
                self.assertTrue(sync_row_live(conn, old))
                probe.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                              (f"oikonome:sync:{tid}",))
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
