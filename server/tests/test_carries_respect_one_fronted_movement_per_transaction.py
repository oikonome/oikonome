"""Carrying a retired row's children never breaks one-fronted-movement.

A personally-paid business cost is explained once per transaction: as a
contribution OR as reimbursable, never both (the database holds this with
a unique index over the two kinds together). When a retired row's
children move onto its live twin, a contribution on the retired row and a
reimbursement already on the twin are two answers to one question; the
twin keeps its own, the retired row's stays where it is, and everything
else still moves. Before, the carry tested the kind alone, the index
refused the move, and the whole carry rolled back, stranding notes and
receipts on a row no read shows.
"""
import datetime as dt
import logging
import unittest

from oikonome.sync import base, reanchor

from .util import make_db

DAY = dt.date(2026, 8, 14)


class FrontedCarryTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.ent = self.conn.execute(
            "INSERT INTO business_entity (name, structure)"
            " VALUES ('Example Studio LLC', 'single_member_llc')"
            " RETURNING id").fetchone()["id"]

    def _txn(self, tid, removed=0, pending=0):
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " pending, removed) VALUES (%s,'card',%s,20.0,'NORTHWIND"
            " HOSTING',%s,%s)", (tid, DAY, pending, removed))

    def _front(self, tid, kind):
        self.conn.execute(
            "INSERT INTO equity_movement (entity_id, kind, amount, date,"
            " txn_id) VALUES (%s,%s,20.0,%s,%s)", (self.ent, kind, DAY, tid))

    def _kinds(self, tid):
        return sorted(r["kind"] for r in self.conn.execute(
            "SELECT kind FROM equity_movement WHERE txn_id=%s",
            (tid,)).fetchall())

    def test_reanchor_moves_the_rest_when_the_twin_has_its_own_answer(self):
        self._txn("old", removed=1)
        self._txn("new")
        self._front("old", "contribution")
        self._front("new", "reimbursement")
        self.conn.execute("INSERT INTO transaction_notes (txn_id, note)"
                          " VALUES ('old', 'client lunch')")
        moved = reanchor.carry(self.conn, "old", "new")
        self.assertIsNotNone(moved)
        self.assertEqual(self.conn.execute(
            "SELECT note FROM transaction_notes WHERE txn_id='new'"
        ).fetchone()["note"], "client lunch")
        self.assertEqual(self._kinds("new"), ["reimbursement"])
        self.assertEqual(self._kinds("old"), ["contribution"])

    def test_settlement_leaves_a_fronted_answer_the_posted_row_has(self):
        self._txn("pend", pending=1)
        self._front("pend", "contribution")
        self._txn("post")
        self._front("post", "reimbursement")
        with self.assertNoLogs("oikonome.sync.base", level=logging.WARNING):
            base.upsert_transactions(self.conn, [base.Transaction(
                id="post", account_id="card", date=DAY, amount=20.0,
                name="NORTHWIND HOSTING",
                raw={"pending_transaction_id": "pend"})])
        self.assertEqual(self._kinds("post"), ["reimbursement"])

    def test_settlement_still_carries_a_draw_beside_a_fronted_answer(self):
        self._txn("pend", pending=1)
        self._front("pend", "draw")
        self._txn("post")
        self._front("post", "reimbursement")
        base.upsert_transactions(self.conn, [base.Transaction(
            id="post", account_id="card", date=DAY, amount=20.0,
            name="NORTHWIND HOSTING",
            raw={"pending_transaction_id": "pend"})])
        self.assertEqual(self._kinds("post"), ["draw", "reimbursement"])

    def test_settlement_rebooks_a_fronted_hold_at_the_posted_amount(self):
        # a 50.00 pre-authorisation capitalized while pending, posting at
        # 46.81: capital and the P&L must agree on the posted cost
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " pending) VALUES ('pend','card',%s,50.0,'NORTHWIND FUEL',1)",
            (DAY,))
        self.conn.execute(
            "INSERT INTO equity_movement (entity_id, kind, amount, date,"
            " txn_id) VALUES (%s,'contribution',50.0,%s,'pend')",
            (self.ent, DAY))
        base.upsert_transactions(self.conn, [base.Transaction(
            id="post", account_id="card", date=DAY, amount=46.81,
            name="NORTHWIND FUEL",
            raw={"pending_transaction_id": "pend"})])
        r = self.conn.execute(
            "SELECT amount, note FROM equity_movement WHERE txn_id='post'"
        ).fetchone()
        self.assertEqual(float(r["amount"]), 46.81)
        self.assertIn("posted at 46.81", r["note"])

    def test_settlement_keeps_a_hand_typed_partial_movement(self):
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name,"
            " pending) VALUES ('pend','card',%s,50.0,'NORTHWIND FUEL',1)",
            (DAY,))
        self.conn.execute(
            "INSERT INTO equity_movement (entity_id, kind, amount, date,"
            " txn_id) VALUES (%s,'contribution',20.0,%s,'pend')",
            (self.ent, DAY))
        base.upsert_transactions(self.conn, [base.Transaction(
            id="post", account_id="card", date=DAY, amount=46.81,
            name="NORTHWIND FUEL",
            raw={"pending_transaction_id": "pend"})])
        self.assertEqual(float(self.conn.execute(
            "SELECT amount FROM equity_movement WHERE txn_id='post'"
        ).fetchone()["amount"]), 20.0)


if __name__ == "__main__":
    unittest.main()
