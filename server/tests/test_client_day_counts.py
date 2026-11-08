"""The day counts the clients print come from the server's own settings.

The Accounts screens say how long a failing connection lasts before it is
removed and how long a released bank keeps its place under the allowance.
Written into the clients, those sentences drift from the instance: an
operator who sets OIKONOME_PLAID_REAP_DAYS=14 saw "30 days" on both the web
and the phone while the daily email said 14. The numbers must be read from
the values the reaper and the allowance count actually use."""

import os
import unittest
from unittest import mock

from oikonome.jobs import reaper
from oikonome.sync import base


class ClientDayCountsTests(unittest.TestCase):
    def test_reap_days_follow_the_operator_setting(self):
        with mock.patch.dict(os.environ, {"OIKONOME_PLAID_REAP_DAYS": "14"}):
            self.assertEqual(reaper.client_day_counts()["plaid_reap_days"],
                             14)

    def test_default_reap_days_match_the_reaper(self):
        env = {k: v for k, v in os.environ.items()
               if k != "OIKONOME_PLAID_REAP_DAYS"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(reaper.client_day_counts()["plaid_reap_days"],
                             reaper.reap_days())

    def test_a_disabled_reaper_sends_no_number(self):
        # 0 turns the reaper off; a connection removed under an earlier
        # setting must not be described as removed "after 0 days"
        for v in ("0", "-3", "junk"):
            with mock.patch.dict(os.environ, {"OIKONOME_PLAID_REAP_DAYS": v}):
                self.assertIsNone(
                    reaper.client_day_counts()["plaid_reap_days"])

    def test_released_hold_is_the_allowance_counts_window(self):
        with mock.patch.object(base, "RELEASED_HOLD_DAYS", 45):
            self.assertEqual(
                reaper.client_day_counts()["released_hold_days"], 45)


if __name__ == "__main__":
    unittest.main()
