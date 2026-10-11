"""A merchant-wide category write, and its undo, take the merchant-identity
lock before they touch a rule or a ledger row.

A rename (or a merge, or the nightly repair of split merchants) takes the
household identity lock, moves the merchant's rows, and only then locks
the category rules stored under the old name so they can follow it. A
category write that locked the rule first and the rows second would cross
it on exactly that pair: the write holds the rule and waits for a row the
rename moved, the rename holds the row and waits for the rule. Postgres
breaks the cycle by killing one side, so either the category change
answers with an error or the rename (and with it a whole night's repair)
rolls back. Taking the identity lock first makes the two take turns.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import bills, llm_categorize, merchant_dedup
from oikonome.web import data

from .lock_order_util import Gate, Side, deadlocks, deadlocks_since, \
    tenant_of, waits_on_a_lock
from .util import TODAY, add_txn, make_db, write_config

LINE = "SQ *LANTERN BOOKS"


class CategoryWriteLockOrderTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.tid = tenant_of(self.conn)
        for back in (90, 60, 30):
            add_txn(self.conn, TODAY - dt.timedelta(days=back), 40.0, LINE,
                    merchant=None)
        merchant_dedup.apply(self.conn)
        self.display = self.conn.execute(
            f"SELECT DISTINCT {merchant_dedup.DISPLAY_MERCHANT} AS d"
            f"  FROM transactions t {merchant_dedup.MC_JOIN}"
            "  WHERE t.name = %s", (LINE,)).fetchone()["d"]
        # a rule already stored under the name, which the rename carries
        llm_categorize.upsert_user_rule(self.conn, self.display,
                                        "ENTERTAINMENT")
        llm_categorize.apply(self.conn)

    def _collide(self, write) -> None:
        """Run `write` while a rename of the same merchant holds its rows
        and has not yet reached the rules."""
        gate = Gate()

        def rename(conn):
            with conn.transaction():
                return merchant_dedup.rename(conn, self.display,
                                             "Lantern Bookshop")

        renamer = Side(self.tid, rename)
        writer = Side(self.tid, write)
        before = deadlocks()
        # paused right after the rows moved, before the rules follow
        with mock.patch.object(bills, "follow_merchant_rename",
                               gate.wrap(bills.follow_merchant_rename)):
            renamer.start()
            self.assertTrue(gate.reached.wait(20), "the rename never started")
            writer.start()
            self.assertTrue(waits_on_a_lock(writer.pid),
                            "the category write never met the rename")
            gate.go.set()
            self.assertTrue(renamer.finish(), "the rename hung")
            self.assertTrue(writer.finish(), "the category write hung")
        self.assertIsNone(renamer.error, renamer.error)
        self.assertIsNone(writer.error, writer.error)
        self.assertEqual(deadlocks_since(before), 0,
                         "the category write and the rename deadlocked")

    def test_a_category_write_meeting_a_rename_waits_for_it(self):
        self._collide(lambda c: data.set_merchant_category(
            c, self.display, "FOOD_AND_DRINK"))

    def test_a_category_undo_meeting_a_rename_waits_for_it(self):
        res = data.set_merchant_category(self.conn, self.display,
                                         "FOOD_AND_DRINK")
        self.assertTrue(res["undo"]["affected"])
        self._collide(lambda c: data.undo_merchant_category(c, res["undo"]))


if __name__ == "__main__":
    unittest.main()

    def test_re_enabling_a_rule_meeting_a_rename_waits_for_it(self):
        data.set_rule_disabled(self.conn, self.display, True)
        # rows the re-enabled rule will re-file
        self.conn.execute("UPDATE transactions SET category_primary="
                          "'GENERAL_MERCHANDISE' WHERE name=%s", (LINE,))
        self._collide(lambda c: data.set_rule_disabled(c, self.display, False))
