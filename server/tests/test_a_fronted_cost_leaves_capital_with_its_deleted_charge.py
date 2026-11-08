"""A capitalized or reimbursable cost leaves the books with its charge.

The movement a fronted expense records is the other half of the cost on the
business's books. Undoing an import, purging an account and unlinking a plan
or wallet hard-delete the charge, so the cost stops counting. The foreign
key's SET NULL would keep the movement, unlinked: capital reads the money as
contributed for a cost the books no longer hold, and booking the same file's
row again records a second contribution no index can refuse. The movement
goes with the charge, and the activity log says so. A movement recorded by
hand is a real entry of its own and keeps only losing its link. An archived
business's charge does not go at all: its books are retention records that
only delete-forever may destroy, so every one of those doors keeps the row,
its class and its movement, and the account stays to hold it.
"""
import unittest

from oikonome.engine import entities, equity, reporting
from oikonome.sync import base as sync_base, batches, coinbase, plan_csv

from .util import add_txn, make_db, write_config

DAY = "2026-03-10"
RAW = '{"_batches": ["batch-f"], "_batch": "batch-f"}'


class FrontedCostLeavesWithItsChargeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.ent = self.conn.execute(
            "INSERT INTO business_entity (name, structure)"
            " VALUES ('Example Studio LLC', 'single_member_llc')"
            " RETURNING id").fetchone()["id"]

    def _imported(self, amount=600.0, batch_raw=RAW, tid=None):
        if tid is None:
            tid = add_txn(self.conn, DAY, amount, "NORTHWIND HOSTING")
        else:
            self.conn.execute(
                "INSERT INTO transactions (id, account_id, date, amount, name,"
                " pending, removed) VALUES (%s,'card',%s,%s,"
                " 'NORTHWIND HOSTING',0,0)", (tid, DAY, amount))
        self.conn.execute(
            "UPDATE transactions SET raw = %s::jsonb WHERE id = %s",
            (batch_raw, tid))
        self.conn.execute(
            "INSERT INTO import_batches (id, source, account_id, row_count)"
            " VALUES ('batch-f','csv','card',1)"
            " ON CONFLICT (tenant_id, id) DO NOTHING")
        return tid

    def _movements(self):
        return self.conn.execute(
            "SELECT kind, txn_id FROM equity_movement"
            " WHERE entity_id = %s", (self.ent,)).fetchall()

    def _contributions(self):
        return equity.capital_summary(self.conn, self.ent)["contributions"]

    def _account(self, item, aid):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES (%s, 'manual', 'Test')"
            " ON CONFLICT (tenant_id, id) DO NOTHING", (item,))
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES (%s, %s, 'Test', 'investment', 'brokerage')",
            (aid, item))

    def test_undo_then_rebook_counts_the_cost_once(self):
        tid = self._imported()
        equity.contribute_expense(self.conn, self.ent, tid)
        self.assertEqual(self._contributions(), 600.0)
        self.assertEqual(int(batches.rollback(self.conn, "batch-f")), 1)
        self.assertEqual(self._movements(), [])
        self.assertEqual(self._contributions(), 0.0)
        self._imported(tid=tid)
        equity.contribute_expense(self.conn, self.ent, tid)
        self.assertEqual(self._contributions(), 600.0)
        self.assertEqual(len(self._movements()), 1)

    def test_a_reimbursement_goes_with_its_charge_too(self):
        tid = self._imported()
        equity.reimburse_expense(self.conn, self.ent, tid)
        batches.rollback(self.conn, "batch-f")
        self.assertEqual(self._movements(), [])

    def test_the_removal_is_written_to_the_activity_log(self):
        tid = self._imported()
        equity.contribute_expense(self.conn, self.ent, tid)
        batches.rollback(self.conn, "batch-f")
        row = self.conn.execute(
            "SELECT label FROM activity_log WHERE kind = 'business'"
            " AND action = 'released' AND target = %s", (tid,)).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("contribution", row["label"])
        self.assertIn("Example Studio LLC", row["label"])

    def test_a_movement_recorded_by_hand_keeps_its_entry(self):
        """A draw that merely names the charge is a real entry of its own;
        only a fronted movement is half of the charge's cost."""
        tid = self._imported()
        equity.record_movement(self.conn, self.ent, kind="draw", amount=50.0,
                               date=DAY, txn_id=tid)
        batches.rollback(self.conn, "batch-f")
        self.assertEqual([(m["kind"], m["txn_id"]) for m in self._movements()],
                         [("draw", None)])

    def test_a_row_a_feed_relies_on_keeps_its_movement(self):
        """The row stays on the ledger, so the cost still counts."""
        tid = self._imported()
        equity.contribute_expense(self.conn, self.ent, tid)
        keep = {tid}
        real = sync_base.relied_on_by_feed
        sync_base.relied_on_by_feed = (
            lambda conn, ids, *a, **k: keep & set(ids))
        self.addCleanup(setattr, sync_base, "relied_on_by_feed", real)
        batches.rollback(self.conn, "batch-f")
        self.assertEqual(len(self._movements()), 1)
        self.assertEqual(self._contributions(), 600.0)

    def test_purging_an_account(self):
        tid = self._imported()
        equity.contribute_expense(self.conn, self.ent, tid)
        sync_base.purge_account_data(self.conn, "card")
        self.assertEqual(self._movements(), [])

    def _booked_then_archived(self):
        tid = self._imported()
        equity.contribute_expense(self.conn, self.ent, tid)
        entities.archive_entity(self.conn, self.ent)
        return tid

    def _classed(self, tid):
        self.conn.execute(
            "INSERT INTO business_txn_class (txn_id, bucket, sched_c_line)"
            " VALUES (%s, 'expense', '8')", (tid,))

    def _kept_booked(self, tid, aid):
        """The archived business's row survives an unlink with its Schedule
        C class and its movement, and the account stays to hold it."""
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM transactions WHERE id = %s", (tid,)).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM business_txn_class WHERE txn_id = %s",
            (tid,)).fetchone())
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM accounts WHERE id = %s", (aid,)).fetchone())
        self.assertEqual([(m["kind"], m["txn_id"]) for m in self._movements()],
                         [("contribution", tid)])
        self.assertEqual(self._contributions(), 600.0)

    def test_purging_an_account_keeps_an_archived_businesss_books(self):
        """The booked charge is the archived business's row, so the purge
        keeps it (as undo refuses to delete it), movement and all."""
        tid = self._booked_then_archived()
        self.conn.execute("UPDATE accounts SET balance_current = -2400"
                          " WHERE id = 'card'")
        sync_base.purge_account_data(self.conn, "card")
        self.assertEqual([(m["kind"], m["txn_id"]) for m in self._movements()],
                         [("contribution", tid)])
        self.assertEqual(self._contributions(), 600.0)
        # the account stays only to hold those rows: the balance the person
        # deleted it with no longer counts toward net worth
        self.assertNotIn("card", {r["id"] for r in
                                  reporting._live_accounts(self.conn)})

    def test_unlinking_a_plan_keeps_an_archived_businesss_books(self):
        aid = plan_csv.account_id(plan_csv.DEFAULT_PROVIDER, "401K")
        self._account(plan_csv.DEFAULT_PROVIDER, aid)
        tid = add_txn(self.conn, DAY, 600.0, "PLAN FEE", account=aid)
        equity.contribute_expense(self.conn, self.ent, tid)
        self._classed(tid)
        entities.archive_entity(self.conn, self.ent)
        self.conn.execute("UPDATE accounts SET balance_current = 9100"
                          " WHERE id = %s", (aid,))
        self.assertEqual(plan_csv.rollback(self.conn, "401K"), 0)
        self._kept_booked(tid, aid)
        # kept only for those rows: its last balance leaves net worth
        self.assertNotIn(aid, {r["id"] for r in
                               reporting._live_accounts(self.conn)})

    def test_unlinking_a_wallet_keeps_an_archived_businesss_books(self):
        self._account(coinbase.item_id("w1"), coinbase.account_id("w1"))
        tid = add_txn(self.conn, DAY, 600.0, "WALLET FEE",
                      account=coinbase.account_id("w1"))
        equity.contribute_expense(self.conn, self.ent, tid)
        self._classed(tid)
        entities.archive_entity(self.conn, self.ent)
        self.conn.execute("UPDATE accounts SET balance_current = 730"
                          " WHERE id = %s", (coinbase.account_id("w1"),))
        self.assertEqual(coinbase.rollback(self.conn, "w1"), 0)
        self._kept_booked(tid, coinbase.account_id("w1"))
        self.assertNotIn(coinbase.account_id("w1"), {
            r["id"] for r in reporting._live_accounts(self.conn)})

    def test_unlinking_a_plan(self):
        aid = plan_csv.account_id(plan_csv.DEFAULT_PROVIDER, "401K")
        self._account(plan_csv.DEFAULT_PROVIDER, aid)
        tid = add_txn(self.conn, DAY, 600.0, "PLAN FEE", account=aid)
        equity.contribute_expense(self.conn, self.ent, tid)
        self.assertEqual(plan_csv.rollback(self.conn, "401K"), 1)
        self.assertEqual(self._movements(), [])

    def test_unlinking_a_wallet(self):
        self._account(coinbase.item_id("w1"), coinbase.account_id("w1"))
        tid = add_txn(self.conn, DAY, 600.0, "WALLET FEE",
                      account=coinbase.account_id("w1"))
        equity.contribute_expense(self.conn, self.ent, tid)
        self.assertEqual(coinbase.rollback(self.conn, "w1"), 1)
        self.assertEqual(self._movements(), [])

    def test_a_wrong_account_retire_keeps_a_booked_row(self):
        """retire_rows deletes only what nobody touched; the row booked to a
        business is someone's work, whichever half of the booking it holds."""
        fronted = self._imported()
        equity.contribute_expense(self.conn, self.ent, fronted)
        tagged = add_txn(self.conn, DAY, 40.0, "PAPER SUPPLY")
        self.assertTrue(entities.assign_transaction(self.conn, tagged,
                                                    self.ent))
        out = batches.retire_rows(self.conn, [fronted, tagged])
        self.assertEqual((out["removed"], out["kept_annotated"]), (0, 2))
        self.assertEqual(self._contributions(), 600.0)

    def test_the_undo_confirm_counts_a_booked_row(self):
        tid = self._imported()
        self.conn.execute("UPDATE import_batches SET row_count = 1")
        self.assertEqual(batches.recent(self.conn)[0]["annotations"], 0)
        equity.contribute_expense(self.conn, self.ent, tid)
        self.assertEqual(batches.recent(self.conn)[0]["annotations"], 1)
        self.assertEqual(batches.annotated(self.conn, [tid]), {tid})


if __name__ == "__main__":
    unittest.main()
