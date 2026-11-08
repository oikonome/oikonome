"""Only the worker that holds a folder import may write to it.

A worker whose heartbeat went quiet (a pool timeout, a dead connection)
has its job taken over by the sweep while it may still be running. The
first worker must learn it lost the job at its next write and stop
there: otherwise both walk the rest of the folder, each counting files
into the same progress, and the job reads past 100 % or finished early.
The heartbeat itself must outlive a failed connect, or one pool timeout
leaves a live job looking dead for the rest of its run."""

import time
import unittest
from unittest import mock

from oikonome.db import staging, tenancy
from oikonome.jobs import bulk_import_job

from .bulk_job_util import csv_for, file_rows, submit, tenant_of
from .util import _ensure_db, make_db, write_config


def _job(conn, job_id):
    return conn.execute(
        "SELECT state, done, total, heartbeat_at FROM bulk_import_jobs"
        " WHERE id = %s", (job_id,)).fetchone()


class LostWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _three_files(self, conn):
        write_config(conn)
        token = staging.put(conn, "bulk", [
            (f"bank-{t}.csv", csv_for(f"SHOP {t}")) for t in "ABC"])
        sub = submit(conn, token, [
            {"index": i, "action": "import", "account_id": "chk"}
            for i in range(3)])
        return sub["job_id"]

    def test_a_worker_whose_job_was_taken_over_writes_nothing_more(self):
        conn = make_db()
        try:
            job_id = self._three_files(conn)
            tid = tenant_of(conn)
            conn_a = tenancy.tenant_connect(tid)
            conn_b = tenancy.tenant_connect(tid)
            ran: list[tuple[str, str]] = []
            real = bulk_import_job._run_one
            took = {}

            def spy(work, t, d, name, data, **kw):
                who = "B" if took.get("b") is not None else "A"
                ran.append((who, name))
                out = real(work, t, d, name, data, **kw)
                if who == "A":
                    # mid-file, worker A's heartbeat has gone quiet and
                    # worker B takes the job over, reporting the file A
                    # was in as interrupted (what B's run does first)
                    conn.execute(
                        "UPDATE bulk_import_jobs SET heartbeat_at ="
                        " now() - interval '20 minutes' WHERE id = %s",
                        (job_id,))
                    took["b"] = bulk_import_job._claim(conn_b, job_id)
                    conn.execute(
                        """WITH f AS (
                               UPDATE bulk_import_files SET state = 'failed',
                                      data = NULL
                                WHERE job_id = %s AND state = 'running'
                            RETURNING 1)
                           UPDATE bulk_import_jobs
                              SET done = done + (SELECT count(*) FROM f)
                            WHERE id = %s""", (job_id, job_id))
                return out

            try:
                job_a = bulk_import_job._claim(conn_a, job_id)
                self.assertIsNotNone(job_a)
                with mock.patch.object(bulk_import_job, "_run_one",
                                       side_effect=spy):
                    bulk_import_job._run(conn_a, tid, job_a)
                    self.assertIsNotNone(took.get("b"))
                    self.assertEqual(ran, [("A", "bank-A.csv")],
                                     "worker A went on after losing the job")
                    bulk_import_job._run(conn_b, tid, took["b"])
            finally:
                conn_a.close()
                conn_b.close()
            # every file ran once, by one worker
            self.assertEqual(ran, [("A", "bank-A.csv"), ("B", "bank-B.csv"),
                                   ("B", "bank-C.csv")])
            job = _job(conn, job_id)
            self.assertEqual((job["state"], job["done"], job["total"]),
                             ("done", 3, 3))
            rows = file_rows(conn, job_id)
            # A's late settle did not overwrite what B recorded
            self.assertEqual(rows[0]["state"], "failed")
            self.assertEqual([rows[1]["state"], rows[2]["state"]],
                             ["done", "done"])
        finally:
            conn.close()

    def test_the_heartbeat_survives_a_failed_connect(self):
        conn = make_db()
        try:
            job_id = self._three_files(conn)
            tid = tenant_of(conn)
            claimer = tenancy.tenant_connect(tid)
            try:
                job = bulk_import_job._claim(claimer, job_id)
            finally:
                claimer.close()
            conn.execute(
                "UPDATE bulk_import_jobs SET heartbeat_at ="
                " now() - interval '20 minutes' WHERE id = %s", (job_id,))
            real = tenancy.tenant_connect
            calls = {"n": 0}

            def flaky(t):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("pool timed out")
                return real(t)

            with mock.patch.object(bulk_import_job, "HEARTBEAT_SECONDS",
                                   0.05), \
                 mock.patch.object(bulk_import_job.tenancy, "tenant_connect",
                                   side_effect=flaky):
                beat = bulk_import_job._Heartbeat(tid, job_id, job["runner"])
                beat.start()
                try:
                    deadline = time.monotonic() + 5
                    fresh = False
                    while time.monotonic() < deadline and not fresh:
                        time.sleep(0.05)
                        fresh = conn.execute(
                            "SELECT heartbeat_at > now() - interval '1 minute'"
                            " AS f FROM bulk_import_jobs WHERE id = %s",
                            (job_id,)).fetchone()["f"]
                finally:
                    beat.stopped.set()
                    beat.join(5)
            self.assertTrue(fresh, "one failed connect stopped the heartbeat")
        finally:
            conn.close()

    def test_the_heartbeat_does_not_keep_a_lost_job_alive(self):
        conn = make_db()
        try:
            job_id = self._three_files(conn)
            tid = tenant_of(conn)
            c = tenancy.tenant_connect(tid)
            try:
                job_a = bulk_import_job._claim(c, job_id)
                conn.execute(
                    "UPDATE bulk_import_jobs SET heartbeat_at ="
                    " now() - interval '20 minutes' WHERE id = %s",
                    (job_id,))
                self.assertIsNotNone(bulk_import_job._claim(c, job_id))
            finally:
                c.close()
            conn.execute(
                "UPDATE bulk_import_jobs SET heartbeat_at ="
                " now() - interval '20 minutes' WHERE id = %s", (job_id,))
            with mock.patch.object(bulk_import_job, "HEARTBEAT_SECONDS",
                                   0.05):
                beat = bulk_import_job._Heartbeat(tid, job_id,
                                                  job_a["runner"])
                beat.start()
                beat.join(5)
                alive = beat.is_alive()
                beat.stopped.set()
            self.assertFalse(alive, "the superseded heartbeat kept beating")
            stale = conn.execute(
                "SELECT heartbeat_at < now() - interval '10 minutes' AS s"
                " FROM bulk_import_jobs WHERE id = %s",
                (job_id,)).fetchone()["s"]
            self.assertTrue(stale)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
