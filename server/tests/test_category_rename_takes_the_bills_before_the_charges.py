"""Renaming a custom category locks the bills that stamp it before it
rewrites a single charge.

A bill can stamp a custom category on the charges it matches. Approving an
offer on that bill locks the bill and then stamps its charges. Renaming
the category rewrites every charge filed under the old name and then the
bills that stamp it. Taken in that order, a rename holding the charges
waits for a bill that an approval holds, while the approval waits for one
of those charges: a cycle Postgres breaks by killing one side, so either
the rename or the approval answers with an error. Offers, then bills, then
charges is the order every bill writer keeps; the rename takes its bills
first.
"""

import datetime as dt
import unittest

from oikonome.engine import bills, merchant_dedup
from oikonome.engine.compat import jsonb
from oikonome.web import data

from .lock_order_util import Gate, Side, deadlocks, deadlocks_since, \
    tenant_of, waits_on_a_lock
from .util import TODAY, add_bill, add_txn, make_db, write_config

LINE = "SQ *LANTERN BOOKS"
CUSTOM = "Book Club"


class CategoryRenameBillOrderTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.tid = tenant_of(self.conn)
        for back in (90, 60, 30):
            add_txn(self.conn, TODAY - dt.timedelta(days=back), 40.0, LINE,
                    merchant=None)
        merchant_dedup.apply(self.conn)
        display = self.conn.execute(
            f"SELECT DISTINCT {merchant_dedup.DISPLAY_MERCHANT} AS d"
            f"  FROM transactions t {merchant_dedup.MC_JOIN}"
            "  WHERE t.name = %s", (LINE,)).fetchone()["d"]
        add_bill(self.conn, display, 40.0)
        # the charges are filed under the custom name (a rule put them
        # there), and the bill stamps that same name on what it matches
        self.conn.execute("UPDATE transactions SET category_primary = %s"
                          " WHERE name = %s", (CUSTOM, LINE))
        self.conn.execute(
            "UPDATE bills SET raw = raw || %s::jsonb WHERE payee = %s",
            (jsonb({"txn_category": CUSTOM}), display))
        self.conn.execute(
            """INSERT INTO bill_proposals (id, kind, payee, amount, evidence,
                                           status, created_at)
               VALUES ('prop:lk-fee', 'attach', 'service fee', -2.0, %s,
                       'pending', now())""",
            (jsonb({"bill_payee": display, "tokens": "service fee",
                    "window_days": 3}),))

    def test_a_category_rename_meeting_a_bill_approval_waits_for_it(self):
        gate = Gate()

        class Paused:
            """The rename's connection, paused once the charges are
            rewritten and before the bills follow."""

            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def execute(self, sql, *a, **kw):
                out = self._conn.execute(sql, *a, **kw)
                if "UPDATE manual_categories SET link_prior" in sql:
                    gate._pause()
                return out

        renamer = Side(self.tid, lambda c: data.rename_category(
            Paused(c), CUSTOM, "Reading"))
        approver = Side(self.tid, lambda c: bills.apply_proposal(
            c, "prop:lk-fee", "approve"))
        before = deadlocks()
        renamer.start()
        self.assertTrue(gate.reached.wait(20), "the rename never started")
        approver.start()
        self.assertTrue(waits_on_a_lock(approver.pid),
                        "the approval never met the rename")
        gate.go.set()
        self.assertTrue(renamer.finish(), "the rename hung")
        self.assertTrue(approver.finish(), "the approval hung")
        self.assertIsNone(renamer.error, renamer.error)
        self.assertIsNone(approver.error, approver.error)
        self.assertNotIn("error", approver.result or {}, approver.result)
        self.assertEqual(deadlocks_since(before), 0,
                         "the category rename and the approval deadlocked")
        # and the bill follows the rename
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM bills WHERE raw->>'txn_category' = 'Reading'"
        ).fetchone())


if __name__ == "__main__":
    unittest.main()
