"""A build that predates the folder-import job still gets a short wait
for its results, but the wait holds no database connection, and posting
the same plan again answers at once.

The tenant connections are one small pool shared by every household on
the instance. A wait that kept one checked out for its whole length let a
handful of old clients — or one household reposting its token in a loop
while a long folder imported — leave every other household's requests
queueing for a connection until they failed."""

import threading
import time
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.jobs import bulk_import_job
from oikonome.web import bulk_import

from .bulk_job_util import SignedInClient, csv_for
from .util import _ensure_db


def _checked_out() -> int:
    stats = tenancy._pool(tenancy.APP_DSN).get_stats()
    return stats.get("pool_size", 0) - stats.get("pool_available", 0)


class OldClientWaitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.h = SignedInClient("ow")
        plan = self.h.analyze([(f"bank-{t}.csv", csv_for(f"SHOP {t}"))
                               for t in ("A", "B")])
        self.token = plan["token"]
        self.decisions = [dict(f, account_id="chk") for f in plan["files"]]

    def test_the_wait_for_results_keeps_no_pooled_connection(self):
        """While the request waits for a job no worker has picked up, the
        pool has nothing checked out on its behalf (a poll borrows one for
        a moment and gives it back)."""
        before = _checked_out()
        answer = {}

        def post():
            answer["r"] = self.h.c.post("/api/import/bulk/run", json={
                "token": self.token, "files": self.decisions})

        with mock.patch.object(bulk_import_job, "nudge"), \
                mock.patch.object(bulk_import, "COMPAT_WAIT_SECONDS", 1.6), \
                mock.patch.object(bulk_import, "COMPAT_POLL_SECONDS", 0.1):
            t = threading.Thread(target=post)
            t.start()
            time.sleep(0.4)          # the submit is done; the wait is on
            samples = []
            for _ in range(20):
                samples.append(_checked_out() - before)
                time.sleep(0.04)
            t.join(timeout=30)
        self.assertEqual(answer["r"].status_code, 200, answer["r"].text)
        held = sum(1 for s in samples if s > 0)
        self.assertLessEqual(held, 4, f"a connection stayed checked out "
                                      f"through the wait: {samples}")

    def test_posting_the_same_plan_again_answers_without_waiting(self):
        """The job already exists, so a repost reads it back at once — the
        not yet imported files marked pending — instead of waiting again."""
        with mock.patch.object(bulk_import_job, "nudge"):
            r = self.h.c.post("/api/import/bulk/run", json={
                "token": self.token, "files": self.decisions,
                "background": True})
            self.assertEqual(r.status_code, 202, r.text)
            with mock.patch.object(bulk_import, "COMPAT_WAIT_SECONDS", 3.0):
                t0 = time.monotonic()
                r = self.h.c.post("/api/import/bulk/run", json={
                    "token": self.token, "files": self.decisions})
                took = time.monotonic() - t0
        self.assertEqual(r.status_code, 200, r.text)
        self.assertLess(took, 1.0, "the repost waited for the job again")
        results = r.json()["results"]
        self.assertEqual(len(results), 2)
        self.assertTrue(all(row["pending"] for row in results), results)


if __name__ == "__main__":
    unittest.main()
