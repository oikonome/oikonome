"""A business pick on a pending charge that has already posted lands on
the posted charge.

A list can show a pending hold after the sync has posted it under a new
id and retired the hold. Capitalizing it, marking it reimbursable or
booking it to a business from that list used to write onto the retired
hold: the movement counted in the capital account at the hold's amount,
while the posted charge stayed personal spend, and nothing carried it
later — the settlement's carry had already run, and a hold that posts for
a different amount (a tip, a fuel pre-authorisation) has no same-amount
twin. The posted charge is found by the hold's id it carries.
"""
import datetime as dt
import unittest

from oikonome.engine import books, entities, equity
from oikonome.sync import base

from .util import TODAY, add_txn, make_db, write_config


class SettledHoldTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Fieldwork LLC", structure="sole_prop")
        self.hold = add_txn(self.conn, TODAY - dt.timedelta(days=1), 100.0,
                            "FUEL STOP", pending=1, txn_id="hold-1")
        base.upsert_transactions(self.conn, [base.Transaction(
            id="posted-1", account_id="card", date=TODAY, amount=45.12,
            name="FUEL STOP", category_primary="TRANSPORTATION",
            raw={"pending_transaction_id": "hold-1"})])
        self.conn.execute("UPDATE transactions SET removed = 1 "
                          "WHERE id = 'hold-1'")

    def tearDown(self):
        self.conn.close()

    def _entity_of(self, tid):
        r = self.conn.execute("SELECT entity_id FROM transactions "
                              "WHERE id = %s", (tid,)).fetchone()
        return str(r["entity_id"]) if r["entity_id"] else None

    def test_capitalizing_the_hold_capitalizes_the_posted_charge(self):
        m = equity.contribute_expense(self.conn, self.ent["id"], self.hold)
        self.assertEqual(m["txn_id"], "posted-1")
        self.assertEqual(float(m["amount"]), 45.12)
        self.assertEqual(self._entity_of("posted-1"), str(self.ent["id"]))
        self.assertIsNone(self._entity_of(self.hold))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) AS n FROM equity_movement WHERE txn_id = %s",
            (self.hold,)).fetchone()["n"], 0)

    def test_the_entity_override_follows_the_hold_to_its_posted_charge(self):
        self.assertTrue(entities.assign_transaction(
            self.conn, self.hold, self.ent["id"]))
        self.assertEqual(self._entity_of("posted-1"), str(self.ent["id"]))
        self.assertIsNone(self._entity_of(self.hold))

    def test_a_row_gone_with_no_posted_charge_is_refused(self):
        gone = add_txn(self.conn, TODAY, 30.0, "CANCELLED HOLD", pending=1)
        self.conn.execute("UPDATE transactions SET removed = 1 "
                          "WHERE id = %s", (gone,))
        self.assertFalse(entities.assign_transaction(
            self.conn, gone, self.ent["id"]))
        with self.assertRaises(ValueError):
            equity.reimburse_expense(self.conn, self.ent["id"], gone)
        self.assertIsNone(self._entity_of(gone))

    def test_classifying_a_retired_row_is_not_the_entitys_to_do(self):
        self.conn.execute("UPDATE transactions SET entity_id = %s "
                          "WHERE id = %s", (self.ent["id"], self.hold))
        self.assertFalse(books.txn_belongs_to_entity(
            self.conn, self.ent["id"], self.hold))


if __name__ == "__main__":
    unittest.main()
