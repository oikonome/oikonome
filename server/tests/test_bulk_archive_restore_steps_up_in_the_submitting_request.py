"""An export ZIP in a folder plan is restored by the request that submits
the plan, after that request's step-up, and never by the background job.

The job runs long after the person proved who they were, with no request
to ask again; a restore inside it would be the one privileged act no
step-up covered. So the archive is decided, stepped up for and started at
submit, and its bytes never become a job file."""

import time
import unittest
from unittest import mock

from oikonome.db import staging
from oikonome.jobs import bulk_import_job

from .bulk_job_util import PW, SignedInClient, csv_for, file_rows
from .test_restore_doors_want_elevation import OURS, THEIRS, _archive
from .util import _ensure_db


class ArchiveStepUpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _smtp_host(self, h):
        from oikonome.engine import budget
        conn = h.conn()
        try:
            return budget.load_config(conn).get("smtp_host")
        finally:
            conn.close()

    def test_an_archive_wants_the_step_up_before_anything_is_spent(self):
        h = SignedInClient("su")
        conn = h.conn()
        try:
            from oikonome.engine import budget
            with budget.config_txn(conn) as cfg:
                cfg["smtp_host"] = OURS
        finally:
            conn.close()
        h.stale_elevation()
        plan = h.analyze([("export.zip", _archive()),
                          ("bank.csv", csv_for("SHOP A"))])
        decisions = [dict(f, action="import") for f in plan["files"]]
        decisions[1]["account_id"] = "chk"
        body = {"token": plan["token"], "files": decisions,
                "background": True}
        with mock.patch.object(bulk_import_job, "nudge"):
            r = h.c.post("/api/import/bulk/run", json=body)
            self.assertEqual(r.status_code, 403, r.text)
            self.assertEqual(r.json()["detail"]["error"],
                             "elevation_required")
            conn = h.conn()
            try:
                self.assertIsNotNone(
                    staging.peek(conn, "bulk", plan["token"]),
                    "the refusal spent the upload")
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM bulk_import_jobs").fetchone())
            finally:
                conn.close()
            self.assertEqual(self._smtp_host(h), OURS)
            r = h.c.post("/api/import/bulk/run", json=dict(body, password=PW))
        self.assertEqual(r.status_code, 202, r.text)
        self.assertTrue(r.json().get("restore_started"), r.text)
        job_id = r.json()["job_id"]
        conn = h.conn()
        try:
            rows = file_rows(conn, job_id)
        finally:
            conn.close()
        self.assertEqual([r["filename"] for r in rows.values()], ["bank.csv"])
        for _ in range(200):
            st = h.c.get("/api/restore/progress").json()
            if st.get("state") != "running":
                break
            time.sleep(0.05)
        self.assertEqual(self._smtp_host(h), THEIRS)
        # the archive's outcome is part of the job's answer
        st = h.c.get(f"/api/import/bulk/jobs/{job_id}").json()
        zips = [f for f in st["files"] if f["name"] == "export.zip"]
        self.assertTrue(zips and zips[0].get("restore_started"), st)


if __name__ == "__main__":
    unittest.main()
