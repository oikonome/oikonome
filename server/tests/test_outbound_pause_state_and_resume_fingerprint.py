"""The outbound pause names everything a hijacker could have repointed, and
Resume lifts only the record the owner actually saw.

- The household's own Plaid or MX client receives the bank login of every
  new link, so a rotation holds new links on it beside the relay and the
  AI endpoint. That hold is a real one (the link doors refuse while it
  stands), so the household's keys alone arm a record and a Resume; once
  the keys are gone there is nothing left to hold and no record.
- Resume carries the fingerprint of the record the banner showed; a record
  widened since (a restore, a later recovery) is not lifted by a click on
  the older banner.
- A non-owner reading Settings learns how many members a recovery removed,
  never who.
- A stored relay port that is not a port number never makes resolving the
  relay raise — every send goes through it.
"""
import os
import unittest
from unittest import mock

from oikonome.notify import outbound_pause as op


class PauseRecord(unittest.TestCase):

    def test_byo_aggregator_keys_are_named_by_a_rotation(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""}):
            what = op._what_is_set({"mx_client_id": "attacker"}, "o@x.dev")
            self.assertTrue(what["aggregator"])
            self.assertTrue(op.paused(
                {"mx_client_id": "attacker",
                 op.KEY: {"at": "t", "llm": True, "aggregator": True}}
            )["aggregator"])

    def test_aggregator_keys_alone_hold_new_links(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""}):
            p = op.paused({"plaid_client_id": "5f2c9e1ab03d4c0012a7f3e1",
                           op.KEY: {"at": "t", "aggregator": True}})
        self.assertTrue(p["aggregator"])
        self.assertTrue(p["version"])

    def test_a_hold_with_no_keys_left_is_no_record(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""}):
            self.assertIsNone(
                op.paused({op.KEY: {"at": "t", "aggregator": True}}))

    def test_hosted_never_pauses_aggregator_keys_it_never_stores(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            what = op._what_is_set({"plaid_client_id": "x"}, "o@x.dev")
        self.assertFalse(what["aggregator"])

    def test_widening_the_record_changes_its_fingerprint(self):
        rec = {"at": "2026-10-01T00:00:00+00:00", "reason": "recovery",
               "smtp": False, "recipients": [], "llm": False,
               "removed_members": [{"email": "m@x.dev"}]}
        before = op.paused({op.KEY: rec})["version"]
        widened = {**rec, "smtp": True}            # `at` kept, as a restore does
        self.assertNotEqual(before, op.paused({op.KEY: widened})["version"])

    def _resume(self, cfg, version):
        from contextlib import contextmanager

        @contextmanager
        def txn(_conn):
            yield cfg
        with mock.patch("oikonome.engine.budget.config_txn", txn):
            return op.resume(object(), version)

    def test_resume_refuses_a_record_changed_since_the_banner(self):
        rec = {"at": "a", "smtp": False, "recipients": [], "llm": True}
        shown = op.paused({op.KEY: rec})["version"]
        cfg = {op.KEY: {**rec, "smtp": True}}
        with self.assertRaises(op.StaleConfirmation):
            self._resume(cfg, shown)
        self.assertIn(op.KEY, cfg)

    def test_resume_lifts_the_record_the_banner_showed(self):
        rec = {"at": "a", "smtp": True, "recipients": [], "llm": False}
        cfg = {op.KEY: dict(rec)}
        self.assertTrue(self._resume(cfg, op.paused(cfg)["version"]))
        self.assertNotIn(op.KEY, cfg)


class RemovedMembersAreTheOwnersToSee(unittest.TestCase):

    def _view(self, role):
        from oikonome.web import api
        view = {"outbound_paused": {
            "smtp": False, "llm": False, "recipients": [],
            "removed_members": [{"email": "a@x.dev"}, {"email": "b@x.dev"}]}}
        with mock.patch.object(api.demoguard, "people_hidden",
                               return_value=False):
            return api._hide_people(view, {"role": role})["outbound_paused"]

    def test_a_member_sees_only_the_count(self):
        p = self._view("member")
        self.assertNotIn("removed_members", p)
        self.assertEqual(p["removed_members_count"], 2)

    def test_the_owner_sees_who(self):
        p = self._view("owner")
        self.assertEqual(len(p["removed_members"]), 2)


class StoredPortNeverRaises(unittest.TestCase):

    def test_bad_ports_fall_back_to_587(self):
        from oikonome.web.report import _stored_port
        for raw in ("587/tcp", "", None, 0, 70000, -1, [1]):
            self.assertEqual(_stored_port(raw), 587, raw)
        self.assertEqual(_stored_port("2525"), 2525)
        self.assertEqual(_stored_port(465), 465)


if __name__ == "__main__":
    unittest.main()
