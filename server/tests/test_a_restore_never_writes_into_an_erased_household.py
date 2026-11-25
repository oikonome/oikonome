"""A restore into a household whose erasure has begun writes nothing.

The domain tables have no foreign key to tenants, so a row written under
a household the erasure has flipped to pending_delete (or already
deleted) is never swept again. Every push door asks the erasure inside its
write transaction before its first row; the restore, the largest write of
all, must ask too, and so must the job's own progress row.
"""
import csv
import io
import json
import unittest
import zipfile

from oikonome.db import tenancy
from oikonome.sync import restore, restore_job

from .util import make_db, write_config


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps({})})
        z.writestr("tenant_settings.csv", s.getvalue())
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["id", "account_id", "date",
                                          "amount", "name"])
        w.writeheader()
        w.writerow({"id": "er-1", "account_id": "chk",
                    "date": "2026-04-01", "amount": "12",
                    "name": "Corner Cafe"})
        z.writestr("transactions.csv", s.getvalue())
    return buf.getvalue()


class ARestoreAsksTheErasure(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id', true) AS t"
        ).fetchone()["t"]
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenants SET status='pending_delete'"
                          " WHERE id=%s", (self.tid,))
        finally:
            admin.close()

    def _landed(self) -> int:
        return self.conn.execute(
            "SELECT count(*) AS n FROM transactions WHERE id='er-1'"
        ).fetchone()["n"]

    def _job_rows(self) -> int:
        return self.conn.execute(
            "SELECT count(*) AS n FROM job_progress WHERE id=%s",
            (restore_job.JOB_ID,)).fetchone()["n"]

    def test_the_restore_itself_refuses_and_writes_nothing(self):
        with self.assertRaises(restore.HouseholdBeingDeleted):
            restore_job.restore_now(self.conn, _zip())
        self.assertEqual(self._landed(), 0)

    def test_the_job_is_not_started_and_leaves_no_progress_row(self):
        out = restore_job.start(self.tid, _zip())
        self.assertIn("being deleted", out.get("error", ""))
        self.assertTrue(out.get("erased"))
        self.assertEqual(self._job_rows(), 0)

    def test_the_claim_waits_for_an_erasure_holding_the_writers(self):
        """The claim asks inside its own transaction, under the push writer
        lock: an erasure holding the writers refuses it even before the
        status reads pending_delete."""
        from oikonome import erasure
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenants SET status='active' WHERE id=%s",
                          (self.tid,))
            erasure.hold_writers(admin, self.tid)
            try:
                out = restore_job.start(self.tid, _zip())
            finally:
                erasure.release_writers(admin, self.tid)
        finally:
            admin.close()
        self.assertTrue(out.get("erased"))
        self.assertEqual(self._job_rows(), 0)

    def test_a_folder_import_does_not_ask_for_the_file_again(self):
        from oikonome.web import bulk_import
        rows = bulk_import._start_restores(
            self.tid, [0], {0: {"filename": "export.zip"}}, {0: _zip()})
        self.assertIn("being deleted", rows[0]["error"])
        self.assertNotIn("upload", rows[0]["error"])

    def test_a_running_job_writes_neither_rows_nor_progress(self):
        self.conn.execute(
            "INSERT INTO job_progress (id, state, progress)"
            " VALUES (%s, 'running', '{}'::jsonb)", (restore_job.JOB_ID,))
        restore_job._run(self.tid, _zip())
        self.assertEqual(self._landed(), 0)
        self.assertEqual(self.conn.execute(
            "SELECT state FROM job_progress WHERE id=%s",
            (restore_job.JOB_ID,)).fetchone()["state"], "running")


class ARestoreNamesItsHousehold(unittest.TestCase):

    def test_a_connection_without_a_household_is_refused(self):
        conn = tenancy.admin_connect()
        try:
            with self.assertRaises(RuntimeError):
                restore_job.restore_now(conn, _zip())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
