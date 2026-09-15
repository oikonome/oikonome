"""Onboarding polish: the unreliable manual-balance flag (a
files-only install reads checking $0 and must nudge, not panic) and the
post-import categorize hook wiring."""

import datetime as dt
import unittest
from unittest import mock

from oikonome.web import report, todayview

from .util import add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 14)


def _only_manual_checking(conn, bal=0.0):
    """Strip the seeded aggregator fixtures; leave one manual checking the
    way bulk_import creates it (balance 0, item 'manual')."""
    conn.execute("DELETE FROM transactions")
    conn.execute("DELETE FROM accounts")
    conn.execute("DELETE FROM items")
    conn.execute(
        """INSERT INTO items (id, aggregator, institution_name, access_token)
           VALUES ('manual','manual','Manual',NULL)""")
    conn.execute(
        """INSERT INTO accounts (id, item_id, name, type, subtype,
                                 balance_current, updated_at)
           VALUES ('manual:checking','manual','Checking','depository',
                   'checking',%s,now())""", (bal,))


class BalanceUnreliableTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_unset_manual_balance_flags_unreliable(self):
        _only_manual_checking(self.conn)
        add_txn(self.conn, TODAY, 50.0, "KROGER", account="manual:checking")
        st = report.gather(self.conn, TODAY)
        self.assertTrue(st["runway"]["balance_unreliable"])
        # both surfaces share build_context — headroom must go quiet too
        self.assertIsNone(todayview.build_context(st)["day"]["headroom"])

    def test_set_balance_clears_flag(self):
        _only_manual_checking(self.conn, bal=1200.0)
        add_txn(self.conn, TODAY, 50.0, "KROGER", account="manual:checking")
        st = report.gather(self.conn, TODAY)
        self.assertFalse(st["runway"]["balance_unreliable"])
        self.assertIsNotNone(todayview.build_context(st)["day"]["headroom"])

    def test_no_transactions_no_flag(self):
        # empty manual account (user just added it) — nothing to nudge about
        _only_manual_checking(self.conn)
        st = report.gather(self.conn, TODAY)
        self.assertFalse(st["runway"]["balance_unreliable"])

    def test_aggregator_zero_balance_is_believed(self):
        # a live source reporting $0 is data, not an unset placeholder —
        # the seeded 'test' aggregator checking stays, dropped to $0
        self.conn.execute(
            "UPDATE accounts SET balance_current=0, balance_available=0 "
            "WHERE id='chk'")
        add_txn(self.conn, TODAY, 50.0, "KROGER", account="chk")
        st = report.gather(self.conn, TODAY)
        self.assertFalse(st["runway"]["balance_unreliable"])


class _InlineThread:
    """threading.Thread stand-in that runs the target synchronously."""

    def __init__(self, target=None, daemon=None):
        self._target = target

    def start(self):
        self._target()


class PostImportCategorizeTests(unittest.TestCase):
    def test_hook_runs_categorize_on_a_tenant_conn(self):
        from oikonome.web import api
        fake_conn = mock.MagicMock()
        with mock.patch.object(api.tenancy, "tenant_connect",
                               return_value=fake_conn) as tc, \
             mock.patch("oikonome.engine.llm_categorize.categorize_new") as cn, \
             mock.patch("threading.Thread", _InlineThread):
            api._categorize_after_import("00000000-0000-0000-0000-000000000001")
        tc.assert_called_once_with("00000000-0000-0000-0000-000000000001")
        cn.assert_called_once_with(fake_conn)
        fake_conn.close.assert_called_once()

    def test_hook_failure_is_swallowed(self):
        from oikonome.web import api
        with mock.patch.object(api.tenancy, "tenant_connect",
                               side_effect=RuntimeError("db down")), \
             mock.patch("threading.Thread", _InlineThread):
            api._categorize_after_import("00000000-0000-0000-0000-000000000001")


if __name__ == "__main__":
    unittest.main()


class SeedMechanicsFilterTests(unittest.TestCase):
    """The first-run budget seed races the categorize pass — uncategorized
    bank mechanics (card payments, transfers) must not count as spend, or a
    sparse-source instance seeds other_monthly at roughly twice reality."""

    def test_uncategorized_card_payment_excluded_from_seed(self):
        from oikonome.engine import budget
        conn = make_db()
        try:
            today = dt.date.today()
            m = (today.replace(day=1) - dt.timedelta(days=40)).replace(day=15)
            # real spend + an UNCATEGORIZED card payment (sparse source)
            add_txn(conn, m, 200.0, "KROGER", account="chk")
            conn.execute(
                "INSERT INTO transactions (id,account_id,date,amount,name,"
                "merchant_name,category_primary,pending,removed,raw) VALUES "
                "('mech-1','chk',%s,5000,'CHASE CREDIT CARD PAYMENT',"
                "'Chase Credit Card',NULL,0,0,'{}'::jsonb)", (m,))
            seed = budget._seed_config(conn)
            # the $5k payment must not inflate the seed
            self.assertLess(seed["other_monthly"], 1000)
        finally:
            conn.close()
