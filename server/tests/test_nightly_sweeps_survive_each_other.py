"""The nightly job's sweeps must not take each other down.

One arq job runs them in order — detection, the net-worth snapshot, the
stale-collector alert, the zombie-Item reaper, the installed gate's own
sweeps, the never-confirmed freeze and the purge of lapsed deletions. A job
has ONE timeout and a cron job is never retried, so a detection pass that
runs long (a sick institution holding several Plaid calls to their client
timeout, a big model backlog) used to cancel the job at the first await
after the deadline: every later sweep was skipped, for every tenant, every
night the slowness held. Snapshots got holes, dead Items kept billing,
freezes and reminders never went out and lapsed deletions were never
erased. A sweep that RAISES (the gate's, say) did the same to the ones
behind it.

Pinned here: each step has its own time budget and its own failure guard,
the order still holds, and the job's own timeout covers the budgets so
arq never cancels the run from outside.
"""

import asyncio
import dataclasses
import datetime as dt
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from oikonome.jobs import worker


class NightlyStepIsolationTests(unittest.TestCase):

    def _run(self, **overrides):
        """nightly_all with every sweep stubbed; `overrides` replaces a
        step by its label. Returns (result, the labels that ran)."""
        ran: list[str] = []

        def sweep(fn, label):
            ran.append(label)
            if label in overrides:
                return overrides[label]()
            return {}

        def gate_nightly():
            ran.append("gate")
            if "gate" in overrides:
                return overrides["gate"]()
            return {"frozen": []}

        def unverified_sweep():
            ran.append("unverified")
            return {"reminded": [], "scheduled": [], "held": []}

        def purge():
            ran.append("purge")
            return []

        from oikonome.jobs import unverified
        with mock.patch.object(worker, "_sweep", side_effect=sweep), \
             mock.patch.object(worker.ext.gate, "nightly",
                               side_effect=gate_nightly), \
             mock.patch.object(unverified, "sweep",
                               side_effect=unverified_sweep), \
             mock.patch.object(worker, "purge_scheduled_deletions",
                               side_effect=purge):
            out = asyncio.run(worker.nightly_all({}))
        return out, ran

    def test_a_slow_detection_pass_does_not_cancel_the_purge(self):
        def slow():
            time.sleep(0.5)
            return {}
        with mock.patch.dict(worker.NIGHTLY_STEP_BUDGET, {"nightly": 0.05}):
            out, ran = self._run(nightly=slow)
        self.assertEqual(ran, ["nightly", "networth-snapshot",
                               "script-alerts", "plaid-reap", "gate",
                               "unverified", "purge"])
        self.assertEqual(out["detect"], "timeout")
        self.assertEqual(out["purged"], [])

    def test_a_failing_gate_sweep_does_not_skip_the_freeze_or_the_purge(self):
        def boom():
            raise RuntimeError("billing service down")
        out, ran = self._run(gate=boom)
        self.assertIn("unverified", ran)
        self.assertIn("purge", ran)
        self.assertEqual(out["frozen"], "error:RuntimeError")

    def test_a_failing_sweep_does_not_skip_the_ones_after_it(self):
        def boom():
            raise RuntimeError("tenant list unavailable")
        out, ran = self._run(**{"networth-snapshot": boom})
        self.assertEqual(ran[-4:], ["plaid-reap", "gate", "unverified",
                                    "purge"])
        self.assertEqual(out["snapshot"], "error:RuntimeError")

    def test_the_job_timeout_covers_every_step_budget(self):
        """arq cancels a job at its timeout no matter what the steps
        inside think their budgets are. The cron entry and the plain
        function (the console's run-now button enqueues it by name) both
        carry a timeout at least the sum of the budgets."""
        total = sum(worker.NIGHTLY_STEP_BUDGET.values())
        job = next(j for j in worker.WorkerSettings.cron_jobs
                   if j.coroutine is worker.nightly_all)
        self.assertGreaterEqual(job.timeout_s, total)
        fn = next(f for f in worker.WorkerSettings.functions
                  if getattr(f, "coroutine", None) is worker.nightly_all)
        self.assertGreaterEqual(fn.timeout_s, total)


