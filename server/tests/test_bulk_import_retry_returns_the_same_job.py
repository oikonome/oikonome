"""Posting the same folder plan twice answers the same job.

The first request used to spend the staged upload before any work, so a
retry after the edge timed out was told the batch had expired, or that a
sync was running — the first request's own lock — while the import it
asked about was landing. Neither was true, and both sent the person to
upload the folder again."""

import unittest
from unittest import mock

from oikonome.jobs import bulk_import_job
from oikonome.web import bulk_import

from .bulk_job_util import SignedInClient, csv_for
from .util import _ensure_db


class BulkRetryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_a_retry_answers_the_job_and_imports_nothing_twice(self):
        h = SignedInClient("rt")
        plan = h.analyze([("bank-a.csv", csv_for("SHOP A")),
                          ("bank-b.csv", csv_for("SHOP B"))])
        decisions = [dict(f, account_id="chk") for f in plan["files"]]
        body = {"token": plan["token"], "files": decisions,
                "background": True}
        with mock.patch.object(bulk_import_job, "nudge"), \
                mock.patch.object(bulk_import, "COMPAT_WAIT_SECONDS", 0.1):
            first = h.c.post("/api/import/bulk/run", json=body)
            self.assertEqual(first.status_code, 202, first.text)
            again = h.c.post("/api/import/bulk/run", json=body)
            self.assertIn(again.status_code, (200, 202), again.text)
            self.assertEqual(again.json()["job_id"], first.json()["job_id"])
            self.assertNotIn("expired", again.text)
            self.assertNotIn("sync", again.text)
            bulk_import_job.run_job(h.tid, first.json()["job_id"])
            landed = h.ledger_count()
            self.assertEqual(landed, 6)
            # an older client's retry, after the job has finished, reads
            # the finished rows
            late = h.c.post("/api/import/bulk/run",
                            json={"token": plan["token"], "files": decisions})
        self.assertEqual(late.status_code, 200, late.text)
        self.assertTrue(all(r["ok"] for r in late.json()["results"]),
                        late.text)
        bulk_import_job.run_job(h.tid, first.json()["job_id"])
        self.assertEqual(h.ledger_count(), landed)

    def test_a_second_folder_waits_for_the_first(self):
        """One live job per household: a second plan submitted while the
        first runs is refused, and its upload stays staged for later."""
        h = SignedInClient("rt2")
        one = h.analyze([("bank-a.csv", csv_for("SHOP A"))])
        two = h.analyze([("bank-b.csv", csv_for("SHOP B"))])
        with mock.patch.object(bulk_import_job, "nudge"):
            r = h.c.post("/api/import/bulk/run", json={
                "token": one["token"], "background": True,
                "files": [dict(f, account_id="chk") for f in one["files"]]})
            self.assertEqual(r.status_code, 202, r.text)
            r = h.c.post("/api/import/bulk/run", json={
                "token": two["token"], "background": True,
                "files": [dict(f, account_id="chk") for f in two["files"]]})
        self.assertEqual(r.status_code, 409, r.text)
        from oikonome.db import staging
        conn = h.conn()
        try:
            self.assertIsNotNone(staging.peek(conn, "bulk", two["token"]))
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
