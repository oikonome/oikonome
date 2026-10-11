"""Undoing a merchant rename, or running several renames in one
transaction, keeps the bill lock order: offers, then bills, then ledger
rows.

Approving an offer on a bill locks the bill and then stamps the bill's
category on the charges it matches. A rename locks the bills it will
re-point before it moves any charge, for exactly that reason. Undoing a
rename moves the charges back first and only then reaches for the bills
the rename had re-pointed; an approval of a fee on one of those bills,
landing between the two, holds the bill and waits for a charge the undo
moved while the undo waits for the bill. Postgres breaks the cycle by
killing one side, so the person sees an error on the undo or the
approval. The undo has to take those bills before the charges.

Several renames in one transaction (merging a chain into its brand, the
nightly repair of split merchants) look like the same hazard, since a
later rename locks its bills after an earlier one moved charges. They are
not: a bill stamps only the charges of the merchants it pays, and a rename
locks every bill paying the merchant whose charges it moves before it
moves them. So by the time a transaction holds a charge it already holds
every bill that could stamp it, and an approval of such a bill is waiting
on the bill, holding nothing. The second test keeps that true.
"""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import bills, merchant_dedup, merchant_identity
from oikonome.engine.compat import jsonb

from .lock_order_util import Gate, Side, deadlocks, deadlocks_since, \
    tenant_of, waits_on_a_lock
from .util import TODAY, add_bill, add_txn, make_db, write_config


def _display_of(conn, line: str) -> str:
    return conn.execute(
        f"SELECT DISTINCT {merchant_dedup.DISPLAY_MERCHANT} AS d"
        f"  FROM transactions t {merchant_dedup.MC_JOIN}"
        "  WHERE t.name = %s", (line,)).fetchone()["d"]


class _Collide(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.tid = tenant_of(self.conn)

    def lines(self, line: str, amount: float = 40.0) -> str:
        for back in (90, 60, 30):
            add_txn(self.conn, TODAY - dt.timedelta(days=back), amount,
                    line, merchant=None)
        merchant_dedup.apply(self.conn)
        return _display_of(self.conn, line)

    def fee_offer(self, bill_payee: str) -> str:
        """A bill that stamps its charges, and a pending offer to attach a
        fee to it: approving the offer re-stamps the bill's charges."""
        self.conn.execute(
            "UPDATE bills SET raw = raw || %s::jsonb WHERE payee = %s",
            (jsonb({"txn_category": "ENTERTAINMENT"}), bill_payee))
        self.conn.execute(
            """INSERT INTO bill_proposals (id, kind, payee, amount, evidence,
                                           status, created_at)
               VALUES ('prop:lk-fee', 'attach', 'service fee', -2.0, %s,
                       'pending', now())""",
            (jsonb({"bill_payee": bill_payee, "tokens": "service fee",
                    "window_days": 3}),))
        return "prop:lk-fee"

    def collide(self, first, gate: Gate, patch, second) -> None:
        """`first` runs until `gate`; then `second` starts, and once it is
        queued behind a lock `first` goes on."""
        a = Side(self.tid, first)
        b = Side(self.tid, second)
        before = deadlocks()
        with patch:
            a.start()
            self.assertTrue(gate.reached.wait(20), "the first never started")
            b.start()
            self.assertTrue(waits_on_a_lock(b.pid),
                            "the second never met the first")
            gate.go.set()
            self.assertTrue(a.finish(), "the first hung")
            self.assertTrue(b.finish(), "the second hung")
        self.assertIsNone(a.error, a.error)
        self.assertIsNone(b.error, b.error)
        self.assertNotIn("error", b.result or {}, b.result)
        self.assertEqual(deadlocks_since(before), 0, "the two deadlocked")


class RenameUndoMeetsABillApprovalTests(_Collide):
    def test_an_undo_takes_the_bills_before_the_charges(self):
        display = self.lines("SQ *LANTERN BOOKS")
        add_bill(self.conn, display, 40.0)
        with self.conn.transaction():
            merchant_dedup.rename(self.conn, display, "Lantern Bookshop")
        change = self.conn.execute(
            "SELECT max(id) AS id FROM merchant_renames").fetchone()["id"]
        # the rename re-pointed the bill, which now carries the note the
        # undo reads to point it back
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM bills WHERE raw ? 'merchant_merges'").fetchone())
        pid = self.fee_offer(display)

        def undo(conn):
            with conn.transaction():
                return merchant_dedup.undo(conn, change)

        gate = Gate()
        # paused once the charges moved back, before the bills follow
        self.collide(
            undo, gate,
            mock.patch.object(merchant_identity, "reconcile_raws",
                              gate.wrap(merchant_identity.reconcile_raws)),
            lambda c: bills.apply_proposal(c, pid, "approve"))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM bill_proposals WHERE id=%s",
            (pid,)).fetchone()["status"], "approved")


class SeveralRenamesInOneTransactionTests(_Collide):
    def test_a_later_rename_never_reaches_for_a_bill_an_approval_holds(self):
        north = self.lines("SQ *LANTERN BOOKS NORTH")
        south = self.lines("SQ *LANTERN BOOKS SOUTH")
        self.assertNotEqual(north, south)
        # one bill pays both outlets
        bills.save_bill(self.conn, payee="Lantern Books", amount=40.0,
                        frequency="MONTHLY", merchants=[north, south],
                        source="detected")
        pid = self.fee_offer("Lantern Books")

        def two_renames(conn):
            with conn.transaction():
                merchant_identity._lock_identity(conn)
                merchant_dedup.rename(conn, north, "Lantern Books")
                merchant_dedup.rename(conn, south, "Lantern Books")

        gate = Gate()
        # paused after the first rename moved its charges, before the
        # second locks its bills
        self.collide(
            two_renames, gate,
            mock.patch.object(bills, "lock_merchant_rename",
                              gate.before(bills.lock_merchant_rename,
                                          call=2)),
            lambda c: bills.apply_proposal(c, pid, "approve"))


if __name__ == "__main__":
    unittest.main()
