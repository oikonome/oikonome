"""The hourly bank sync spreads across the hour instead of firing for the
whole fleet at :00. Each tenant hashes to one of SYNC_BUCKETS (12) stable
five-minute slots and syncs there every hour — same per-tenant cadence,
no thundering herd against Plaid's per-client rate limit, and the last
tenant of a big fleet no longer starts its "hourly" pull tens of minutes
late behind everyone else's."""

import unittest
from unittest import mock

from oikonome.jobs import worker


class SyncSpreadTests(unittest.TestCase):

    def test_bucket_is_stable_and_in_range(self):
        tid = "a3b8c2d1-0000-4000-8000-000000000042"
        b1 = worker._tenant_bucket(tid)
        b2 = worker._tenant_bucket(tid)
        self.assertEqual(b1, b2, "a tenant's slot must never move")
        self.assertIn(b1, range(worker.SYNC_BUCKETS))

    def test_sweep_serves_only_the_named_bucket(self):
        tids = [f"tenant-{i}" for i in range(40)]
        target = 3
        expected = [t for t in tids
                    if worker._tenant_bucket(t) == target]
        self.assertTrue(expected, "fixture must land tenants in the slot")
        ran = []
        with mock.patch.object(worker, "_tenant_ids", return_value=tids):
            out = worker._sweep(ran.append, "sync", bucket=target)
        self.assertEqual(ran, expected)
        self.assertEqual(set(out), set(expected))

    def test_no_bucket_means_everyone(self):
        tids = [f"tenant-{i}" for i in range(10)]
        ran = []
        with mock.patch.object(worker, "_tenant_ids", return_value=tids):
            worker._sweep(ran.append, "nightly")
        self.assertEqual(ran, tids)

    def test_cron_fires_every_five_minutes(self):
        """The slot math needs a firing per slot: (minute // 5) % 12 only
        reaches all twelve buckets if the cron runs on every multiple of
        five. A stray edit back to minute=0 would silently stop syncing
        11/12ths of the fleet."""
        job = next(j for j in worker.WorkerSettings.cron_jobs
                   if j.coroutine is worker.sync_all)
        self.assertEqual(job.minute, set(range(0, 60, 5)))

    def test_every_bucket_is_reachable_from_the_clock(self):
        slots = {(m // 5) % worker.SYNC_BUCKETS for m in range(0, 60, 5)}
        self.assertEqual(slots, set(range(worker.SYNC_BUCKETS)))


class ManualSweepReachesEveryTenantTests(unittest.TestCase):
    """The console's 'run sync sweep now' is a fleet-wide trigger. The cron
    job serves one five-minute slot per firing, so enqueuing it by hand
    reached about one tenant in twelve while the console said the fleet
    was queued — on a one-household install, usually nobody."""

    def test_console_sync_button_sweeps_every_bucket(self):
        import asyncio

        from oikonome.web import adminconsole
        job = adminconsole._RUN_JOBS["sync_all"]
        names = {getattr(x, "name", None) or x.__name__
                 for x in worker.WorkerSettings.functions}
        self.assertIn(job, names,
                      "the console must enqueue a function the worker knows")
        fn = getattr(worker, job)
        tids = [f"tenant-{i}" for i in range(40)]
        ran = []
        with mock.patch.object(worker, "_tenant_ids", return_value=tids), \
             mock.patch.object(worker, "sync_tenant", ran.append):
            asyncio.run(fn({}))
        self.assertEqual(sorted(ran), sorted(tids))
