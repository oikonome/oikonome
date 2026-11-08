"""Bill detection is single-flight per household.

The detection pass reads the whole multi-year ledger and holds one pooled
connection for the length of it. Reached from the "Find bills & income"
doors and from the nightly with nothing coalescing them, N parallel
requests ran N identical passes and drained the pool every other household
reads through. One per-household try-lock now guards every entry: a second
on-demand request is refused (the doors answer 409) without doing the pass,
and the nightly waits for the pass in flight and then runs its own.
"""

import threading
import time
import unittest
from unittest import mock

from fastapi import HTTPException

from oikonome.db import tenancy
from oikonome.engine import bills
from oikonome.jobs import worker
from oikonome.web import pages

from .util import make_db


class BillDetectionSingleFlightTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()
        cls.tid = cls.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        cls.key = f"oikonome:bills-detect:{cls.tid}"

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _hold(self):
        c = tenancy.tenant_connect(self.tid)
        got = c.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                        (self.key,)).fetchone()["ok"]
        self.assertTrue(got, "the test could not take the lock itself")
        return c

    def _release(self, c):
        c.execute("SELECT pg_advisory_unlock(hashtext(%s))", (self.key,))
        c.close()

    def test_a_second_run_is_refused_without_doing_the_pass(self):
        held = self._hold()
        try:
            with mock.patch.object(bills, "run") as detect:
                out = bills.run_single_flight(self.conn)
                detect.assert_not_called()
            self.assertIsNone(out)
        finally:
            self._release(held)

    def test_the_lock_is_released_for_the_next_run(self):
        """tenant_connect is pooled: close() would hand the session back
        with the lock still on it, so the unlock is explicit."""
        with mock.patch.object(bills, "run",
                               return_value={"proposed_add": 0}) as detect:
            self.assertEqual(bills.run_single_flight(self.conn),
                             {"proposed_add": 0})
            self.assertEqual(bills.run_single_flight(self.conn),
                             {"proposed_add": 0})
            self.assertEqual(detect.call_count, 2)
        c = tenancy.tenant_connect(self.tid)
        try:
            self.assertTrue(c.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (self.key,)).fetchone()["ok"])
        finally:
            self._release(c)

    def test_a_failing_pass_still_releases_the_lock(self):
        with mock.patch.object(bills, "run", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                bills.run_single_flight(self.conn)
        with mock.patch.object(bills, "run", return_value={}) as detect:
            bills.run_single_flight(self.conn)
            detect.assert_called_once()

    def test_another_households_lock_does_not_block(self):
        other = tenancy.tenant_connect(self.tid)
        k = f"oikonome:bills-detect:{'0' * 8}-0000-0000-0000-000000000000"
        other.execute("SELECT pg_advisory_lock(hashtext(%s))", (k,))
        try:
            with mock.patch.object(bills, "run", return_value={}) as detect:
                self.assertEqual(bills.run_single_flight(self.conn), {})
                detect.assert_called_once()
        finally:
            other.execute("SELECT pg_advisory_unlock(hashtext(%s))", (k,))
            other.close()

    def test_the_nightly_waits_for_the_pass_in_flight_then_runs(self):
        """The nightly is the household's one guaranteed pass of the day:
        skipping it because an on-demand pass happened to be running would
        lose the night's detection after a late sync."""
        held = self._hold()
        threading.Timer(0.5, self._release, (held,)).start()
        started = time.monotonic()
        with mock.patch.object(worker.bills, "run",
                               return_value={"proposed_add": 0}) as detect, \
             mock.patch("oikonome.engine.amazon_match.run_match"), \
             mock.patch("oikonome.engine.merchant_dedup.apply"), \
             mock.patch("oikonome.engine.llm_categorize.run"):
            worker.nightly_tenant(self.tid)
            detect.assert_called_once()
        self.assertGreaterEqual(time.monotonic() - started, 0.4)

    def test_the_legacy_form_door_answers_409_while_a_pass_runs(self):
        held = self._hold()
        try:
            with mock.patch.object(bills, "run") as detect:
                with self.assertRaises(HTTPException) as cm:
                    pages.bills_detect(user={"tenant_id": self.tid})
                detect.assert_not_called()
            self.assertEqual(cm.exception.status_code, 409)
        finally:
            self._release(held)

    def test_the_legacy_form_door_is_rate_limited(self):
        route = next(r for r in pages.router.routes
                     if getattr(r, "path", "") == "/bills/detect")
        self.assertTrue(route.dependencies,
                        "the detection door carries no rate limit")


if __name__ == "__main__":
    unittest.main()
