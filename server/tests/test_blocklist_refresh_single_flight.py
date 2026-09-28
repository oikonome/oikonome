"""The public blocklist refresh is single-flight.

`refresh_lists` fetches both feeds and then DELETEs and re-INSERTs
spam_domains_public. Two doors reach it — the daily cron and the admin
console's run-job button — so two runs can overlap: both fetch, both
delete, and the second INSERT pass collides with the first's rows (unique
domain key) and rolls back after doing all the network work. A held
session-level TRY lock makes the late arrival skip instead, and the lock
is released afterwards so the next refresh runs.
"""

import unittest

from oikonome.db import tenancy
from oikonome.web import spamdefense

from .util import _ensure_db


class BlocklistRefreshSingleFlightTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self._orig_fetch = spamdefense._fetch_list
        spamdefense._fetch_list = lambda url: ["single-flight.test"]

    def tearDown(self):
        spamdefense._fetch_list = self._orig_fetch

    def test_skips_while_another_refresh_holds_the_lock(self):
        holder = tenancy.admin_connect()
        try:
            got = holder.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (spamdefense.REFRESH_LOCK_KEY,)).fetchone()["ok"]
            self.assertTrue(got, "the test could not take the lock itself")
            res = spamdefense.refresh_lists()
        finally:
            holder.close()                 # session lock goes with it
        self.assertFalse(res["ok"])
        self.assertEqual(res["skipped"], "already-running")

    def test_lock_is_released_after_a_run(self):
        first = spamdefense.refresh_lists()
        self.assertTrue(first["ok"])
        second = spamdefense.refresh_lists()
        self.assertTrue(second["ok"], second)
        self.assertNotIn("skipped", second)

    def test_worker_reports_skip_not_fetch_error(self):
        from oikonome.jobs import worker
        holder = tenancy.admin_connect()
        try:
            holder.execute("SELECT pg_advisory_lock(hashtext(%s))",
                           (spamdefense.REFRESH_LOCK_KEY,))
            self.assertEqual(worker._blocklist_refresh_body(),
                             "skipped:already-running")
        finally:
            holder.close()


if __name__ == "__main__":
    unittest.main()
