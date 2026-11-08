"""Whole-export restores share the web process, so they are capped there.

A restore runs for minutes on a worker thread inside the web process and
holds a tenant connection for its whole transaction. The per-household
claim stops one household racing itself, but nothing stopped several
households restoring at once: each held pooled tenant connections until it
finished, and a handful of them starved every other request on the
instance into pool timeouts.

What these protect: past the process-wide cap a further restore — any
household's — is refused with a sentence a person can act on and claims no
job row; a running restore holds ONE tenant connection, not two; and while
the cap is full an ordinary request still gets a connection.
"""

import io
import threading
import time
import unittest
import zipfile

from oikonome.db import tenancy
from oikonome.sync import restore, restore_job

from .util import make_db


def _zip() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("tenant_settings.csv", "config\n{}\n")
    return buf.getvalue()


def _tid(conn) -> str:
    return conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]


def _checked_out() -> int:
    st = tenancy._pool(tenancy.APP_DSN).get_stats()
    return st.get("pool_size", 0) - st.get("pool_available", 0)


class RestoresAtOnceTests(unittest.TestCase):

    def setUp(self):
        self.conns = [make_db() for _ in range(restore_job.RESTORES_AT_ONCE + 1)]
        for c in self.conns:
            self.addCleanup(c.close)
        self.release = threading.Event()
        self.entered = threading.Semaphore(0)
        real = restore.restore_zip

        def held(conn, data, **kw):
            self.entered.release()
            self.release.wait(20)
            return real(conn, data, **kw)

        restore.restore_zip = held
        self.addCleanup(setattr, restore, "restore_zip", real)
        self.addCleanup(self.release.set)

    def _settle(self, conn):
        for _ in range(200):
            if restore_job.status(conn)["state"] in ("done", "error"):
                return
            time.sleep(0.05)
        self.fail("restore job never settled")

    def test_a_restore_past_the_cap_is_refused_and_requests_still_get_a_connection(self):
        cap = restore_job.RESTORES_AT_ONCE
        before = _checked_out()
        for c in self.conns[:cap]:
            out = restore_job.start(_tid(c), _zip())
            self.assertTrue(out.get("restore_started"), out)
        for _ in range(cap):
            self.assertTrue(self.entered.acquire(timeout=10))
        # each running restore holds exactly one tenant connection
        self.assertEqual(_checked_out() - before, cap)

        extra = self.conns[cap]
        out = restore_job.start(_tid(extra), _zip())
        self.assertIn("try again in a few minutes", out.get("error", ""))
        # the refused household was not left with a claimed job row
        self.assertEqual(restore_job.status(extra)["state"], "idle")

        # an ordinary request is still served a pooled connection
        probe = tenancy.tenant_connect(_tid(extra))
        try:
            self.assertEqual(probe.execute("SELECT 1 AS one").fetchone()["one"], 1)
        finally:
            probe.close()

        self.release.set()
        for c in self.conns[:cap]:
            self._settle(c)
        # the slots come back once the restores finish
        out = restore_job.start(_tid(extra), _zip())
        self.assertTrue(out.get("restore_started"), out)
        self._settle(extra)

    def test_a_claim_refused_for_the_household_gives_its_slot_back(self):
        c = self.conns[0]
        c.execute(
            """INSERT INTO job_progress (id, state, progress)
               VALUES (%s, 'running', '{}'::jsonb)""", (restore_job.JOB_ID,))
        for _ in range(restore_job.RESTORES_AT_ONCE + 1):
            self.assertIn("already running",
                          restore_job.start(_tid(c), _zip()).get("error", ""))
        out = restore_job.start(_tid(self.conns[1]), _zip())
        self.assertTrue(out.get("restore_started"), out)
        self.release.set()
        self._settle(self.conns[1])


if __name__ == "__main__":
    unittest.main()
