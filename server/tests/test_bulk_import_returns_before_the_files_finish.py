"""A folder import answers as soon as its plan is accepted; the files land
afterwards, on the worker.

Run inside the request, two hundred statements kept a web worker, a
database connection and the household's sync lock busy for as long as
they took — hours, for PDFs — while the edge in front of a hosted
instance gave up after about a hundred seconds. The person read a failure
about an import that was still landing."""

import unittest
from unittest import mock

from oikonome.jobs import bulk_import_job
from oikonome.web import bulk_import

from .bulk_job_util import SignedInClient, csv_for
from .util import _ensure_db


class BulkImportRunsInTheBackgroundTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.h = SignedInClient("bg")

    def _plan(self):
        plan = self.h.analyze([(f"bank-{t}.csv", csv_for(f"SHOP {t}"))
                               for t in ("A", "B", "C")])
        return plan, [dict(f, account_id="chk") for f in plan["files"]]

    def test_the_submit_answers_with_a_job_and_writes_no_rows(self):
        plan, decisions = self._plan()
        with mock.patch.object(bulk_import_job, "nudge"):
            r = self.h.c.post("/api/import/bulk/run", json={
                "token": plan["token"], "files": decisions,
                "background": True})
        self.assertEqual(r.status_code, 202, r.text)
        body = r.json()
        self.assertIn(body["state"], ("queued", "running"))
        self.assertEqual(body["total"], 3)
        self.assertEqual(self.h.ledger_count(), 0,
                         "rows were written inside the request")
        conn = self.h.conn()
        try:
            bulk_import_job.run_job(self.h.tid, body["job_id"])
            st = bulk_import_job.status(conn, body["job_id"])
        finally:
            conn.close()
        self.assertEqual(st["state"], "done", st)
        self.assertEqual(st["done"], 3)
        self.assertTrue(all(f["ok"] for f in st["files"]), st)
        self.assertEqual(self.h.ledger_count(), 9)
        # and the progress doors read the same job back
        r = self.h.c.get(f"/api/import/bulk/jobs/{body['job_id']}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "done")
        r = self.h.c.get("/api/import/bulk/jobs/current")
        self.assertEqual(r.json()["job_id"], body["job_id"])

    def test_an_older_client_still_gets_a_results_list(self):
        """A build that predates the job posts the plan and reads
        `results`: it gets 200 and one row per file, the ones not yet
        imported marked pending, never a 202 with nothing to read."""
        plan, decisions = self._plan()
        with mock.patch.object(bulk_import_job, "nudge"), \
                mock.patch.object(bulk_import, "COMPAT_WAIT_SECONDS", 0.2):
            r = self.h.c.post("/api/import/bulk/run", json={
                "token": plan["token"], "files": decisions})
        self.assertEqual(r.status_code, 200, r.text)
        results = r.json()["results"]
        self.assertEqual(len(results), 3)
        for row in results:
            self.assertFalse(row["ok"])
            self.assertTrue(row["pending"])
            self.assertIn("background", row["error"])

    def test_with_no_job_the_current_door_reads_idle(self):
        r = self.h.c.get("/api/import/bulk/jobs/current")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "idle")


if __name__ == "__main__":
    unittest.main()
