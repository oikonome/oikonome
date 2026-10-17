"""The worker takes fewer jobs at once than it has threads, and the jobs
that pull banks may run for as long as a multi-year pull takes.

Every job runs its work in a thread, and asyncio sizes its default
executor from the CPU count (six on a two-vCPU box), so a burst of bank
webhooks queued jobs behind threads while their arq timeouts ran down.
Whether a job WAITING for a thread is charged against its own timeout is
arq's bookkeeping and cannot be observed without a live Redis-backed
worker, so these tests pin the sizes that keep it from happening."""

import asyncio
import os
import unittest
from unittest import mock

from oikonome.jobs import worker


def _by_name(settings):
    out = {}
    for f in settings.functions:
        name = getattr(f, "name", None) or getattr(f, "__name__", None)
        out[name] = f
    return out


class WorkerSizingTests(unittest.TestCase):
    def test_default_threads_leave_headroom_over_concurrent_jobs(self):
        with mock.patch.dict(os.environ, {"OIKONOME_WORKER_THREADS": "",
                                          "OIKONOME_POOL_MAX": ""}):
            self.assertEqual(worker._worker_threads(), 12)
        self.assertEqual(worker.WorkerSettings.max_jobs,
                         worker.WORKER_THREADS - worker.WORKER_THREAD_HEADROOM)
        self.assertGreater(worker.WORKER_THREADS,
                           worker.WorkerSettings.max_jobs)

    def test_unset_threads_keep_the_jobs_within_the_workers_pool(self):
        for pool in ("4", "10", "16"):
            with mock.patch.dict(os.environ, {"OIKONOME_WORKER_THREADS": "",
                                              "OIKONOME_POOL_MAX": pool}):
                threads = worker._worker_threads()
            self.assertEqual(threads - worker.WORKER_THREAD_HEADROOM,
                             int(pool), pool)

    def test_compose_gives_the_worker_its_own_pool_knobs(self):
        import pathlib

        import yaml
        root = pathlib.Path(__file__).resolve().parents[2]
        doc = yaml.safe_load((root / "docker" / "compose.yaml").read_text())
        wenv = doc["services"]["worker"]["environment"]
        aenv = doc["services"]["app"]["environment"]
        self.assertEqual(wenv["OIKONOME_POOL_MAX"],
                         "${OIKONOME_WORKER_POOL_MAX:-${OIKONOME_POOL_MAX:-}}")
        self.assertEqual(
            wenv["OIKONOME_CONTROL_POOL_MAX"],
            "${OIKONOME_WORKER_CONTROL_POOL_MAX:-"
            "${OIKONOME_CONTROL_POOL_MAX:-}}")
        self.assertIn("OIKONOME_FIRST_SYNC_CONCURRENCY", wenv)
        # the web process keeps the shared knobs
        self.assertEqual(aenv["OIKONOME_POOL_MAX"], "${OIKONOME_POOL_MAX:-}")
        example = (root / "docker" / ".env.example").read_text()
        for knob in ("OIKONOME_WORKER_POOL_MAX",
                     "OIKONOME_WORKER_CONTROL_POOL_MAX",
                     "OIKONOME_FIRST_SYNC_CONCURRENCY"):
            self.assertIn(knob, example)

    def test_first_sync_concurrency_follows_the_environment(self):
        with mock.patch.dict(os.environ,
                             {"OIKONOME_FIRST_SYNC_CONCURRENCY": ""}):
            self.assertEqual(worker._first_sync_concurrency(), 3)
        with mock.patch.dict(os.environ,
                             {"OIKONOME_FIRST_SYNC_CONCURRENCY": "0"}):
            self.assertEqual(worker._first_sync_concurrency(), 1)
        with mock.patch.dict(os.environ,
                             {"OIKONOME_FIRST_SYNC_CONCURRENCY": "x"}), \
                self.assertLogs("oikonome.jobs", "WARNING"):
            self.assertEqual(worker._first_sync_concurrency(), 3)

    def test_threads_follow_the_environment(self):
        with mock.patch.dict(os.environ, {"OIKONOME_WORKER_THREADS": "20"}):
            self.assertEqual(worker._worker_threads(), 20)
        with mock.patch.dict(os.environ, {"OIKONOME_WORKER_THREADS": "1"}):
            self.assertEqual(worker._worker_threads(), 2)
        with mock.patch.dict(os.environ, {"OIKONOME_WORKER_THREADS": "x",
                                          "OIKONOME_POOL_MAX": ""}), \
                self.assertLogs("oikonome.jobs", "WARNING"):
            self.assertEqual(worker._worker_threads(), 12)

    def test_startup_sizes_the_loop_executor(self):
        async def run():
            worker._size_executor()
            ex = asyncio.get_running_loop()._default_executor
            return ex._max_workers
        self.assertEqual(asyncio.run(run()), worker.WORKER_THREADS)

    def test_bank_pulls_get_a_timeout_sized_to_the_work(self):
        fns = _by_name(worker.WorkerSettings)
        for name in ("sync_item", "sync_one"):
            self.assertEqual(fns[name].timeout_s, worker.SYNC_TIMEOUT, name)
        self.assertGreaterEqual(worker.SYNC_TIMEOUT, 30 * 60)


if __name__ == "__main__":
    unittest.main()
