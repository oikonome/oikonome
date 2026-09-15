"""The nightly sweep is single-flight per tenant.

`nightly_tenant` is reachable from two doors — the nightly cron sweep and
the admin console's run-job enqueue (a fresh job per click) — with nothing
stopping two runs for the same tenant overlapping. Both read the same
savings_goal_state before either writes it back, so both observe the same
milestone crossing and each sends the savings-milestone email: the config
write dedupes state, not a send that already fired. Same per-tenant TRY-lock
shape as `sync_tenant` and `_email_if_due`: the second entrant skips, because
the run already in flight is doing this exact idempotent sweep.
"""

import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.jobs import worker

from .util import make_db


class NightlySingleFlightTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()
        cls.tid = cls.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _hold(self, key: str):
        c = tenancy.tenant_connect(self.tid)
        got = c.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                        (key,)).fetchone()["ok"]
        self.assertTrue(got, "the test could not take the lock itself")
        return c

    def _release(self, c, key: str):
        c.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
        c.close()

    def test_a_second_entrant_skips_the_whole_sweep(self):
        """While one run holds the lock, a concurrent run must not reach the
        milestone check — reaching it is what duplicates the email."""
        key = f"oikonome:nightly:{self.tid}"
        held = self._hold(key)
        try:
            with mock.patch.object(worker.bills, "run") as detect, \
                 mock.patch("oikonome.engine.savings.check_milestones") as ms:
                out = worker.nightly_tenant(self.tid)
                detect.assert_not_called()
                ms.assert_not_called()
                self.assertEqual(out, {"_status": "already-running"})
        finally:
            self._release(held, key)

    def test_another_tenants_lock_does_not_block(self):
        """The key carries the tenant id — an instance-wide key would
        serialise every tenant's nightly behind the first one."""
        other = f"oikonome:nightly:{'0' * 8}-0000-0000-0000-000000000000"
        held = self._hold(other)
        try:
            with mock.patch.object(worker.bills, "run",
                                   return_value={"proposed_add": 0}) as detect, \
                 mock.patch("oikonome.engine.amazon_match.run_match"), \
                 mock.patch("oikonome.engine.merchant_dedup.apply"), \
                 mock.patch("oikonome.engine.llm_categorize.run"):
                worker.nightly_tenant(self.tid)
                detect.assert_called_once()
        finally:
            self._release(held, other)

    def test_the_lock_is_released_for_the_next_run(self):
        """tenant_connect is POOLED: close() hands the session back with any
        advisory lock still on it, so the unlock must be explicit or every
        later nightly for this tenant is refused by a lock nobody holds."""
        with mock.patch.object(worker.bills, "run",
                               return_value={"proposed_add": 0}), \
             mock.patch("oikonome.engine.amazon_match.run_match"), \
             mock.patch("oikonome.engine.merchant_dedup.apply"), \
             mock.patch("oikonome.engine.llm_categorize.run"):
            worker.nightly_tenant(self.tid)
        probe = tenancy.tenant_connect(self.tid)
        key = f"oikonome:nightly:{self.tid}"
        try:
            free = probe.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (key,)).fetchone()["ok"]
            self.assertTrue(free, "the lock was not released")
        finally:
            probe.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
            probe.close()

    def test_the_key_is_built_from_the_tenant_id(self):
        import inspect
        src = inspect.getsource(worker.nightly_tenant)
        self.assertIn('f"oikonome:nightly:{tenant_id}"', src)


class NightlyStepIsolationTests(unittest.TestCase):
    """One broken step must cost only that step.

    Every step of the nightly sweep is isolated for a stated reason: a
    tenant-specific data defect in one of them must not take the rest of
    that household's night down with it. Recurring detection is the FIRST
    substantial step — unisolated, a defect there would skip the Plaid
    cross-check, the amazon/dedup/LLM chain, the receipt parse, the
    categorizer retrain and the savings-goal emails, every night, for as
    long as the defect stood.
    """

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()
        cls.tid = cls.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_a_failure_in_recurring_detection_still_runs_the_rest(self):
        with mock.patch.object(worker.bills, "run",
                               side_effect=ValueError("bad evidence blob")), \
             mock.patch("oikonome.engine.amazon_match.run_match") as amazon, \
             mock.patch("oikonome.engine.merchant_dedup.apply") as dedup, \
             mock.patch("oikonome.engine.llm_categorize.run") as llm, \
             mock.patch("oikonome.engine.receipts.parse_pending") as receipts, \
             mock.patch("oikonome.engine.model_train.maybe_train",
                        return_value={}) as retrain:
            out = worker.nightly_tenant(self.tid)
        amazon.assert_called_once()
        dedup.assert_called_once()
        llm.assert_called_once()
        receipts.assert_called_once()
        retrain.assert_called_once()
        # …and the sweep still answers with usable counters, so the
        # heartbeat and the caller are not the second casualty
        self.assertEqual(out["proposed_add"], 0)
        self.assertIn("error", out)
