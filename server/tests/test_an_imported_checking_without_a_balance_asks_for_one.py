"""A checking account an import created without a balance is a balance
the household has not set yet, not a missing account — and never a $0 that
outranks a real one.

Every file import (Mint, QIF, OFX, the competitor exports) creates its
checking row through `ensure_split_account` with no balance. Read as "no
checking", a files-only household was told to link a checking account it
had just imported instead of to set today's balance. Sorted as $0, the
empty row beat a real overdrawn account in the runway's auto-pick and
every cash alarm went quiet.
"""

import datetime as dt
import unittest

from oikonome.engine import forecast
from oikonome.sync import base
from oikonome.web import report, todayview

from .util import add_txn, make_db, write_config

TODAY = dt.date.today()


class ImportedCheckingWithoutBalanceTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _files_only(self, prefix, name, **kw):
        self.conn.execute("DELETE FROM accounts")
        aid = base.ensure_split_account(self.conn, prefix, name, **kw)
        add_txn(self.conn, TODAY, 40.0, "KROGER", account=aid)
        return aid

    def _assert_asks_for_the_balance(self):
        st = report.gather(self.conn, TODAY)
        rw = st["runway"]
        self.assertFalse(rw["no_checking"])
        self.assertTrue(rw["balance_unreliable"])
        _s, plain, html = report.build(st)
        for text in (plain, html):
            self.assertNotIn("No checking account linked", text)
        self.assertIn("set today's checking balance", plain)
        self.assertIn("Set today's checking balance", html)
        self.assertFalse(forecast.build(self.conn, today=TODAY)["no_checking"])

    def test_a_mint_import_asks_for_todays_balance_not_for_an_account(self):
        self._files_only("mint", "Everyday Checking")
        self._assert_asks_for_the_balance()

    def test_an_ofx_import_asks_for_todays_balance_not_for_an_account(self):
        self._files_only("ofx", "Acct 1234", key="1234",
                         type_="depository", subtype="checking")
        self._assert_asks_for_the_balance()

    def test_an_empty_imported_row_does_not_hide_a_real_overdraft(self):
        self.conn.execute("UPDATE accounts SET balance_current=-80, "
                          "balance_available=-80 WHERE id='chk'")
        self.conn.execute("UPDATE accounts SET balance_current=1250 "
                          "WHERE id='card'")
        base.ensure_split_account(self.conn, "mint", "Old Checking")
        add_txn(self.conn, TODAY, 40.0, "KROGER")
        self.assertEqual(forecast._checking_start(self.conn, {}),
                         (-80, True, False))
        st = report.gather(self.conn, TODAY)
        self.assertEqual(st["runway"]["checking"], -80)
        self.assertFalse(st["runway"]["balance_unreliable"])
        ctx = todayview.build_context(st)
        self.assertEqual((ctx["cash"] or {}).get("state"), "critical")

    def test_no_checking_row_at_all_is_still_no_checking(self):
        self.conn.execute("DELETE FROM accounts WHERE id='chk'")
        self.assertEqual(forecast._checking_start(self.conn, {}),
                         (None, False, False))

    def _assert_no_alarm_and_asks(self):
        self.conn.execute("UPDATE accounts SET balance_current=1250 "
                          "WHERE id='card'")
        self.assertEqual(forecast._checking_start(
            self.conn, {"excluded_accounts": ["chk"]}
            if self._excluded else {}), (0.0, False, True))
        st = report.gather(self.conn, TODAY)
        rw = st["runway"]
        self.assertFalse(rw["no_checking"])
        self.assertTrue(rw["balance_unreliable"])
        ctx = todayview.build_context(st)
        self.assertNotEqual((ctx["cash"] or {}).get("state"), "critical")
        _s, plain, _html = report.build(st)
        self.assertNotIn("short $1,250 today", plain)
        self.assertIn("set today's checking balance", plain)

    def test_an_imported_checking_beside_a_linked_savings_asks_not_alarms(self):
        self._excluded = False
        self.conn.execute("DELETE FROM accounts WHERE id='chk'")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,balance_available) VALUES ('sav','it1',"
            "'Savings','depository','savings',9000,9000)")
        aid = base.ensure_split_account(self.conn, "mint", "Everyday Checking")
        add_txn(self.conn, TODAY, 40.0, "KROGER", account=aid)
        self._assert_no_alarm_and_asks()

    def test_an_imported_checking_beside_an_excluded_linked_one_asks_not_alarms(self):
        self._excluded = True
        write_config(self.conn, excluded_accounts=["chk"])
        aid = base.ensure_split_account(self.conn, "mint", "Old Checking")
        add_txn(self.conn, TODAY, 40.0, "KROGER", account=aid)
        self._assert_no_alarm_and_asks()

    def test_a_linked_checking_at_a_real_zero_still_alarms(self):
        self.conn.execute("UPDATE accounts SET balance_current=0, "
                          "balance_available=0 WHERE id='chk'")
        self.conn.execute("UPDATE accounts SET balance_current=1250 "
                          "WHERE id='card'")
        add_txn(self.conn, TODAY, 40.0, "KROGER")
        self.assertEqual(forecast._checking_start(self.conn, {}),
                         (0, True, False))
        st = report.gather(self.conn, TODAY)
        self.assertFalse(st["runway"]["balance_unreliable"])
        ctx = todayview.build_context(st)
        self.assertEqual((ctx["cash"] or {}).get("state"), "critical")


if __name__ == "__main__":
    unittest.main()
