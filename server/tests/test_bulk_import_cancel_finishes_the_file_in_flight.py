"""Cancelling a folder import lets the file in flight finish and drops
the rest, bytes and all.

A file is imported in one transaction, so stopping one half-way would
either roll it back (work thrown away) or, under an importer that writes
as it goes, leave it half landed. The cancel is read between files."""

import unittest
from unittest import mock

from oikonome.db import staging
from oikonome.jobs import bulk_import_job

from .bulk_job_util import (SignedInClient, csv_for, file_rows, submit,
                            tenant_of)
from .util import _ensure_db, make_db, write_config


class CancelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_a_cancel_after_the_first_file(self):
        conn = make_db()
        try:
            write_config(conn)
            token = staging.put(conn, "bulk", [
                (f"bank-{t}.csv", csv_for(f"SHOP {t}")) for t in "ABC"])
            with mock.patch.object(bulk_import_job, "nudge"):
                sub = submit(conn, token, [
                    {"index": i, "action": "import", "account_id": "chk"}
                    for i in range(3)])
            real = bulk_import_job._run_one
            calls = []

            def first_then_cancel(*a, **kw):
                out = real(*a, **kw)
                calls.append(1)
                if len(calls) == 1:
                    self.assertEqual(
                        bulk_import_job.cancel(conn, sub["job_id"])["state"],
                        "cancelling")
                return out

            with mock.patch.object(bulk_import_job, "_run_one",
                                   side_effect=first_then_cancel):
                bulk_import_job.run_job(tenant_of(conn), sub["job_id"])
            self.assertEqual(len(calls), 1)
            st = bulk_import_job.status(conn, sub["job_id"])
            rows = file_rows(conn, sub["job_id"])
            self.assertEqual(st["state"], "cancelled", st)
            self.assertEqual(rows[0]["state"], "done")
            self.assertEqual([rows[1]["state"], rows[2]["state"]],
                             ["skipped", "skipped"])
            self.assertTrue(all(r["gone"] for r in rows.values()))
            self.assertEqual(conn.execute(
                "SELECT count(*) AS n FROM transactions"
                " WHERE name LIKE 'SHOP%%'").fetchone()["n"], 3)
        finally:
            conn.close()

    def test_a_queued_job_cancels_at_once_through_the_door(self):
        h = SignedInClient("cx")
        plan = h.analyze([("bank-a.csv", csv_for("SHOP A"))])
        with mock.patch.object(bulk_import_job, "nudge"):
            r = h.c.post("/api/import/bulk/run", json={
                "token": plan["token"], "background": True,
                "files": [dict(f, account_id="chk") for f in plan["files"]]})
        job_id = r.json()["job_id"]
        r = h.c.post(f"/api/import/bulk/jobs/{job_id}/cancel")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["state"], "cancelled")
        bulk_import_job.run_job(h.tid, job_id)
        self.assertEqual(h.ledger_count(), 0)
        r = h.c.post("/api/import/bulk/jobs/not-a-job/cancel")
        self.assertEqual(r.status_code, 404, r.text)


if __name__ == "__main__":
    unittest.main()
