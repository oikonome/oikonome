"""A whole-ledger restore runs in the background, and says where it is.

Held inside the upload, a large archive (tens of thousands of transactions, minutes of
merging) outlives the edge timeout in front of a hosted instance: the
browser is handed an error page for work that succeeds, and the obvious
response — upload it again — is the worst one. The upload now starts a job
and returns; the client polls this state to completion.

What these protect: the claim is exclusive (two merges must not race), a
finished job carries the result the page renders, a failed one carries a
message meant for a person, and progress moves while it runs.
"""

import csv
import io
import json
import unittest
import zipfile

from oikonome.sync import restore, restore_job

from .util import make_db


def _zip(n_txn: int = 0) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader(); w.writerow({"config": json.dumps({})})
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


class RestoreJobTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id', true) AS t"
        ).fetchone()["t"]

    def _claim(self):
        """Claim the row without spawning the worker thread."""
        return self.conn.execute(
            """INSERT INTO job_progress (id, state, progress)
               VALUES (%s, 'running', '{}'::jsonb)
               ON CONFLICT (tenant_id, id) DO UPDATE SET
                   state='running', started_at=now(), updated_at=now()
               WHERE job_progress.state != 'running'
                  OR job_progress.updated_at < now() - %s::interval
               RETURNING id""",
            (restore_job.JOB_ID, restore_job.STALE_AFTER)).fetchone()

    def test_idle_before_anything_runs(self):
        self.assertEqual(restore_job.status(self.conn)["state"], "idle")

    def test_a_second_restore_is_refused_while_one_runs(self):
        self.assertIsNotNone(self._claim())
        out = restore_job.start(self.tid, _zip())
        self.assertIn("already running", out.get("error", ""))

    def test_a_finished_job_carries_the_result_the_page_renders(self):
        restore_job.start(self.tid, _zip())
        st = self._settle()
        self.assertEqual(st["state"], "done")
        self.assertIn("source", st["progress"]["result"])

    def test_a_finished_restore_stays_visible_then_becomes_history(self):
        """The import page shows this state whenever it is opened, so a
        reload mid-restore still finds the job — but the row is never
        deleted, and without a window a restore from months ago would
        greet every visit forever."""
        restore_job.start(self.tid, _zip())
        self.assertEqual(self._settle()["state"], "done")
        # an hour on, still the same row, still done underneath
        self.conn.execute(
            "UPDATE job_progress SET updated_at = now() - %s::interval "
            "WHERE id=%s",
            (f"{restore_job.SETTLED_VISIBLE_SECONDS + 60} seconds",
             restore_job.JOB_ID))
        self.assertEqual(restore_job.status(self.conn)["state"], "idle")
        # ...and a fresh restore is not refused by that stale row
        self.assertIsNotNone(self._claim())

    def test_a_bad_archive_reports_a_message_meant_for_a_person(self):
        restore_job.start(self.tid, b"not a zip at all")
        st = self._settle()
        self.assertEqual(st["state"], "error")
        self.assertTrue(st["progress"]["error"])

    def test_restore_refuses_while_the_sync_lock_is_held(self):
        """The restore's data write is mutually exclusive with a purge:
        both contend on oikonome:sync:<tenant>. Without that lock a purge
        running beside an uncommitted restore deletes only the rows that
        exist, and the restore's later commit resurrects an erased tenant.
        With a sync/purge lock held, the restore must refuse — not write
        data behind it."""
        from oikonome.db import tenancy as _t
        holder = _t.admin_connect()
        self.addCleanup(holder.close)
        self.assertTrue(holder.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (f"oikonome:sync:{self.tid}",)).fetchone()["ok"])
        # claim the row start() would have claimed, then run the
        # worker-thread body directly with the lock held
        self.assertIsNotNone(self._claim())
        restore_job._run(self.tid, _zip(3))
        st = restore_job.status(self.conn)
        self.assertEqual(st["state"], "error")
        self.assertIn("busy", st["progress"]["error"])
        holder.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                       (f"oikonome:sync:{self.tid}",))

    def _settle(self, tries: int = 100) -> dict:
        import time
        for _ in range(tries):
            st = restore_job.status(self.conn)
            if st["state"] in ("done", "error"):
                return st
            time.sleep(0.1)
        self.fail("restore job never settled")

    def test_progress_ticks_name_the_table_being_read(self):
        seen = []
        restore.restore_zip(self.conn, _zip(),
                            progress=lambda t, n, d: seen.append((t, n, d)))
        self.assertTrue(any(t == "tenant_settings.csv" for t, _, _ in seen))

    def test_a_synchronous_restore_reports_nothing(self):
        # the ContextVar must not leak between calls, or an inline restore
        # would write progress for a job that does not exist
        restore.restore_zip(self.conn, _zip())
        self.assertIsNone(restore._PROGRESS.get())


if __name__ == "__main__":
    unittest.main()
