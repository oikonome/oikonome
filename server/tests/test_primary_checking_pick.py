"""The ledger heads itself with the primary checking balance, so the
accounts list must name the account the cash forecast actually starts
from: the pin while it is live, else the fall-through pick (largest
balance), and a balance-less checking row only when nothing better
exists — never an account the forecast itself would ignore.
"""

import unittest

from oikonome.engine import forecast

from .util import make_db, write_config


class PrimaryCheckingPickTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,balance_available) VALUES "
            "('chk2','it1','Small Checking','depository','checking',120,120)")

    def tearDown(self):
        self.conn.close()

    def test_live_pin_wins_whatever_its_balance(self):
        acct, bal, _avail, unset = forecast.checking_pick(
            self.conn, {"checking_account_id": "chk2"})
        self.assertEqual((acct, bal, unset), ("chk2", 120, False))

    def test_no_pin_falls_through_to_the_largest_balance(self):
        acct, bal, _avail, unset = forecast.checking_pick(self.conn, {})
        self.assertEqual((acct, bal, unset), ("chk", 5000, False))

    def test_balance_and_id_agree_with_the_runway_start(self):
        # the balance the ledger shows must be the one the runway walks from
        for cfg in ({}, {"checking_account_id": "chk2"}):
            self.assertEqual(forecast.checking_pick(self.conn, cfg)[1:],
                             forecast._checking_start(self.conn, cfg))

    def test_unset_balance_names_the_row_but_flags_it(self):
        self.conn.execute(
            "UPDATE accounts SET balance_current=NULL, balance_available=NULL"
            " WHERE type='depository'")
        acct, bal, _avail, unset = forecast.checking_pick(self.conn, {})
        self.assertIn(acct, ("chk", "chk2"))
        self.assertEqual((bal, unset), (0.0, True))

    def test_a_live_pin_with_no_balance_hands_over_to_the_pick(self):
        """A pinned account nobody set a balance on anchors nothing: the
        runway falls through to the largest-balance checking, so the id the
        ledger heads itself with must be that one too, not the pin."""
        self.conn.execute(
            "UPDATE accounts SET balance_current=NULL, balance_available=NULL"
            " WHERE id='chk2'")
        cfg = {"checking_account_id": "chk2"}
        acct, bal, _avail, unset = forecast.checking_pick(self.conn, cfg)
        self.assertEqual((acct, bal, unset), ("chk", 5000, False))
        self.assertEqual((bal, _avail, unset),
                         forecast._checking_start(self.conn, cfg))

    def test_no_checking_account_is_no_pick(self):
        self.conn.execute("DELETE FROM accounts WHERE type='depository'")
        self.assertEqual(forecast.checking_pick(self.conn, {}),
                         (None, None, False, False))


if __name__ == "__main__":
    unittest.main()
