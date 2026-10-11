"""A folder import the worker lost half-way is picked up again by the
sweep — but the file it was in the middle of is not run twice.

Some importers write as they go, so a file cut off mid-run may have
partly landed; running it again from the top could double what did.
That file is reported as interrupted for the person to drop again, and
only the files never started are run."""

import unittest
from unittest import mock

from oikonome.db import staging
from oikonome.jobs import bulk_import_job

from .bulk_job_util import csv_for, file_rows, submit, tenant_of
from .util import _ensure_db, make_db, write_config


class InterruptedJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_the_sweep_finishes_the_job_and_skips_the_cut_file(self):
        conn = make_db()
        try:
            write_config(conn)
            token = staging.put(conn, "bulk", [
                (f"bank-{t}.csv", csv_for(f"SHOP {t}")) for t in "ABC"])
            with mock.patch.object(bulk_import_job, "nudge"):
                sub = submit(conn, token, [
                    {"index": i, "action": "import", "account_id": "chk"}
                    for i in range(3)])
            job_id = sub["job_id"]
            # a worker that died inside file 0: the job says running, its
            # heartbeat has gone quiet
            conn.execute(
                "UPDATE bulk_import_jobs SET state = 'running',"
                " started_at = now() - interval '20 minutes',"
                " heartbeat_at = now() - interval '20 minutes'"
                " WHERE id = %s", (job_id,))
            conn.execute("UPDATE bulk_import_files SET state = 'running'"
                         " WHERE job_id = %s AND file_id = 0", (job_id,))
            ran = []
            real = bulk_import_job._run_one

            def spy(conn_, tid, d, name, data, **kw):
                ran.append(name)
                return real(conn_, tid, d, name, data, **kw)

            with mock.patch.object(bulk_import_job, "_run_one",
                                   side_effect=spy):
                bulk_import_job.sweep(tenant_id=tenant_of(conn))
            self.assertEqual(ran, ["bank-B.csv", "bank-C.csv"])
            rows = file_rows(conn, job_id)
            self.assertEqual(rows[0]["state"], "failed")
            self.assertIn("interrupted", rows[0]["result"]["error"])
            self.assertEqual([rows[1]["state"], rows[2]["state"]],
                             ["done", "done"])
            self.assertEqual(
                bulk_import_job.status(conn, job_id)["state"], "done")
        finally:
            conn.close()

    def test_the_sweep_runs_a_job_nobody_nudged(self):
        """The queue nudge is only a latency hint: with the queue down,
        the sweep still finds the job and runs it."""
        conn = make_db()
        try:
            write_config(conn)
            token = staging.put(conn, "bulk", [("a.csv", csv_for("SHOP A"))])
            with mock.patch.object(bulk_import_job, "nudge"):
                sub = submit(conn, token, [
                    {"index": 0, "action": "import", "account_id": "chk"}])
            bulk_import_job.sweep(tenant_id=tenant_of(conn))
            self.assertEqual(
                bulk_import_job.status(conn, sub["job_id"])["state"], "done")
        finally:
            conn.close()

    def test_a_live_heartbeat_is_left_alone(self):
        conn = make_db()
        try:
            write_config(conn)
            token = staging.put(conn, "bulk", [("a.csv", csv_for("SHOP A"))])
            with mock.patch.object(bulk_import_job, "nudge"):
                sub = submit(conn, token, [
                    {"index": 0, "action": "import", "account_id": "chk"}])
            conn.execute(
                "UPDATE bulk_import_jobs SET state = 'running',"
                " started_at = now(), heartbeat_at = now() WHERE id = %s",
                (sub["job_id"],))
            with mock.patch.object(bulk_import_job, "_run_one") as one:
                bulk_import_job.sweep(tenant_id=tenant_of(conn))
                bulk_import_job.run_job(tenant_of(conn), sub["job_id"])
            one.assert_not_called()
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