class CronClockTests(unittest.TestCase):
    """The nightly runs in the instance's zone (what OIKONOME_TZ is
    documented to do) and the maintenance reboot at 09:37 UTC (what
    OIKONOME_AUTO_REBOOT is documented to do). Left to itself arq reads
    every cron in a fixed offset captured at worker start: on a Pacific
    install the reboot landed at 09:37 in the morning, local, and every job
    slid an hour across a DST change until the worker restarted."""

    def test_crons_follow_the_instance_zone_by_name(self):
        with mock.patch.dict("os.environ",
                             {"OIKONOME_TZ": "America/Los_Angeles"}):
            tz = worker._cron_zone()
        self.assertEqual(tz.utcoffset(dt.datetime(2026, 1, 15, 12)),
                         dt.timedelta(hours=-8))
        self.assertEqual(tz.utcoffset(dt.datetime(2026, 7, 15, 12)),
                         dt.timedelta(hours=-7),
                         "a captured fixed offset would not follow DST")
        self.assertIsNotNone(getattr(worker.WorkerSettings, "timezone", None))

    def _reboot_at(self, when_utc):
        calls = []
        with mock.patch.object(worker, "_utcnow", return_value=when_utc), \
             mock.patch.object(worker, "_nightly_reboot_check",
                               side_effect=lambda: calls.append(1) or "ran"):
            out = asyncio.run(worker.nightly_reboot({}))
        return out, calls

    def test_the_maintenance_reboot_runs_at_0937_utc_only(self):
        utc = dt.timezone.utc
        out, calls = self._reboot_at(dt.datetime(2026, 7, 1, 9, 37, tzinfo=utc))
        self.assertEqual((out, calls), ("ran", [1]))
        # 09:37 on a Pacific wall clock is 16:37 UTC — not the window
        out, calls = self._reboot_at(dt.datetime(2026, 7, 1, 16, 37,
                                                 tzinfo=utc))
        self.assertEqual(calls, [])
        job = next(j for j in worker.WorkerSettings.cron_jobs
                   if j.coroutine is worker.nightly_reboot)
        self.assertIsNone(job.hour, "the cron must fire hourly so the "
                                    "UTC hour is reachable from any zone")
        self.assertEqual(job.minute, 37)
        email = next(j for j in worker.WorkerSettings.cron_jobs
                     if j.coroutine is worker.email_all)
        # the host goes down about a minute after the flag; that minute
        # must not be the one the hourly email sweep starts in
        self.assertGreater(abs(job.minute - email.minute), 2)

    def test_the_reboot_waits_for_an_email_sweep_in_flight(self):
        """A send cut between a household's mail and the stamp recording
        it goes out again, SMS and push included, after the restart."""
        import threading
        release = threading.Event()
        utc = dt.timezone.utc
        at_nine = dt.datetime(2026, 7, 1, 9, 37, tzinfo=utc)
        calls = []

        def sweep(fn, label):
            release.wait(5)
            return {}

        async def scenario():
            with mock.patch.object(worker, "_sweep", side_effect=sweep), \
                 mock.patch.object(worker, "_utcnow", return_value=at_nine), \
                 mock.patch.object(worker, "_nightly_reboot_check",
                                   side_effect=lambda: calls.append(1)
                                   or "ran"):
                task = asyncio.ensure_future(worker.email_all({}))
                for _ in range(500):
                    if worker._email_threads_running():
                        break
                    await asyncio.sleep(0.01)
                during = await worker.nightly_reboot({})
                release.set()
                await task
                after = await worker.nightly_reboot({})
                return during, after

        try:
            during, after = asyncio.run(scenario())
        finally:
            release.set()
        self.assertEqual(during, "email-sending")
        self.assertEqual(after, "ran")
        self.assertEqual(calls, [1])

    def test_the_reboot_waits_for_a_nightly_in_flight(self):
        """The nightly's hour is local and the reboot's is UTC, so in some
        zones they meet; the reboot must not cut the nightly short."""
        utc = dt.timezone.utc
        with mock.patch.object(worker, "_NIGHTLY_RUNS", 1):
            out, calls = self._reboot_at(
                dt.datetime(2026, 7, 1, 9, 37, tzinfo=utc))
        self.assertEqual((out, calls), ("nightly-running", []))

    def test_the_reboot_waits_for_a_step_still_running_past_its_budget(self):
        """A step that overran its budget keeps running in its thread after
        the run has moved on and returned; the reboot must wait for that
        thread too, then go ahead once it finishes. One event loop, as in
        the worker: asyncio.run would join the thread on exit."""
        import threading
        release = threading.Event()
        utc = dt.timezone.utc
        at_nine = dt.datetime(2026, 7, 1, 9, 37, tzinfo=utc)
        calls = []

        def sweep(fn, label):
            if label == "nightly":
                release.wait(5)
            return {}

        async def scenario():
            from oikonome.jobs import unverified
            with mock.patch.dict(worker.NIGHTLY_STEP_BUDGET,
                                 {"nightly": 0.05}), \
                 mock.patch.object(worker, "_sweep", side_effect=sweep), \
                 mock.patch.object(worker.ext.gate, "nightly",
                                   return_value={"frozen": []}), \
                 mock.patch.object(unverified, "sweep", return_value={
                     "reminded": [], "scheduled": [], "held": []}), \
                 mock.patch.object(worker, "purge_scheduled_deletions",
                                   return_value=[]), \
                 mock.patch.object(worker, "_utcnow", return_value=at_nine), \
                 mock.patch.object(worker, "_nightly_reboot_check",
                                   side_effect=lambda: calls.append(1)
                                   or "ran"):
                out = await worker.nightly_all({})
                self.assertEqual(out["detect"], "timeout")
                self.assertEqual(worker._NIGHTLY_RUNS, 0)
                during = await worker.nightly_reboot({})
                release.set()
                for _ in range(500):
                    if worker._step_threads_running() == 0:
                        break
                    await asyncio.sleep(0.01)
                after = await worker.nightly_reboot({})
                return during, after

        try:
            during, after = asyncio.run(scenario())
        finally:
            release.set()
        self.assertEqual(during, "nightly-running")
        self.assertEqual(after, "ran")
        self.assertEqual(calls, [1])

