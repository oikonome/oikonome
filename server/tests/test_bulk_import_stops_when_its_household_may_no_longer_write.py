"""A folder import stops when its household may no longer be written to.

The job runs on the worker for up to three quarters of an hour, long
after the request whose standing checks let the plan in. A suspension, a
lockout, or a lapse into the read-only state lands anywhere in that
stretch; a job that went on creating accounts and landing rows would
outlive the very standing the request was checked against. Each file asks
the same standing gates a write request meets."""

import unittest
from unittest import mock

from oikonome import ext
from oikonome.db import staging, tenancy
from oikonome.jobs import bulk_import_job

from .bulk_job_util import csv_for, file_rows, submit, tenant_of
from .util import _ensure_db, make_db, write_config


class HouseholdStandingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.tid = tenant_of(self.conn)
        token = staging.put(self.conn, "bulk", [
            (f"bank-{t}.csv", csv_for(f"SHOP {t}")) for t in "ABC"])
        with mock.patch.object(bulk_import_job, "nudge"):
            self.job_id = submit(self.conn, token, [
                {"index": i, "action": "import", "account_id": "chk"}
                for i in range(3)])["job_id"]

    def tearDown(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenants SET status = 'active'"
                          " WHERE id = %s", (self.tid,))
        finally:
            admin.close()
        self.conn.close()

    def _run_and_after_first(self, act):
        real = bulk_import_job._run_one
        calls = []

        def first_then_act(*a, **kw):
            out = real(*a, **kw)
            calls.append(1)
            if len(calls) == 1:
                act()
            return out

        with mock.patch.object(bulk_import_job, "_run_one",
                               side_effect=first_then_act):
            bulk_import_job.run_job(self.tid, self.job_id)
        return calls

    def _assert_stopped_after_one(self, calls, reason):
        st = bulk_import_job.status(self.conn, self.job_id)
        rows = file_rows(self.conn, self.job_id)
        self.assertEqual(len(calls), 1, "a file imported after the change")
        self.assertEqual((st["state"], st["stop_reason"]),
                         ("stopped", reason), st)
        self.assertEqual(rows[0]["state"], "done")
        for i in (1, 2):
            self.assertEqual(rows[i]["state"], "skipped")
            self.assertTrue(rows[i]["gone"], "a skipped file kept its bytes")
            self.assertEqual(st["files"][i]["error"],
                             bulk_import_job.STOP_NOTES[reason])
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions"
            " WHERE name LIKE 'SHOP%%'").fetchone()["n"], 3)

    def test_a_household_suspended_mid_job_gets_no_further_files(self):
        def suspend():
            admin = tenancy.admin_connect()
            try:
                admin.execute("UPDATE tenants SET status = 'suspended'"
                              " WHERE id = %s", (self.tid,))
            finally:
                admin.close()

        calls = self._run_and_after_first(suspend)
        self._assert_stopped_after_one(calls, "locked_out")

    def test_a_household_gone_read_only_mid_job_gets_no_further_files(self):
        patcher = mock.patch.object(ext.gate, "write_blocked",
                                    return_value=True)
        self.addCleanup(patcher.stop)
        calls = self._run_and_after_first(patcher.start)
        self._assert_stopped_after_one(calls, "read_only")


if __name__ == "__main__":
    unittest.main()
