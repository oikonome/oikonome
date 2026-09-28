"""Classifying a SimpleFIN account re-signs its balance — and what that
does to the net-worth history already on disk.

SimpleFIN reports card debt as a NEGATIVE balance and the engine wants it
positive, so the sync flips the sign for accounts whose type is credit.
When the user corrects a type, `mark_type_user_set` re-signs the stored
balance in the same statement. The question these tests pin is what
happens to the nights that already ran under the wrong type:

  * The ONLY per-account balances this schema stores are the three columns
    on `accounts` — there is no per-account balance-history table for the
    re-sign to miss (asserted below, so that adding one forces whoever
    adds it to handle the crossing).
  * The recorded history is `networth_snapshot`, one aggregate row per
    night. Its `total` is immune to the correction: the stored sign and
    the reading convention flip TOGETHER (a card's balance is negated on
    the way in and negated again by the credit branch of the net-worth
    sum), so a night recorded under the wrong type recorded the right
    dollar total. Nothing to rewrite — proved below rather than assumed,
    because "the history is wrong" is the natural reading of the re-sign.
  * The one thing those nights got wrong is which asset CLASS the money
    sat in (Cash instead of Card debt). That is not recoverable: the row
    is a sum over every account, and no per-account contribution is kept.
    It is also never read back — only `total` feeds the trend — and the
    nightly snapshot files the corrected account correctly from the next
    run on, which is what the last test checks.
"""

import datetime as dt
import unittest

from oikonome.engine import reporting
from oikonome.sync import base as sync_base

from .util import make_db, write_config


class AccountTypeChangeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # a SimpleFIN card the name heuristic read as a checking account:
        # the debt is stored with the bank's own sign, unflipped
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name, "
            "access_token) VALUES "
            "('sfin:demo','simplefin-org','Demo Bank',NULL)")
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current,balance_available) VALUES "
            "('sfin-card','sfin:demo','Everyday Spending','depository',"
            "'checking',-250,-100)")

    def tearDown(self):
        self.conn.close()

    def _bal(self):
        r = self.conn.execute(
            "SELECT balance_current c, balance_available a FROM accounts "
            "WHERE id='sfin-card'").fetchone()
        return r["c"], r["a"]

    def test_crossing_into_credit_resigns_the_stored_balances(self):
        sync_base.mark_type_user_set(self.conn, "sfin-card", "credit",
                                     "credit card")
        self.assertEqual(self._bal(), (250.0, 100.0))

    def test_crossing_into_a_loan_resigns_the_stored_balance(self):
        """A loan is debt, and the engine stores debt positive (the
        property layer reads a loan balance as the amount owed and
        subtracts it). A mortgage left on SimpleFIN's own sign therefore
        lands as a positive property item the size of the debt — a $400k
        error on a $200k loan, and it appears the moment the user helpfully
        classifies the account."""
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('sfin-mort','sfin:demo','Home Mortgage','depository',"
            "'checking',-200000)")
        sync_base.mark_type_user_set(self.conn, "sfin-mort", "loan",
                                     "mortgage")
        self.assertEqual(self.conn.execute(
            "SELECT balance_current c FROM accounts WHERE id='sfin-mort'"
        ).fetchone()["c"], 200000.0)
        nw = reporting.compute_networth(self.conn)
        self.assertEqual([p["value"] for p in nw["property_items"]],
                         [-200000.0])
        # card and loan are both debt, so moving between them is not a
        # crossing and must not flip anything
        sync_base.mark_type_user_set(self.conn, "sfin-mort", "credit",
                                     "credit card")
        self.assertEqual(self.conn.execute(
            "SELECT balance_current c FROM accounts WHERE id='sfin-mort'"
        ).fetchone()["c"], 200000.0)

    def test_a_change_that_does_not_cross_credit_leaves_balances_alone(self):
        """Only the crossing re-signs. A subtype correction, or the same
        classification saved twice, must not negate the balance again —
        one extra flip is the whole debt reported as an asset."""
        sync_base.mark_type_user_set(self.conn, "sfin-card", "credit",
                                     "credit card")
        sync_base.mark_type_user_set(self.conn, "sfin-card", "credit",
                                     "credit card")
        self.assertEqual(self._bal(), (250.0, 100.0))
        sync_base.mark_type_user_set(self.conn, "sfin-card", "credit",
                                     "charge card")        # subtype only
        self.assertEqual(self._bal(), (250.0, 100.0))
        # and back across the boundary once, exactly once
        sync_base.mark_type_user_set(self.conn, "sfin-card", "depository",
                                     "checking")
        self.assertEqual(self._bal(), (-250.0, -100.0))
        sync_base.mark_type_user_set(self.conn, "sfin-card", "depository",
                                     "savings")
        self.assertEqual(self._bal(), (-250.0, -100.0))

    def test_recorded_networth_history_needs_no_rewrite(self):
        """A night recorded while the card read as checking holds the right
        dollar total, so the correction must not move the trend. If it
        did, the user would see a step the household never lived on the
        day they fixed a label."""
        reporting.snapshot_networth(self.conn)
        before = self.conn.execute(
            "SELECT date, total FROM networth_snapshot").fetchone()
        # pretend that night was a month ago: the row on disk from before
        # the classification, the one there is no way to recompute
        earlier = before["date"] - dt.timedelta(days=30)
        self.conn.execute(
            "INSERT INTO networth_snapshot (date, total, by_class) "
            "SELECT %s, total, by_class FROM networth_snapshot WHERE date=%s",
            (earlier, before["date"]))

        sync_base.mark_type_user_set(self.conn, "sfin-card", "credit",
                                     "credit card")

        # the live total, read through the corrected type, equals the
        # historical row read through the wrong one — the two negations
        # cancel, so the stored history is already right
        self.assertEqual(reporting.compute_networth(self.conn)["current_total"],
                         before["total"])
        reporting.snapshot_networth(self.conn)          # the next night
        series = dict(reporting._snapshot_series(self.conn))
        self.assertEqual(len(series), 2)
        self.assertEqual(len(set(series.values())), 1)   # no step in the line

    def test_the_nightly_snapshot_files_the_corrected_account_by_class(self):
        """The asset-class split of the nights already recorded cannot be
        repaired (the row is a sum over every account), but every snapshot
        after the correction must file the debt as card debt, not cash."""
        reporting.snapshot_networth(self.conn)
        sync_base.mark_type_user_set(self.conn, "sfin-card", "credit",
                                     "credit card")
        reporting.snapshot_networth(self.conn)
        by_class = self.conn.execute(
            "SELECT by_class FROM networth_snapshot").fetchone()["by_class"]
        # fixture card 250 + this one 250, both owed; checking untouched
        self.assertEqual(by_class["Card debt"], -500.0)
        self.assertEqual(by_class["Cash"], 5000.0)

    def test_no_per_account_balance_history_escapes_the_resign(self):
        """The re-sign corrects three columns on `accounts`, which is every
        place this schema keeps an account's balance. A new per-account
        balance-history table would silently keep the pre-correction sign
        for that account forever, so it has to be re-signed here too —
        this test fails the day one is added, which is the reminder."""
        holders = {(r["table_name"], r["column_name"]) for r in self.conn.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema='public' AND column_name LIKE '%balance%'")}
        self.assertEqual(holders, {("accounts", "balance_current"),
                                   ("accounts", "balance_available"),
                                   ("accounts", "balance_limit")})


if __name__ == "__main__":
    unittest.main()
