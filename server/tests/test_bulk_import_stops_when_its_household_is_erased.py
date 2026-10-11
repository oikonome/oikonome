"""A folder import whose household is erased under it stops quietly.

An erasure deletes every row the household has, the job's own included.
The runner reads its job row before each file; finding it gone it stops,
without raising and without writing anything back under a household that
no longer exists."""

import unittest
from unittest import mock

from oikonome.db import staging
from oikonome.jobs import bulk_import_job

from .bulk_job_util import csv_for, submit, tenant_of
from .util import _ensure_db, make_db, write_config


class ErasedUnderTheJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_a_deleted_job_row_stops_the_runner(self):
        conn = make_db()
        try:
            write_config(conn)
            token = staging.put(conn, "bulk", [
                (f"bank-{t}.csv", csv_for(f"SHOP {t}")) for t in "AB"])
            with mock.patch.object(bulk_import_job, "nudge"):
                sub = submit(conn, token, [
                    {"index": i, "action": "import", "account_id": "chk"}
                    for i in range(2)])
            real = bulk_import_job._run_one
            calls = []

            def first_then_erase(*a, **kw):
                out = real(*a, **kw)
                calls.append(1)
                conn.execute("DELETE FROM bulk_import_jobs WHERE id = %s",
                             (sub["job_id"],))
                return out

            with mock.patch.object(bulk_import_job, "_run_one",
                                   side_effect=first_then_erase):
                bulk_import_job.run_job(tenant_of(conn), sub["job_id"])
            self.assertEqual(len(calls), 1)
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM bulk_import_files").fetchone())
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
