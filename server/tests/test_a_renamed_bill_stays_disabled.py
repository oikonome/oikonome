"""Disabling a bill survives renaming it.

"Disabled" is one tap on the Bills page, and it means the schedule stops
watching this bill: no drift corrections, no standing "remove?" proposal.
The flag is stored by PAYEE STRING in the budget config, and renaming a
bill changes exactly that string — so the rename and the config entry have
to move together, and every reader has to see them move together.

Two ways that went wrong, and both are closed here:

  * the rename committed the row and left the config to a SECOND commit, so
    between the two the bill's name and the flag's name disagreed — a
    nightly pass landing in that gap watched a paused bill, and a budget
    drawn in it counted a bill the person had excluded;
  * the nightly pass read the rows and then re-read the config, two
    statements and so two snapshots, which reopens the same gap on the read
    side however atomic the write is.

The invariant either way: a bill that was disabled before a rename is still
disabled after it, and nothing acts on it in between.
"""

import datetime as dt
import unittest

from oikonome.engine import bills, budget

from .util import TODAY, add_bill, add_txn, make_db, write_config


class ARenameCarriesTheDisabledMark(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "Alderway Fiber", 80, next_due=TODAY)

    def tearDown(self):
        self.conn.close()

    def _disable(self, payee, cap=None):
        with budget.config_txn(self.conn) as cfg:
            cfg["disabled_bills"] = [payee]
            if cap is not None:
                cfg["occurrence_caps"] = {payee: cap}

    def test_the_rename_moves_the_flag_in_its_own_transaction(self):
        self._disable("Alderway Fiber", cap=2)
        self.assertEqual(
            1, bills.rename_bill(self.conn, "Alderway Fiber", "Alderway Net"))
        cfg = budget.load_config(self.conn)
        # no second save from the caller is needed for the pair to agree
        self.assertEqual(cfg["disabled_bills"], ["Alderway Net"])
        self.assertEqual(cfg["occurrence_caps"], {"Alderway Net": 2})

    def test_a_rename_that_is_refused_changes_no_config(self):
        add_bill(self.conn, "Alderway Net", 40, next_due=TODAY)
        self._disable("Alderway Fiber", cap=2)
        # the target name is taken: the rename does nothing at all
        self.assertEqual(
            0, bills.rename_bill(self.conn, "Alderway Fiber", "Alderway Net"))
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["disabled_bills"], ["Alderway Fiber"])
        self.assertEqual(cfg["occurrence_caps"], {"Alderway Fiber": 2})

    def test_an_enabled_bill_does_not_become_disabled_by_being_renamed(self):
        self._disable("Sumridge Water")          # a DIFFERENT bill's flag
        self.assertEqual(
            1, bills.rename_bill(self.conn, "Alderway Fiber", "Sumridge Water"))
        # the renamed bill now carries a name that is on the list, which is
        # what the person's own list says — but nothing else moved onto it
        cfg = budget.load_config(self.conn)
        self.assertEqual(cfg["disabled_bills"], ["Sumridge Water"])


class TheNightlyPassSkipsADisabledBillMidRename(unittest.TestCase):
    """The read side of the same window.

    The pass reads the bill rows, then acts on them. If it re-reads the
    disabled list separately, a rename committing in between hands it a row
    under the old name and a list under the new one, and the bill is watched
    for a night as though it had never been disabled. The flag therefore
    travels WITH the row, out of the row's own statement.
    """

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # a bill whose charges have drifted: left unguarded, the pass would
        # rewrite its amount and leave an `auto` audit row
        add_bill(self.conn, "Alderway Fiber", 80, next_due=TODAY,
                 last_seen=TODAY - dt.timedelta(days=150))
        for i, when in enumerate((90, 60, 30)):
            add_txn(self.conn, TODAY - dt.timedelta(days=when), 95.0,
                    "ALDERWAY FIBER", txn_id=f"af{i}")
        with budget.config_txn(self.conn) as cfg:
            cfg["disabled_bills"] = ["Alderway Fiber"]

    def tearDown(self):
        self.conn.close()

    def _bill(self, payee):
        return self.conn.execute(
            "SELECT payee, amount FROM bills WHERE payee=%s",
            (payee,)).fetchone()

    def test_the_bill_is_left_alone_when_it_is_disabled(self):
        bills.run(self.conn, TODAY)
        self.assertEqual(abs(self._bill("Alderway Fiber")["amount"]), 80)
        self.assertEqual([], self.conn.execute(
            "SELECT id FROM bill_proposals").fetchall())

    def test_a_rename_landing_mid_pass_does_not_un_disable_it(self):
        # the rename commits AFTER the pass has read its rows and BEFORE it
        # decides what to do with each one — the gap the finding names
        original = bills._advance_due_dates

        def rename_mid_pass(conn, today, stats):
            original(conn, today, stats)
            bills.rename_bill(conn, "Alderway Fiber", "Alderway Net")

        bills._advance_due_dates = rename_mid_pass
        try:
            bills.run(self.conn, TODAY)
        finally:
            bills._advance_due_dates = original

        # the rename took, and the bill is still paused: same amount, and
        # no proposal of any kind was raised against it
        self.assertIsNone(self._bill("Alderway Fiber"))
        self.assertEqual(abs(self._bill("Alderway Net")["amount"]), 80)
        self.assertEqual([], self.conn.execute(
            "SELECT id, kind FROM bill_proposals").fetchall())
        # and the flag followed the name, so tomorrow's pass agrees
        self.assertEqual(budget.load_config(self.conn)["disabled_bills"],
                         ["Alderway Net"])


if __name__ == "__main__":
    unittest.main()
