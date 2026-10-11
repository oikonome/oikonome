"""A folder plan is decided once, when it is submitted.

A plan that leaves an export ZIP unticked asks for no step-up. If a later
post of the same token could re-decide the plan — tick the archive, move
it to the front — a session that never proved who it was would start a
whole-ledger restore, which repoints the household's mail relay,
recipients and AI endpoint. Every later post answers the job the first
one froze."""

import unittest
from unittest import mock

from oikonome.jobs import bulk_import_job
from oikonome.sync import restore_job

from .bulk_job_util import SignedInClient, file_rows
from .test_restore_doors_want_elevation import _archive
from .util import _ensure_db

PDF = b"%PDF-1.4\n% not a real statement\n"


class FrozenPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_a_later_post_cannot_tick_the_archive(self):
        h = SignedInClient("fz")
        h.stale_elevation()
        plan = h.analyze([("a.pdf", PDF), ("b.pdf", PDF), ("c.pdf", PDF),
                          ("evil.zip", _archive())])
        decisions = [dict(f, account_id="chk") for f in plan["files"]]
        decisions[3]["action"] = "skip"
        with mock.patch.object(bulk_import_job, "nudge"), \
                mock.patch.object(restore_job, "start") as start:
            r = h.c.post("/api/import/bulk/run", json={
                "token": plan["token"], "files": decisions,
                "background": True})
            self.assertEqual(r.status_code, 202, r.text)
            job_id = r.json()["job_id"]
            flipped = [dict(decisions[3], action="import")] + decisions[:3]
            r = h.c.post("/api/import/bulk/run", json={
                "token": plan["token"], "files": flipped,
                "background": True})
            self.assertIn(r.status_code, (200, 202), r.text)
            self.assertEqual(r.json()["job_id"], job_id)
            start.assert_not_called()
        conn = h.conn()
        try:
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM job_progress WHERE id = 'restore'").fetchone())
            rows = file_rows(conn, job_id)
            self.assertEqual(sorted(rows), [0, 1, 2])
            self.assertFalse(any(r["filename"].endswith(".zip")
                                 for r in rows.values()))
            frozen = conn.execute(
                "SELECT decisions FROM bulk_import_jobs WHERE id = %s",
                (job_id,)).fetchone()["decisions"]
            self.assertEqual([d["file_id"] for d in frozen["files"]],
                             [0, 1, 2])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
