"""A household's stored uploads are bounded by one per-household quota.

Receipts are durable: a stored image is kept until someone deletes it. With
only a per-IP hourly rate in front of the upload doors, a household could
grow the shared database without bound, and once the disk filled every
household's writes would fail. One accounting helper adds up every column
that holds a household's file bytes, and an upload past the quota is
refused before anything is written."""

import os
import unittest
from unittest import mock

from oikonome.db import staging
from oikonome.engine import receipts, storage_quota

from .util import _ensure_db, make_db

PDF = b"%PDF-1.4\n"


def _pdf(n: int) -> bytes:
    return PDF + b"0" * (n - len(PDF))


class StorageQuotaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_every_household_blob_column_is_counted(self):
        """A new tenant table holding bytes must be added to the list, or
        its uploads would grow outside the quota."""
        conn = make_db()
        try:
            rows = conn.execute(
                """SELECT c.table_name, c.column_name
                     FROM information_schema.columns c
                    WHERE c.table_schema = 'public'
                      AND c.data_type = 'bytea'
                      AND EXISTS (
                          SELECT 1 FROM information_schema.columns t
                           WHERE t.table_schema = 'public'
                             AND t.table_name = c.table_name
                             AND t.column_name = 'tenant_id')""").fetchall()
            found = {(r["table_name"], r["column_name"]) for r in rows}
            listed = {(t, c) for t, cols in storage_quota.BLOB_COLUMNS
                      for c in cols}
            self.assertEqual(found, listed)
        finally:
            conn.close()

    def test_bytes_held_adds_up_receipts_and_staged_uploads(self):
        conn = make_db()
        try:
            self.assertEqual(storage_quota.bytes_held(conn), 0)
            receipts.add(conn, None, _pdf(1000), "application/pdf")
            staging.put(conn, "bulk", [("a.csv", b"x" * 500)])
            self.assertEqual(storage_quota.bytes_held(conn), 1500)
        finally:
            conn.close()

    def test_a_receipt_past_the_household_quota_is_refused(self):
        conn = make_db()
        try:
            with mock.patch.dict(os.environ,
                                 {"OIKONOME_HOUSEHOLD_STORAGE_MB": "1"}):
                receipts.add(conn, None, _pdf(600 * 1024), "application/pdf")
                with self.assertRaises(storage_quota.StorageQuotaExceeded) \
                        as cm:
                    receipts.add(conn, None, _pdf(600 * 1024),
                                 "application/pdf")
            # the doors already map ValueError to a refusal
            self.assertIsInstance(cm.exception, ValueError)
            self.assertIn("storage", str(cm.exception))
            n = conn.execute("SELECT count(*) AS n FROM receipts"
                             ).fetchone()["n"]
            self.assertEqual(n, 1)
        finally:
            conn.close()

    def test_the_quota_reads_empty_and_zero_as_documented(self):
        with mock.patch.dict(os.environ,
                             {"OIKONOME_HOUSEHOLD_STORAGE_MB": ""}):
            self.assertEqual(storage_quota.quota_bytes(),
                             storage_quota.DEFAULT_MB * 1024 * 1024)
        with mock.patch.dict(os.environ,
                             {"OIKONOME_HOUSEHOLD_STORAGE_MB": "0"}):
            self.assertIsNone(storage_quota.quota_bytes())
        with mock.patch.dict(os.environ,
                             {"OIKONOME_HOUSEHOLD_STORAGE_MB": "0"}):
            conn = make_db()
            try:
                storage_quota.reserve(conn, 10 ** 12)    # unlimited
            finally:
                conn.close()

    def test_a_stage_past_the_household_quota_is_refused(self):
        """Staged imports share the quota with kept receipts: a household
        whose receipts fill it cannot park more bytes in the stage."""
        conn = make_db()
        try:
            with mock.patch.dict(os.environ,
                                 {"OIKONOME_HOUSEHOLD_STORAGE_MB": "1"}):
                receipts.add(conn, None, _pdf(900 * 1024), "application/pdf")
                with self.assertRaises(storage_quota.StorageQuotaExceeded):
                    staging.put(conn, "bulk", [("a.csv", b"x" * 200 * 1024)])
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM import_staging").fetchone())
        finally:
            conn.close()

    def test_a_folder_import_file_that_fails_gives_its_bytes_back(self):
        """The bytes a folder import holds go when each file has run,
        whether it imported or failed, so a failed run never parks its
        upload against the quota."""
        from .bulk_job_util import run_job, submit
        from .util import write_config
        conn = make_db()
        try:
            write_config(conn)
            token = staging.put(conn, "bulk", [
                ("bad.csv", b"\x00\x01 not a statement at all \xff" * 50)])
            sub = submit(conn, token, [{"index": 0, "action": "import",
                                        "account_id": "chk"}])
            run_job(conn, sub["job_id"])
            row = conn.execute(
                "SELECT state, data IS NULL AS gone FROM bulk_import_files"
                " WHERE job_id = %s", (sub["job_id"],)).fetchone()
            self.assertNotEqual(row["state"], "done")
            self.assertTrue(row["gone"])
            self.assertEqual(storage_quota.bytes_held(conn), 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
