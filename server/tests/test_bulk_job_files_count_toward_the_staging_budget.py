"""Bytes a folder import is still holding count against the household's
staging budget. Moving an upload from the stage into a job must not free
its room, or a household could park one budget's worth in jobs, stage
another, and repeat."""

import unittest
from unittest import mock

from oikonome.db import staging
from oikonome.jobs import bulk_import_job

from .bulk_job_util import submit
from .util import _ensure_db, make_db, write_config


class JobBytesBudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_job_bytes_and_a_new_stage_share_one_budget(self):
        conn = make_db()
        try:
            write_config(conn)
            token = staging.put(conn, "bulk", [("a.csv", b"x" * 600)])
            with mock.patch.object(bulk_import_job, "nudge"):
                submit(conn, token, [{"index": 0, "action": "import",
                                      "account_id": "chk"}])
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM import_staging").fetchone())
            with mock.patch.object(staging, "TENANT_BUDGET", 1000), \
                    self.assertRaises(ValueError):
                staging.put(conn, "bulk", [("b.csv", b"y" * 600)])
        finally:
            conn.close()

    def test_the_hourly_sweep_clears_bytes_a_job_no_longer_needs(self):
        conn = make_db()
        try:
            write_config(conn)
            token = staging.put(conn, "bulk", [("a.csv", b"x" * 10)])
            with mock.patch.object(bulk_import_job, "nudge"):
                sub = submit(conn, token, [{"index": 0, "action": "import",
                                            "account_id": "chk"}])
            # a job nobody ever ran, long past any run it could still have
            conn.execute("UPDATE bulk_import_jobs SET created_at = now()"
                         " - interval '3 hours' WHERE id = %s",
                         (sub["job_id"],))
            staging.sweep_all()
            row = conn.execute(
                "SELECT f.data IS NULL AS gone, f.state, j.state AS job"
                "  FROM bulk_import_files f JOIN bulk_import_jobs j"
                "    ON j.id = f.job_id WHERE f.job_id = %s",
                (sub["job_id"],)).fetchone()
            self.assertTrue(row["gone"])
            self.assertEqual(row["state"], "skipped")
            # a deliberate stop with its reason, which the clients turn
            # into "it waited too long for the server" — not the crash
            # wording an 'error' gets
            self.assertEqual(row["job"], "stopped")
            st = bulk_import_job.status(conn, sub["job_id"])
            self.assertEqual((st["state"], st["stop_reason"]),
                             ("stopped", "expired"))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
