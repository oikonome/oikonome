"""Merchant LLM classification is single-flight per tenant.

Three doors reach it — the hourly/webhook sync and a file import's
background thread (both via categorize_new) and the nightly sweep (run) —
under advisory locks that never contend with one another. Without a lock
of its own, two overlapping doors each SELECT the same not-yet-classified
merchants and each POST the same batch to the tenant's LLM backend: the
same answer, paid for twice. The sync-side door must skip while a
classification is in flight (idempotent, pending-gated — the next pass
catches up); the nightly mop-up must wait its turn, never run beside it.
"""

import os
import threading
import time
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import llm_categorize

from .util import TEST_DB, make_db

_APP_DSN = os.environ.get(
    "OIKONOME_TEST_DSN",
    f"postgresql://oikonome_app:apppass@127.0.0.1:5433/{TEST_DB}")

_BACKEND = {"url": "http://llm.test", "model": "m", "api_key": "",
            "extra_body": "", "vision_model": "", "source": "env"}


class MerchantClassifySingleFlightTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        # a second session for the same tenant, standing in for the
        # concurrent door that holds the classification lock
        self.other = tenancy.tenant_connect(self.tid, _APP_DSN)
        self.addCleanup(self.other.close)
        self.addCleanup(self.conn.close)

    def _hold(self):
        self.other.execute(
            f"SELECT pg_advisory_lock({llm_categorize._MERCHANT_LOCK})")

    def _release(self):
        self.other.execute(
            f"SELECT pg_advisory_unlock({llm_categorize._MERCHANT_LOCK})")

    def _classifying(self, classify):
        """Every collaborator around the merchant-classify call, faked:
        one pending merchant, a configured backend, nothing else pending —
        so a test observes exactly one thing, whether _classify_merchants
        ran."""
        from contextlib import ExitStack
        stack = ExitStack()
        for p in (
                mock.patch.object(llm_categorize, "_classify_merchants",
                                  side_effect=classify),
                mock.patch.object(llm_categorize, "pending_merchants",
                                  return_value=[{"merchant": "x"}]),
                mock.patch.object(llm_categorize, "_backend",
                                  return_value=dict(_BACKEND)),
                mock.patch.object(llm_categorize, "model_pass",
                                  return_value=0),
                mock.patch.object(llm_categorize, "amazon_pending",
                                  return_value=[]),
                mock.patch.object(llm_categorize, "summaries_pending",
                                  return_value=[]),
                mock.patch.object(llm_categorize, "proposals_pending_tags",
                                  return_value=[])):
            stack.enter_context(p)
        return stack

    def test_the_sync_door_skips_while_a_classification_is_in_flight(self):
        calls = []
        self._hold()
        try:
            with self._classifying(lambda conn, **kw: calls.append("hit")):
                llm_categorize.categorize_new(self.conn)
            self.assertEqual(calls, [], "the sync door must not bill the "
                             "LLM for merchants another door is already "
                             "classifying")
        finally:
            self._release()
        # with the lock free, the same call classifies — the skip above was
        # the lock, not a dead code path
        with self._classifying(lambda conn, **kw: calls.append("hit")):
            llm_categorize.categorize_new(self.conn)
        self.assertEqual(calls, ["hit"])

    def test_the_nightly_door_waits_instead_of_running_beside(self):
        classified = threading.Event()
        started = threading.Event()

        def nightly():
            with self._classifying(lambda conn, **kw: classified.set()):
                started.set()
                llm_categorize.run(self.conn)

        self._hold()
        try:
            t = threading.Thread(target=nightly, daemon=True)
            t.start()
            self.assertTrue(started.wait(5))
            time.sleep(0.5)
            self.assertFalse(
                classified.is_set(),
                "run() must wait for the in-flight classification, not "
                "classify the same merchants beside it")
        finally:
            self._release()
        t.join(10)
        self.assertTrue(classified.is_set(),
                        "run() must classify once the lock frees")


if __name__ == "__main__":
    unittest.main()
