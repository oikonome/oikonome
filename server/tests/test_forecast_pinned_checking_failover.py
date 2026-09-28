"""A pinned primary checking account that has become a linked group's
SHADOW (its source went stale or errored, the healthy twin took over)
must not feed a frozen balance into the runway — the forecast follows the
same live failover every other money aggregate does.
"""

import unittest
from unittest import mock

from oikonome.engine import forecast, links

from .util import make_db, write_config


class PinnedCheckingFailoverTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_pinned_shadow_falls_through_to_the_live_pick(self):
        # a second checking account with a different balance
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,balance_available) VALUES "
            "('chk2','it1','Twin Checking','depository','checking',1234,1234)")
        cfg = {"checking_account_id": "chk"}
        self.assertEqual(forecast._checking_balance(self.conn, cfg), 5000)
        # production sets BOTH spellings of the shadow set on the tenant
        # connection (the Python helper and the app.shadow_ids session
        # variable the auto-pick SQL reads) — mirror that here
        self.conn.execute("SELECT set_config('app.shadow_ids', 'chk', false)")
        with mock.patch.object(links, "shadow_ids", return_value=["chk"]):
            # the pinned account is now the shadow: the auto-pick must
            # answer with the live twin, not the frozen 5000
            self.assertEqual(forecast._checking_balance(self.conn, cfg), 1234)


if __name__ == "__main__":
    unittest.main()
