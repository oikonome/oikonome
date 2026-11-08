"""Deleting an account keeps the rows an archived business owns.

An archived business's transactions are retention records that only the
owner's delete-forever may remove, and delete-forever itself returns them to
personal rather than deleting them. Undoing an import refuses to delete them
and a wrong-account retire keeps them. Purging an account, or every account
of a connection, must not be the road around that: a row assigned to an
archived business (or on an account the business owns) stays, with its
Schedule C class, the account stays to hold it, and the result says why.
The other rows on the account still go.
"""
import unittest

from oikonome.engine import entities
from oikonome.sync import base as sync_base

from .util import add_txn, make_db, write_config

DAY = "2026-03-10"


class DeletingAnAccountKeepsArchivedBooks(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Example Studio LLC",
            structure="sole_prop")["id"]

    def _booked(self, name="NORTHWIND HOSTING"):
        tid = add_txn(self.conn, DAY, 600.0, name)
        self.assertTrue(entities.assign_transaction(self.conn, tid, self.ent))
        self.conn.execute(
            "INSERT INTO business_txn_class (txn_id, bucket, sched_c_line)"
            " VALUES (%s, 'expense', '8')", (tid,))
        return tid

    def _alive(self, tid):
        return self.conn.execute(
            "SELECT 1 FROM transactions WHERE id = %s", (tid,)).fetchone()

    def _classed(self, tid):
        return self.conn.execute(
            "SELECT 1 FROM business_txn_class WHERE txn_id = %s",
            (tid,)).fetchone()

    def _card_exists(self):
        return self.conn.execute(
            "SELECT 1 FROM accounts WHERE id = 'card'").fetchone()

    def test_purging_an_account_keeps_an_archived_businesss_row(self):
        booked = self._booked()
        personal = add_txn(self.conn, DAY, 30.0, "CORNER CAFE")
        entities.archive_entity(self.conn, self.ent)
        out = sync_base.purge_account_data(self.conn, "card")
        self.assertIsNotNone(self._alive(booked))
        self.assertIsNotNone(self._classed(booked))
        self.assertIsNone(self._alive(personal))
        self.assertIsNotNone(self._card_exists())
        self.assertEqual(out["kept_transactions"], 1)
        self.assertIn("archived business", out["note"])

    def test_purging_an_account_an_archived_business_owns_keeps_its_rows(self):
        tid = add_txn(self.conn, DAY, 600.0, "NORTHWIND HOSTING")
        entities.assign_account(self.conn, "card", self.ent)
        entities.archive_entity(self.conn, self.ent)
        out = sync_base.purge_account_data(self.conn, "card")
        self.assertIsNotNone(self._alive(tid))
        self.assertEqual(out["accounts"], 0)

    def test_force_kept_does_not_reach_an_archived_businesss_row(self):
        tid = self._booked()
        entities.archive_entity(self.conn, self.ent)
        sync_base.purge_account_data(self.conn, "card", force_kept=True)
        self.assertIsNotNone(self._alive(tid))

    def test_purging_a_connection_keeps_an_archived_businesss_row(self):
        item = self.conn.execute(
            "SELECT item_id FROM accounts WHERE id = 'card'"
        ).fetchone()["item_id"]
        tid = self._booked()
        entities.archive_entity(self.conn, self.ent)
        out = sync_base.purge_item_accounts(self.conn, item)
        self.assertIsNotNone(self._alive(tid))
        self.assertIsNotNone(self._classed(tid))
        self.assertGreaterEqual(out["kept_transactions"], 1)
        self.assertIn("archived business", out["note"])

    def test_an_active_businesss_row_still_goes_with_the_account(self):
        tid = self._booked()
        out = sync_base.purge_account_data(self.conn, "card")
        self.assertIsNone(self._alive(tid))
        self.assertEqual(out["kept_transactions"], 0)


if __name__ == "__main__":
    unittest.main()