class CronAcrossDstTests(unittest.TestCase):
    """The cron clock must neither stall nor double across a DST change.
    Driven the way arq's heartbeat drives it: "now" in the worker's
    timezone, a job fires when its next_run is under now + the window,
    then its next run is computed from the one just fired."""

    ZONE = "America/Los_Angeles"

    def _fires(self, start_utc, hours, step_s=30):
        import importlib
        with mock.patch.dict("os.environ", {"OIKONOME_TZ": self.ZONE}):
            importlib.reload(worker)
            try:
                tz = worker.WorkerSettings.timezone
                jobs = [dataclasses.replace(j)
                        for j in worker.WorkerSettings.cron_jobs]
            finally:
                importlib.reload(worker)
        fired = {j.name: [] for j in jobs}
        n = start_utc.astimezone(tz)
        end = n + dt.timedelta(hours=hours)
        window = dt.timedelta(seconds=1)
        while n < end:
            for j in jobs:
                if j.next_run is None:
                    j.calculate_next(n)
                    continue
                if j.next_run < n + window:
                    fired[j.name].append(
                        j.next_run.astimezone(dt.timezone.utc))
                    j.calculate_next(j.next_run)
            n += dt.timedelta(seconds=step_s)
        return fired

    def _max_gap(self, times):
        return max((b - a).total_seconds() for a, b in zip(times, times[1:]))

    def test_fall_back_neither_stalls_nor_doubles(self):
        utc = dt.timezone.utc
        # 2026-11-01: 01:00-01:59 happens twice in Los Angeles
        fired = self._fires(dt.datetime(2026, 11, 1, 7, 0, tzinfo=utc), 5)
        sync = fired["cron:sync_all"]
        self.assertLessEqual(self._max_gap(sync), 300 + 1, "sync stalled")
        self.assertEqual(len(sync), len(set(sync)))
        self.assertLessEqual(self._max_gap(fired["cron:webhooks_all"]), 61)
        # the second 01:37 is 09:37 UTC — the maintenance window
        self.assertIn(dt.datetime(2026, 11, 1, 9, 37), [
            t.replace(tzinfo=None, second=0, microsecond=0)
            for t in fired["cron:nightly_reboot"]])

    def test_hour_pinned_jobs_run_once_a_day_across_both_changes(self):
        utc = dt.timezone.utc
        for start in (dt.datetime(2026, 10, 31, 0, 0, tzinfo=utc),
                      dt.datetime(2026, 3, 7, 0, 0, tzinfo=utc)):
            fired = self._fires(start, 72, step_s=60)
            local = [t.astimezone(ZoneInfo(self.ZONE))
                     for t in fired["cron:nightly_all"]]
            self.assertEqual(len(local), 3, (start, local))
            self.assertEqual({(t.hour, t.minute) for t in local}, {(4, 17)})
            self.assertEqual(len({t.date() for t in local}), 3)

    def test_spring_forward_keeps_interval_jobs_running(self):
        utc = dt.timezone.utc
        # 2026-03-08: 02:00-02:59 does not exist in Los Angeles
        fired = self._fires(dt.datetime(2026, 3, 8, 8, 0, tzinfo=utc), 4)
        self.assertLessEqual(self._max_gap(fired["cron:sync_all"]), 301)


if __name__ == "__main__":
    unittest.main()


class RebootMinuteIsDocumentedTests(unittest.TestCase):
    """The reboot's minute is read on the instance's clock (so it always
    stays 32 minutes clear of the email sweep) and its hour in UTC. In a
    half-hour zone that is 09:07 UTC, not 09:37 — the operator-facing
    setting has to say so, or a planned window is off by half an hour."""

    ZONE = "Asia/Kolkata"
    _fires = CronAcrossDstTests._fires

    def test_a_half_hour_zone_reboots_at_0907_utc_and_the_docs_say_so(self):
        import pathlib
        utc = dt.timezone.utc
        fired = self._fires(dt.datetime(2026, 7, 1, 8, 0, tzinfo=utc), 2,
                            step_s=60)
        in_window = [t for t in fired["cron:nightly_reboot"]
                     if t.astimezone(utc).hour == 9]
        self.assertEqual([(t.hour, t.minute) for t in
                          (x.astimezone(utc) for x in in_window)], [(9, 7)])
        env = (pathlib.Path(__file__).resolve().parents[2]
               / "docker" / ".env.example").read_text()
        block = env[env.index("# Automated maintenance reboot"):
                    env.index("# OIKONOME_AUTO_REBOOT=1")]
        self.assertIn("09:07 UTC in half-hour", block)
        self.assertNotIn("checked daily at 09:37 UTC", block)
