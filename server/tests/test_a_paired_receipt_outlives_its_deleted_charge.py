"""A receipt paired with a charge goes back to waiting when the charge is
deleted outright.

A receipt snapped before its charge is a thing of its own: the matcher (or
a person) hangs it on the charge later. Undoing an import, purging an
account and unlinking a plan or wallet hard-delete transaction rows, and the
foreign key's cascade would take that photograph with them though nobody
chose to delete it. It waits again instead, and meets the charge if the
rows come back. A receipt uploaded onto the charge itself is part of that
charge and goes with it. One a PERSON paired keeps its hand mark while it
waits — the same as when its charge is retired — so the rules never pair it
again behind their back, and the list shows it released for them to choose.
"""
import unittest

from oikonome.engine import receipt_match, receipts
from oikonome.sync import base as sync_base, batches, coinbase, plan_csv

from .test_a_receipt_snapped_early_meets_its_charge import (
    DAY, PNG, _enable_llm, _snap)
from .util import add_txn, make_db, write_config


class PairedReceiptOutlivesItsChargeTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _receipts_on(self, tid):
        """(one paired by hand, one uploaded onto the charge itself)."""
        paired = receipts.add(self.conn, None, PNG, "image/png")
        self.assertTrue(receipt_match.match_by_hand(self.conn, paired, tid))
        own = receipts.add(self.conn, tid, PNG, "image/png")
        return paired, own

    def _assert_released(self, paired, own):
        r = self.conn.execute(
            "SELECT txn_id, matched_at, match_method FROM receipts"
            " WHERE id = %s", (paired,)).fetchone()
        self.assertEqual((r["txn_id"], r["matched_at"], r["match_method"]),
                         (None, None, "manual"))
        listed = {w["id"]: w for w in
                  receipt_match.waiting(self.conn)["waiting"]}
        self.assertIn(paired, listed)
        self.assertTrue(listed[paired]["released"])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM receipts WHERE id = %s", (own,)).fetchone())

    def _account(self, item, aid):
        self.conn.execute(
            "INSERT INTO items (id, aggregator, institution_name)"
            " VALUES (%s, 'manual', 'Test')"
            " ON CONFLICT (tenant_id, id) DO NOTHING", (item,))
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype)"
            " VALUES (%s, %s, 'Test', 'investment', 'brokerage')",
            (aid, item))

    def test_undoing_an_import(self):
        tid = add_txn(self.conn, DAY, 18.40, "CORNER CAFE")
        self.conn.execute(
            "UPDATE transactions SET raw = %s::jsonb WHERE id = %s",
            ('{"_batches": ["batch-r"], "_batch": "batch-r"}', tid))
        paired, own = self._receipts_on(tid)
        self.assertEqual(int(batches.rollback(self.conn, "batch-r")), 1)
        self._assert_released(paired, own)

    def test_an_undone_hand_pairing_is_not_re_paired_by_the_rules(self):
        """The same file imported again brings the charge back as a lone
        exact match; the rules would pair it on sight, and the person who
        chose the first pairing never chose this one."""
        _enable_llm(self.conn)
        rid = _snap(self.conn, "Corner Cafe", 18.40)
        raw = '{"_batches": ["batch-h"], "_batch": "batch-h"}'
        tid = add_txn(self.conn, DAY, 18.40, "CORNER CAFE")
        self.conn.execute("UPDATE transactions SET raw = %s::jsonb"
                          " WHERE id = %s", (raw, tid))
        # the rules would already have taken it; a person pairing it is the
        # case under test, so start from a hand pairing
        self.conn.execute("UPDATE receipts SET txn_id = NULL,"
                          " match_method = NULL WHERE id = %s", (rid,))
        self.assertTrue(receipt_match.match_by_hand(self.conn, rid, tid))
        self.assertEqual(int(batches.rollback(self.conn, "batch-h")), 1)
        add_txn(self.conn, DAY, 18.40, "CORNER CAFE")
        receipt_match.match_waiting(self.conn)
        r = self.conn.execute("SELECT txn_id, match_method FROM receipts"
                              " WHERE id = %s", (rid,)).fetchone()
        self.assertEqual((r["txn_id"], r["match_method"]), (None, "manual"))
        w = {x["id"]: x for x in receipt_match.waiting(self.conn)["waiting"]}
        self.assertTrue(w[rid]["released"])

    def test_an_undone_rule_pairing_meets_the_charge_again(self):
        _enable_llm(self.conn)
        rid = _snap(self.conn, "Corner Cafe", 18.40)
        tid = add_txn(self.conn, DAY, 18.40, "CORNER CAFE")
        self.conn.execute("UPDATE transactions SET raw = %s::jsonb"
                          " WHERE id = %s",
                          ('{"_batches": ["batch-a"], "_batch": "batch-a"}',
                           tid))
        receipt_match.match_waiting(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT match_method FROM receipts WHERE id = %s",
            (rid,)).fetchone()["match_method"], "auto")
        self.assertEqual(int(batches.rollback(self.conn, "batch-a")), 1)
        again = add_txn(self.conn, DAY, 18.40, "CORNER CAFE")
        receipt_match.match_waiting(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT txn_id FROM receipts WHERE id = %s",
            (rid,)).fetchone()["txn_id"], again)

    def test_purging_an_account(self):
        tid = add_txn(self.conn, DAY, 18.40, "CORNER CAFE")
        paired, own = self._receipts_on(tid)
        self.assertEqual(
            sync_base.purge_account_data(self.conn, "card")["transactions"], 1)
        self._assert_released(paired, own)

    def test_unlinking_a_plan(self):
        aid = plan_csv.account_id(plan_csv.DEFAULT_PROVIDER, "401K")
        self._account(plan_csv.DEFAULT_PROVIDER, aid)
        tid = add_txn(self.conn, DAY, 18.40, "CONTRIBUTION", account=aid)
        paired, own = self._receipts_on(tid)
        self.assertEqual(plan_csv.rollback(self.conn, "401K"), 1)
        self._assert_released(paired, own)

    def test_unlinking_a_wallet(self):
        self._account(coinbase.item_id("w1"), coinbase.account_id("w1"))
        tid = add_txn(self.conn, DAY, 18.40, "BUY",
                      account=coinbase.account_id("w1"))
        paired, own = self._receipts_on(tid)
        self.assertEqual(coinbase.rollback(self.conn, "w1"), 1)
        self._assert_released(paired, own)


if __name__ == "__main__":
    unittest.main()
