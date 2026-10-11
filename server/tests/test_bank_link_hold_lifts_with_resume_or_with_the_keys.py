"""The bank-link hold lifts when the owner confirms the keys are theirs, or
when the keys it names are gone.

- Resume, sent with the version the banner showed, lifts the hold.
- The version covers the keys the banner names, so keys saved after the
  banner loaded make an older banner's Resume stale instead of confirming
  a client the owner never saw.
- Clearing the keys lifts the hold by itself, and a record holding nothing
  else reads as no record.
- Keys the owner saves after the record was armed are not the keys it
  named, so they are not held; nor is anything held by a record armed
  with no keys to hold."""

import os
import unittest
from unittest import mock

from oikonome.engine import budget
from oikonome.notify import outbound_pause as op

from .bank_link_hold_fixtures import arm_hold, save_household_keys
from .util import make_db


class HoldLifts(unittest.TestCase):

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""})
        self._env.start()
        self.conn = make_db()
        save_household_keys(self.conn, mx=False)

    def tearDown(self):
        self.conn.close()
        self._env.stop()

    def _cfg(self):
        return budget.load_config(self.conn)

    def test_resume_with_the_shown_version_lifts_the_hold(self):
        arm_hold(self.conn)
        shown = op.paused(self._cfg())["version"]
        self.assertTrue(op.resume(self.conn, shown))
        self.assertFalse(op.links_held(self._cfg(), "plaid"))
        self.assertIsNone(op.paused(self._cfg()))

    def test_keys_saved_after_the_banner_loaded_make_its_resume_stale(self):
        # the AI endpoint is paused too, so the record outlives the swap
        arm_hold(self.conn, llm=True)
        shown = op.paused(self._cfg())["version"]
        with budget.config_txn(self.conn) as cfg:
            cfg["plaid_client_id"] = "6a3d0f2bc14e5d0023b8e4f2"
        with self.assertRaises(op.StaleConfirmation):
            op.resume(self.conn, shown)
        self.assertIsNotNone(op.paused(self._cfg()))

    def test_keys_saved_after_arming_are_not_held(self):
        arm_hold(self.conn, llm=True)
        with budget.config_txn(self.conn) as cfg:
            cfg["plaid_client_id"] = "6a3d0f2bc14e5d0023b8e4f2"
        cfg = self._cfg()
        self.assertFalse(op.links_held(cfg, "plaid"))
        self.assertFalse(op.paused(cfg)["aggregator"])

    def test_clearing_the_keys_lifts_the_hold_and_an_empty_record(self):
        arm_hold(self.conn)
        self.assertTrue(op.links_held(self._cfg(), "plaid"))
        with budget.config_txn(self.conn) as cfg:
            for k in ("plaid_client_id", "plaid_secret", "plaid_env"):
                cfg.pop(k, None)
        cfg = self._cfg()
        self.assertFalse(op.links_held(cfg, "plaid"))
        self.assertIsNone(op.paused(cfg))

    def test_a_record_armed_without_the_hold_holds_nothing(self):
        arm_hold(self.conn, aggregator=False, llm=True)
        cfg = self._cfg()
        self.assertFalse(op.links_held(cfg, "plaid"))
        self.assertNotIn("aggregator_keys", op.paused(cfg))


if __name__ == "__main__":
    unittest.main()
