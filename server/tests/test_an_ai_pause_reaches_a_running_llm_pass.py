"""A recovery that pauses the household's AI endpoint stops transaction
text going to it from the next batch of a run already under way.

The nightly run (and the sync-time pass) resolves its backend once and
then spends minutes on batch after batch. A pause written in that time
must not wait for the run to end: every later batch goes to the
operator's own backend instead.
"""

import json
import os
import unittest
from unittest.mock import patch

import httpx

from oikonome.engine import budget, llm_categorize

from .test_llm_categorize import ENV, add_raw_txn
from .util import make_db


class PauseMidRunTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        cfg = budget.load_config(self.conn)
        cfg.update(llm_url="http://cfg.test:9090", llm_model="cfg-model")
        budget.save_config(self.conn, cfg)
        for i, name in enumerate(("Mystery Shop", "Other Place",
                                  "Third Stall")):
            add_raw_txn(self.conn, f"t{i}", "2025-07-01", 10 + i, name,
                        raw={})

    def tearDown(self):
        self.conn.close()

    def _pause(self):
        cfg = budget.load_config(self.conn)
        cfg["outbound_paused"] = {"at": "2025-07-02T00:00:00+00:00",
                                  "reason": "recovery", "smtp": False,
                                  "recipients": [], "llm": True}
        budget.save_config(self.conn, cfg)

    def _transport(self, hosts):
        def handler(request):
            hosts.append(request.url.host)
            if len(hosts) == 1:
                # the owner's recovery lands while the first batch is out
                self._pause()
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps({"1": "TRAVEL"})}}]})
        return httpx.MockTransport(handler)

    def test_the_nightly_run_switches_backend_at_the_next_batch(self):
        hosts = []
        with patch.dict(os.environ, ENV):
            llm_categorize.run(self.conn, batch_size=1,
                               transport=self._transport(hosts))
        self.assertGreaterEqual(len(hosts), 2, hosts)
        self.assertEqual(hosts[0], "cfg.test")
        self.assertEqual(set(hosts[1:]), {"llm.test"},
                         "no batch after the pause reached the paused "
                         "endpoint")

    def test_the_sync_pass_switches_backend_at_the_next_batch(self):
        hosts = []
        with patch.dict(os.environ, ENV):
            llm_categorize.categorize_new(self.conn, batch_size=1,
                                          transport=self._transport(hosts))
        self.assertGreaterEqual(len(hosts), 2, hosts)
        self.assertEqual(hosts[0], "cfg.test")
        self.assertEqual(set(hosts[1:]), {"llm.test"})

    def test_without_a_pause_the_tenant_endpoint_keeps_every_batch(self):
        hosts = []

        def handler(request):
            hosts.append(request.url.host)
            return httpx.Response(200, json={"choices": [{"message": {
                "content": json.dumps({"1": "TRAVEL"})}}]})
        with patch.dict(os.environ, ENV):
            llm_categorize.run(self.conn, batch_size=1,
                               transport=httpx.MockTransport(handler))
        self.assertTrue(hosts)
        self.assertEqual(set(hosts), {"cfg.test"})


if __name__ == "__main__":
    unittest.main()
