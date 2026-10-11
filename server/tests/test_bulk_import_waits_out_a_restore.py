"""A folder import waits out a restore of the same household instead of
giving its files up, and a cancel reaches a file still waiting its turn.

A folder holding an export ZIP starts the archive's restore from the
submitting request, before the job's own files; the restore holds the
household's sync lock for the whole merge, which takes minutes for a real
archive. A file that gave up after its usual wait was settled as "drop
this file again" with its bytes cleared, so the plan's decision for it was
lost to a restore the same plan had started."""

import unittest
from unittest import mock

from oikonome.db import staging, tenancy
from oikonome.engine.compat import jsonb
from oikonome.jobs import bulk_import_job

from .bulk_job_util import csv_for, file_rows, submit, tenant_of
from .util import _ensure_db, make_db, write_config


class _Restore:
    """A stand-in for a restore in progress: its progress row reads
    running and it holds the household's sync lock, for as long as the
    test says."""

    def __init__(self, tid):
        self.tid = tid
        self.conn = tenancy.tenant_connect(tid)

    def begin(self):
        self.conn.execute(
            """INSERT INTO job_progress (id, state, progress)
               VALUES ('restore', 'running', %s::jsonb)
               ON CONFLICT (tenant_id, id) DO UPDATE SET state = 'running',
                   started_at = now(), updated_at = now()""",
            (jsonb({"table": "transactions", "rows": 0, "tables_done": 0}),))
        self.conn.execute("SELECT pg_advisory_lock(hashtext(%s))",
                          (f"oikonome:sync:{self.tid}",))

    def end(self):
        self.conn.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                          (f"oikonome:sync:{self.tid}",))
        self.conn.execute("UPDATE job_progress SET state = 'done',"
                          " updated_at = now() WHERE id = 'restore'")

    def close(self):
        self.conn.close()


class WaitsOutARestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.tid = tenant_of(self.conn)
        self.restore = _Restore(self.tid)

    def tearDown(self):
        self.restore.close()
        self.conn.close()

    def _submit(self, tags):
        token = staging.put(self.conn, "bulk", [
            (f"bank-{t}.csv", csv_for(f"SHOP {t}")) for t in tags])
        with mock.patch.object(bulk_import_job, "nudge"):
            return submit(self.conn, token, [
                {"index": i, "action": "import", "account_id": "chk"}
                for i in range(len(tags))])["job_id"]

    def _rows(self):
        return self.conn.execute(
            "SELECT count(*) AS n FROM transactions"
            " WHERE name LIKE 'SHOP%%'").fetchone()["n"]

    def test_a_restore_longer_than_the_give_up_window_is_waited_out(self):
        """A restore that starts mid-folder and runs past the window a
        busy lock is given: the next file keeps waiting while the restore
        is running, then imports."""
        job_id = self._submit("AB")
        real = bulk_import_job._run_file
        calls, waits = [], []

        def restore_before_the_second(*a, **kw):
            # between files: the job holds no lock here
            calls.append(1)
            if len(calls) == 2:
                self.restore.begin()
            return real(*a, **kw)

        def sleep(s):
            waits.append(s)
            if len(waits) == 4:
                self.restore.end()

        with mock.patch.object(bulk_import_job, "_run_file",
                               side_effect=restore_before_the_second), \
                mock.patch.object(bulk_import_job, "_sleep",
                                  side_effect=sleep), \
                mock.patch.object(bulk_import_job, "LOCK_GIVE_UP_SECONDS", 0):
            bulk_import_job.run_job(self.tid, job_id)
        st = bulk_import_job.status(self.conn, job_id)
        self.assertEqual(len(waits), 4, "the file gave up on the restore")
        self.assertEqual(st["state"], "done", st)
        self.assertTrue(all(f["ok"] for f in st["files"]), st)
        self.assertEqual(self._rows(), 6)

    def test_a_job_queued_behind_a_restore_is_left_for_later(self):
        """A job that would start while a restore of its household runs
        is not claimed — it stays queued, holding no worker slot and
        spending none of its time budget — and runs once the restore is
        over."""
        job_id = self._submit("A")
        self.restore.begin()
        with mock.patch.object(bulk_import_job, "_sleep"), \
                mock.patch.object(bulk_import_job, "LOCK_GIVE_UP_SECONDS", 0):
            got = bulk_import_job.run_job(self.tid, job_id)
        self.assertNotEqual(got, "ran")
        st = bulk_import_job.status(self.conn, job_id)
        self.assertEqual(st["state"], "queued", st)
        self.assertEqual(file_rows(self.conn, job_id)[0]["state"], "pending")
        self.assertIsNone(self.conn.execute(
            "SELECT started_at FROM bulk_import_jobs WHERE id = %s",
            (job_id,)).fetchone()["started_at"])
        self.restore.end()
        self.assertEqual(bulk_import_job.run_job(self.tid, job_id), "ran")
        st = bulk_import_job.status(self.conn, job_id)
        self.assertEqual(st["state"], "done", st)
        self.assertTrue(st["files"][0]["ok"], st)

    def test_a_cancel_reaches_a_file_waiting_its_turn_within_one_tick(self):
        """The wait reads the cancel on every tick: "Cancel the rest"
        stops a file still waiting behind a busy lock at the next check,
        rather than when the wait would have given up."""
        job_id = self._submit("A")
        lock = f"oikonome:sync:{self.tid}"
        blocker = tenancy.tenant_connect(self.tid)
        waits = []
        try:
            blocker.execute("SELECT pg_advisory_lock(hashtext(%s))", (lock,))

            def sleep(s):
                import time
                waits.append(s)
                if len(waits) == 1:
                    self.assertEqual(
                        bulk_import_job.cancel(self.conn, job_id)["state"],
                        "cancelling")
                time.sleep(0.02)

            with mock.patch.object(bulk_import_job, "_sleep",
                                   side_effect=sleep), \
                    mock.patch.object(bulk_import_job,
                                      "LOCK_GIVE_UP_SECONDS", 2):
                bulk_import_job.run_job(self.tid, job_id)
        finally:
            blocker.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                            (lock,))
            blocker.close()
        st = bulk_import_job.status(self.conn, job_id)
        self.assertEqual(len(waits), 1, "the wait went on after the cancel")
        self.assertEqual(st["state"], "cancelled", st)
        self.assertEqual(st["files"][0]["state"], "skipped")
        self.assertEqual(st["files"][0]["error"],
                         bulk_import_job.CANCELLED_NOTE)
        self.assertEqual(self._rows(), 0)


if __name__ == "__main__":
    unittest.main()

    def test_a_restore_past_the_jobs_own_time_budget_is_not_waited_out(self):
        """The wait on a restore is bounded by the job's time budget: a
        household that keeps restoring must not pin a worker slot for as
        long as it likes. Past the budget the file is reported not
        reached, and the job settles."""
        job_id = self._submit("AB")
        real = bulk_import_job._run_file
        calls, waits = [], []

        def restore_before_the_second(*a, **kw):
            calls.append(1)
            if len(calls) == 2:
                self.restore.begin()
            return real(*a, **kw)

        def sleep(s):
            waits.append(s)
            if len(waits) > 50:
                self.restore.end()
                raise AssertionError("the file waited on the restore past "
                                     "the job's time budget")

        with mock.patch.object(bulk_import_job, "_run_file",
                               side_effect=restore_before_the_second), \
                mock.patch.object(bulk_import_job, "_sleep",
                                  side_effect=sleep), \
                mock.patch.object(bulk_import_job, "LOCK_GIVE_UP_SECONDS", 0), \
                mock.patch.object(bulk_import_job, "JOB_MAX_SECONDS", 0):
            bulk_import_job.run_job(self.tid, job_id)
        self.restore.end()
        st = bulk_import_job.status(self.conn, job_id)
        self.assertEqual(st["state"], "done", st)
        self.assertTrue(st["files"][0]["ok"], st)
        self.assertFalse(st["files"][1]["ok"], st)
        self.assertIn("not reached", st["files"][1]["error"])
