"""A nightly that cannot get the household's Amazon-chain lock in time
skips that one step and runs the rest of the night.

The hourly sync can hold the chain lock for minutes while a slow model
classifies and summarizes. The nightly waits only so long for it; when the
wait runs out, the receipt pass, the savings milestones and the
nightly-detect heartbeat after the chain must still run, or two such nights
read as a broken nightly.
"""

import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import llm_categorize
from oikonome.jobs import worker

from .util import make_db


class BusyChainLockTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.other = tenancy.tenant_connect(self.tid)

    def tearDown(self):
        self.other.close()
        self.conn.close()

    def test_the_steps_after_a_busy_chain_still_run(self):
        self.other.execute(
            f"SELECT pg_advisory_lock({llm_categorize._AMAZON_LOCK})")
        try:
            with mock.patch.object(llm_categorize, "LOCK_WAIT_SECONDS", 0.3), \
                 mock.patch("oikonome.engine.amazon_match.run_match") as am, \
                 mock.patch("oikonome.engine.receipts.parse_pending") as rp:
                stats = worker.nightly_tenant(self.tid)
        finally:
            self.other.execute(
                f"SELECT pg_advisory_unlock({llm_categorize._AMAZON_LOCK})")
        am.assert_not_called()
        rp.assert_called_once()
        self.assertEqual(stats.get("amazon_chain_skipped"), "busy")
        beat = self.conn.execute(
            "SELECT ran_at FROM job_runs WHERE job = 'nightly-detect'"
        ).fetchone()
        self.assertIsNotNone(beat, "the nightly-detect heartbeat never ran")


    def test_a_failed_recurring_detection_leaves_the_heartbeat_unstamped(self):
        """Recurring detection failing every night (a data defect in one
        household) must not stamp nightly-detect as healthy: Doctor reads
        that stamp, and a fresh '+0 proposals' hid due dates that had
        stopped rolling."""
        with mock.patch("oikonome.engine.bills.run",
                        side_effect=ValueError("bad raw")):
            stats = worker.nightly_tenant(self.tid)
        self.assertEqual(stats.get("error"), "recurring detection failed")
        beat = self.conn.execute(
            "SELECT ran_at FROM job_runs WHERE job = 'nightly-detect'"
        ).fetchone()
        self.assertIsNone(beat)


if __name__ == "__main__":
    unittest.main()
