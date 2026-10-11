"""A folder plan that is refused leaves its upload staged.

The refusals that remain at submit — a plan longer than any batch, an
archive with no step-up, another folder import still running — are all
answered before the stage is spent, so the client's token still points at
the upload and the person retries without uploading again. A sync holding
the household's lock is no longer a refusal at all: the job waits its
turn for the lock, file by file."""

import unittest
from unittest import mock

from fastapi import HTTPException

from oikonome.db import staging, tenancy
from oikonome.jobs import bulk_import_job
from oikonome.web import bulk_import

from .bulk_job_util import csv_for, submit, tenant_of
from .util import make_db, write_config


class BulkRefusalTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _stage(self, *names):
        return staging.put(self.conn, "bulk", [
            (n, csv_for(n.upper().replace(".", " "))) for n in names])

    def test_a_held_sync_lock_is_not_a_refusal(self):
        tid = tenant_of(self.conn)
        token = self._stage("a.csv")
        other = tenancy.tenant_connect(tid)
        try:
            self.assertTrue(other.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (f"oikonome:sync:{tid}",)).fetchone()["ok"])
            sub = submit(self.conn, token,
                         [{"index": 0, "action": "import",
                           "account_id": "chk"}])
            self.assertEqual(sub["state"], "queued")
            other.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                          (f"oikonome:sync:{tid}",))
        finally:
            other.close()

    def test_an_archive_without_its_step_up_keeps_the_stage(self):
        token = staging.put(self.conn, "bulk", [
            ("export.zip", b"PK\x05\x06" + b"\0" * 18)])

        def refuse():
            raise HTTPException(403, {"error": "elevation_required"})

        with self.assertRaises(HTTPException):
            submit(self.conn, token, [{"index": 0, "action": "import"}],
                   step_up=refuse)
        self.assertIsNotNone(staging.peek(self.conn, "bulk", token))

    def test_an_archive_with_no_step_up_to_ask_is_refused(self):
        token = staging.put(self.conn, "bulk", [
            ("export.zip", b"PK\x05\x06" + b"\0" * 18)])
        with mock.patch.object(bulk_import_job, "nudge"), \
                self.assertRaises(PermissionError):
            bulk_import.submit(self.conn, token,
                               [{"index": 0, "action": "import"}],
                               {"role": "owner"})
        self.assertIsNotNone(staging.peek(self.conn, "bulk", token))

    def test_a_second_folder_while_one_runs_keeps_the_stage(self):
        first = self._stage("a.csv")
        second = self._stage("b.csv")
        submit(self.conn, first, [{"index": 0, "action": "import",
                                   "account_id": "chk"}])
        with self.assertRaises(bulk_import.Busy):
            submit(self.conn, second, [{"index": 0, "action": "import",
                                        "account_id": "chk"}])
        self.assertIsNotNone(staging.peek(self.conn, "bulk", second))

    def test_a_plan_longer_than_any_batch_keeps_the_stage(self):
        token = self._stage("a.csv")
        with mock.patch.object(bulk_import_job, "nudge"), \
                self.assertRaises(ValueError):
            bulk_import.submit(
                self.conn, token,
                [{"index": 0, "action": "import"}] *
                (bulk_import.MAX_FILES + 1), {"role": "owner"})
        self.assertIsNotNone(staging.peek(self.conn, "bulk", token))


if __name__ == "__main__":
    unittest.main()
