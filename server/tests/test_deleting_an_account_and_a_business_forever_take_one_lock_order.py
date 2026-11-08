"""Deleting an account (or unlinking a plan or wallet, or undoing an
import) and deleting a business forever keep one lock order: the
transaction rows, then the business row.

delete-forever locks every row booked to the business, in id order, and
deletes the business row last; an assignment takes its row and then the
business FOR SHARE. The account deletes look at the business too — whether
it is archived (its rows are kept) or which fronted costs leave with the
charge — under FOR SHARE on the business row. If that look came before the
delete locked the rows it goes on to write, a delete-forever already
holding those rows would wait on the share lock while the account delete
waits on the rows: Postgres kills one of them and the owner gets an error
for a delete that should simply have queued. So the account deletes lock
the rows they will write, in id order, before they look at any business.
"""
import unittest
from unittest import mock

from oikonome.engine import entities, equity
from oikonome.sync import base as sync_base, batches, coinbase, plan_csv
from oikonome.web import data as web_data

from .lock_order_util import (Gate, Side, deadlocks, deadlocks_since,
                              tenant_of, waits_on_a_lock)
from .util import add_txn, make_db, write_config

DAY = "2026-03-10"
NAME = "Example Studio LLC"


class AccountDeletesMeetADeleteForeverTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.ent = entities.create_entity(
            self.conn, name=NAME, structure="sole_prop")["id"]

    def _account(self, item, aid):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES (%s, 'manual', 'Test')"
            " ON CONFLICT (tenant_id, id) DO NOTHING", (item,))
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES (%s, %s, 'Test', 'investment', 'brokerage')",
            (aid, item))

    def _booked(self, account="card", fronted=False):
        tid = add_txn(self.conn, DAY, 600.0, "NORTHWIND HOSTING",
                      account=account)
        self.assertTrue(entities.assign_transaction(self.conn, tid, self.ent))
        if fronted:
            equity.contribute_expense(self.conn, self.ent, tid)
        add_txn(self.conn, DAY, 30.0, "CORNER CAFE", account=account)
        return tid

    def _collide(self, delete):
        """delete-forever takes the booked rows and stops just before it
        deletes the business; `delete` then runs on a second connection
        and must queue behind it; then delete-forever goes on. Both must
        finish, and the database must have seen no deadlock."""
        gate = Gate()

        def forever(c):
            real = c.execute

            def execute(query, *a, **kw):
                if str(query).startswith("DELETE FROM business_entity"):
                    gate._pause()
                return real(query, *a, **kw)
            c.execute = execute
            return entities.delete_forever(c, self.ent, NAME)

        tid = tenant_of(self.conn)
        d = Side(tid, forever)
        p = Side(tid, delete)
        before = deadlocks()
        try:
            d.start()
            self.assertTrue(gate.reached.wait(20),
                            "delete-forever never took the booked rows")
            p.start()
            self.assertTrue(waits_on_a_lock(p.pid),
                            "the account delete never met delete-forever")
            gate.go.set()
        finally:
            gate.go.set()
            self.assertTrue(d.finish(), "delete-forever hung")
            self.assertTrue(p.finish(), "the account delete hung")
        self.assertIsNone(d.error, f"delete-forever failed: {d.error!r}")
        self.assertIsNone(p.error, f"the account delete failed: {p.error!r}")
        self.assertEqual(deadlocks_since(before), 0)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM business_entity WHERE id = %s",
            (self.ent,)).fetchone())

    def test_purging_an_account_with_an_archived_businesss_row(self):
        self._booked()
        entities.archive_entity(self.conn, self.ent)
        self._collide(lambda c: sync_base.purge_account_data(c, "card"))

    def test_purging_an_account_with_a_fronted_cost(self):
        self._booked(fronted=True)
        self._collide(lambda c: sync_base.purge_account_data(c, "card"))

    def test_unlinking_a_plan_with_a_fronted_cost(self):
        aid = plan_csv.account_id(plan_csv.DEFAULT_PROVIDER, "401K")
        self._account(plan_csv.DEFAULT_PROVIDER, aid)
        self._booked(account=aid, fronted=True)
        self._collide(lambda c: plan_csv.rollback(c, "401K"))

    def test_unlinking_a_wallet_with_a_fronted_cost(self):
        self._account(coinbase.item_id("w1"), coinbase.account_id("w1"))
        self._booked(account=coinbase.account_id("w1"), fronted=True)
        self._collide(lambda c: coinbase.rollback(c, "w1"))

    def test_unlinking_a_wallet_with_an_archived_businesss_row(self):
        self._account(coinbase.item_id("w1"), coinbase.account_id("w1"))
        self._booked(account=coinbase.account_id("w1"), fronted=True)
        entities.archive_entity(self.conn, self.ent)
        self._collide(lambda c: coinbase.rollback(c, "w1"))

    BATCH = "imp-1"   # an import undo deletes rows too, in the same order

    def _from_import(self, *ids):
        self.conn.execute(
            "UPDATE transactions SET raw = raw || jsonb_build_object("
            "'_batch', %s::text) WHERE id = ANY(%s)", (self.BATCH, list(ids)))
        self.conn.execute(
            "INSERT INTO import_batches (id, source, account_id, row_count)"
            " VALUES (%s, 'csv', 'card', %s)", (self.BATCH, len(ids)))

    def test_undoing_an_import_with_a_fronted_cost(self):
        booked = self._booked(fronted=True)
        cafe = self.conn.execute(
            "SELECT id FROM transactions WHERE id <> %s AND account_id='card'",
            (booked,)).fetchone()["id"]
        self._from_import(booked, cafe)
        self._collide(lambda c: batches.rollback(c, self.BATCH))

    def test_a_purge_takes_the_paired_row_with_its_own(self):
        """A personal charge on the account is paired with a deposit on
        another account that is booked to the business. Unlinking the pair
        rewrites the deposit, so the purge locks it together with its own
        rows, in the same id order delete-forever uses — otherwise the
        purge holds the business's other row while delete-forever holds the
        deposit, and each waits for the other."""
        dep = add_txn(self.conn, DAY, -30.0, "REFUND", account="chk",
                      txn_id="a-deposit")
        booked = add_txn(self.conn, DAY, 600.0, "NORTHWIND HOSTING",
                         txn_id="b-booked")
        cafe = add_txn(self.conn, DAY, 30.0, "CORNER CAFE",
                       txn_id="c-cafe")
        web_data.link_reimbursement(self.conn, cafe, dep)
        self.assertTrue(entities.assign_transaction(self.conn, dep, self.ent))
        self.assertTrue(entities.assign_transaction(self.conn, booked,
                                                    self.ent))
        entities.archive_entity(self.conn, self.ent)
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM reimbursements WHERE expense_id = %s",
            (cafe,)).fetchone())

        gate = Gate()
        tid = tenant_of(self.conn)
        with mock.patch.object(sync_base, "lock_account_rows",
                               gate.wrap(sync_base.lock_account_rows)):
            p = Side(tid, lambda c: sync_base.purge_account_data(c, "card"))
            d = Side(tid, lambda c: entities.delete_forever(c, self.ent,
                                                            NAME))
            before = deadlocks()
            try:
                p.start()
                self.assertTrue(gate.reached.wait(20),
                                "the purge never locked its rows")
                d.start()
                self.assertTrue(waits_on_a_lock(d.pid),
                                "delete-forever never met the purge")
                gate.go.set()
            finally:
                gate.go.set()
                self.assertTrue(p.finish(), "the purge hung")
                self.assertTrue(d.finish(), "delete-forever hung")
        self.assertIsNone(p.error, f"the purge failed: {p.error!r}")
        self.assertIsNone(d.error, f"delete-forever failed: {d.error!r}")
        self.assertEqual(deadlocks_since(before), 0)


if __name__ == "__main__":
    unittest.main()
