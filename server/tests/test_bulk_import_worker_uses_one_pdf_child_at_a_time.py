"""The worker never extracts two bulk-import PDFs at once.

Each PDF is read in a child process allowed several hundred megabytes;
two households' folder imports running side by side would put two of
those beside the worker's own memory. Folder imports share one PDF slot,
so the second household's PDF waits for the first — but only for an
extraction, never for another household's turn at its own sync lock."""

import threading
import time
import unittest
from unittest import mock

from oikonome.db import staging, tenancy
from oikonome.jobs import bulk_import_job

from .bulk_job_util import submit, tenant_of
from .util import _ensure_db, make_db, write_config

PDF = b"%PDF-1.4\n% not a real statement\n"


class OnePdfChildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_two_jobs_never_extract_together(self):
        conns, jobs = [], []
        try:
            for _ in range(2):
                conn = make_db()
                write_config(conn)
                token = staging.put(conn, "bulk", [("a.pdf", PDF),
                                                   ("b.pdf", PDF)])
                with mock.patch.object(bulk_import_job, "nudge"):
                    sub = submit(conn, token, [
                        {"index": i, "action": "import",
                         "account_id": "chk", "amount_sign": "statement"}
                        for i in range(2)])
                conns.append(conn)
                jobs.append((tenant_of(conn), sub["job_id"]))
            lock = threading.Lock()
            now = [0]
            peak = [0]

            def extraction(*a, **kw):
                with lock:
                    now[0] += 1
                    peak[0] = max(peak[0], now[0])
                time.sleep(0.2)
                with lock:
                    now[0] -= 1
                raise ValueError("this PDF could not be read")

            with mock.patch("oikonome.sync.pdfimport._run_extraction",
                            side_effect=extraction):
                threads = [threading.Thread(target=bulk_import_job.run_job,
                                            args=job) for job in jobs]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=60)
            self.assertEqual(peak[0], 1)
            for conn, (_, job_id) in zip(conns, jobs):
                st = bulk_import_job.status(conn, job_id)
                self.assertEqual(st["state"], "done", st)
                self.assertEqual(st["done"], 2)
        finally:
            for conn in conns:
                conn.close()

    def test_a_household_waiting_for_its_own_lock_leaves_the_slot_free(self):
        """A file waits for its household's sync lock (a sync or a restore
        holds it) without holding the worker's PDF slot, so another
        household's folder PDFs import meanwhile instead of queueing
        behind a wait that is not theirs."""
        conns, jobs = [], []
        blocker = None
        try:
            for _ in range(2):
                conn = make_db()
                write_config(conn)
                token = staging.put(conn, "bulk", [("a.pdf", PDF)])
                with mock.patch.object(bulk_import_job, "nudge"):
                    sub = submit(conn, token, [
                        {"index": 0, "action": "import",
                         "account_id": "chk", "amount_sign": "statement"}])
                conns.append(conn)
                jobs.append((tenant_of(conn), sub["job_id"]))
            busy_tid = jobs[0][0]
            blocker = tenancy.tenant_connect(busy_tid)
            blocker.execute("SELECT pg_advisory_lock(hashtext(%s))",
                            (f"oikonome:sync:{busy_tid}",))
            waits = []

            def sleep(s):
                waits.append(threading.current_thread().name)
                time.sleep(0.02)

            with mock.patch("oikonome.sync.pdfimport._run_extraction",
                            side_effect=ValueError("not a statement")), \
                    mock.patch.object(bulk_import_job, "_sleep",
                                      side_effect=sleep), \
                    mock.patch.object(bulk_import_job,
                                      "LOCK_GIVE_UP_SECONDS", 60):
                busy = threading.Thread(target=bulk_import_job.run_job,
                                        args=jobs[0], name="busy")
                busy.start()
                for _ in range(200):
                    if waits.count("busy") >= 2:
                        break
                    time.sleep(0.01)
                self.assertGreaterEqual(waits.count("busy"), 2)
                other = threading.Thread(target=bulk_import_job.run_job,
                                         args=jobs[1], name="other")
                other.start()
                other.join(timeout=5)
                other_finished = not other.is_alive()
                blocker.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                                (f"oikonome:sync:{busy_tid}",))
                busy.join(timeout=30)
                other.join(timeout=30)
            self.assertTrue(other_finished, "the other household's PDF "
                            "waited behind a lock that was not its own")
            for conn, (_, job_id) in zip(conns, jobs):
                st = bulk_import_job.status(conn, job_id)
                self.assertEqual(st["state"], "done", st)
                self.assertEqual(st["done"], 1)
        finally:
            if blocker is not None:
                blocker.close()
            for conn in conns:
                conn.close()


if __name__ == "__main__":
    unittest.main()
