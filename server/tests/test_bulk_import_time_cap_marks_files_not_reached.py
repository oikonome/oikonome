"""A folder import that runs past its time budget stops between files and
says which files it never reached, so the person can drop just those
again; it does not hold the worker forever."""

import unittest
from unittest import mock

from oikonome.db import staging
from oikonome.jobs import bulk_import_job

from .bulk_job_util import csv_for, file_rows, submit, tenant_of
from .util import _ensure_db, make_db, write_config


class TimeCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_past_the_cap_the_rest_are_not_reached(self):
        conn = make_db()
        try:
            write_config(conn)
            token = staging.put(conn, "bulk", [
                (f"bank-{t}.csv", csv_for(f"SHOP {t}")) for t in "ABC"])
            with mock.patch.object(bulk_import_job, "nudge"):
                sub = submit(conn, token, [
                    {"index": i, "action": "import", "account_id": "chk"}
                    for i in range(3)])
            with mock.patch.object(bulk_import_job, "JOB_MAX_SECONDS", 0):
                bulk_import_job.run_job(tenant_of(conn), sub["job_id"])
            st = bulk_import_job.status(conn, sub["job_id"])
            rows = file_rows(conn, sub["job_id"])
            self.assertEqual(st["state"], "done", st)
            self.assertEqual(rows[0]["state"], "done")
            for i in (1, 2):
                self.assertEqual(rows[i]["state"], "skipped")
                self.assertIn("not reached", rows[i]["result"]["error"])
                self.assertTrue(rows[i]["gone"])
        finally:
            conn.close()

    def test_files_past_the_row_budget_are_refused_with_a_note(self):
        conn = make_db()
        try:
            write_config(conn)
            token = staging.put(conn, "bulk", [
                (f"bank-{t}.csv", csv_for(f"SHOP {t}")) for t in "ABC"])
            from oikonome.web import bulk_import
            with mock.patch.object(bulk_import_job, "nudge"):
                sub = submit(conn, token, [
                    {"index": i, "action": "import", "account_id": "chk"}
                    for i in range(3)])
            # three rows a file: a budget of five lets two files through
            # (it is checked before each file, never mid-file)
            with mock.patch.object(bulk_import, "MAX_TOTAL_ROWS", 5):
                bulk_import_job.run_job(tenant_of(conn), sub["job_id"])
            st = bulk_import_job.status(conn, sub["job_id"])
            self.assertEqual([f["ok"] for f in st["files"]],
                             [True, True, False], st)
            self.assertIn("row budget", st["files"][2]["error"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
