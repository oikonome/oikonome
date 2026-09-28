"""A receipt a person took off a charge is never paired back onto it.

The refusal is remembered by the charge's id, and a charge changes id in
more places than a hold settling: a reconnect renames a restored row into
the id the new link uses, and a duplicate lineage row is retired onto its
twin. Each carries the refusal to the id the charge lives on now.

And a posted row can arrive before anything has carried the refusal — with
no link back to its hold, while the hold's removal comes later in the same
sync, or while both are still live. The matcher must not pair the receipt
with that twin automatically; it stays on offer for a person to choose.
That holds only for a refused HOLD (pending, or since retired): a refused
posted charge is a purchase of its own, and an identical one the next day
is another. The twin is the hold's settlement, which need not cost the
same: an aggregator with no pending link posts a tipped charge under a new
id. And the block lapses when the refused hold has a settlement of its own
at least as close to it — then the charge in question is another purchase.

The refusal is checked again at the moment of pairing, against the
receipt's row as it stands: a rename that moved it after the matcher read
the receipt must not leave a stale list to pair against.
"""
import datetime as dt
import unittest

from oikonome.engine import receipt_match
from oikonome.sync import adopt, base as sync_base

from .test_a_receipt_snapped_early_meets_its_charge import (
    CLOSED, DAY, _attached, _enable_llm, _post, _snap)
from .util import add_txn, make_db, write_config


def _memory(conn, rid):
    return list(conn.execute(
        "SELECT unmatched_txn_ids FROM receipts WHERE id = %s",
        (rid,)).fetchone()["unmatched_txn_ids"])


def _offered(conn, rid):
    for w in receipt_match.waiting(conn)["waiting"]:
        if w["id"] == str(rid):
            return [c["txn_id"] for c in w["candidates"]]
    return None


class RefusalFollowsARenameTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_refusal_follows_a_reconnect_rename(self):
        rid = _snap(self.conn)
        add_txn(self.conn, DAY, 42.17, "MAPLE LEAF BISTRO", txn_id="old-r")
        receipt_match.match_waiting(self.conn)
        self.assertEqual(receipt_match.unmatch(self.conn, rid), "old-r")
        self.assertTrue(adopt._rewrite_txn_id(self.conn, "old-r", "new-r"))
        self.assertEqual(_memory(self.conn, rid), ["new-r"])
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertEqual(_attached(self.conn, rid), (None, None))

    def test_a_refusal_follows_a_retired_duplicate_onto_its_survivor(self):
        rid = _snap(self.conn)
        add_txn(self.conn, DAY, 42.17, "MAPLE LEAF BISTRO", txn_id="lose-t")
        self.assertTrue(receipt_match.match_by_hand(self.conn, rid, "lose-t"))
        self.assertEqual(receipt_match.unmatch(self.conn, rid), "lose-t")
        add_txn(self.conn, DAY, 42.17, "MAPLE LEAF BISTRO", txn_id="keep-t")
        adopt._retire_twin(self.conn, "keep-t", "lose-t")
        self.assertEqual(_memory(self.conn, rid), ["keep-t"])
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertEqual(_attached(self.conn, rid), (None, None))


class RefusedChargesTwinIsNotPairedTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def _refused_hold(self, hold):
        rid = _snap(self.conn)
        _post(self.conn, hold, 42.17, "MAPLE LEAF BISTRO", pending=True,
              date=DAY)
        self.assertEqual(_attached(self.conn, rid), (hold, "auto"))
        self.assertEqual(receipt_match.unmatch(self.conn, rid), hold)
        return rid

    def test_a_posted_row_with_no_link_to_its_refused_hold_is_not_paired(self):
        rid = self._refused_hold("hold-z1")
        # the posted row arrives naming no hold; the matcher runs in this
        # sync, before any removal could carry the refusal over
        _post(self.conn, "post-z1", 42.17, "MAPLE LEAF BISTRO",
              date=DAY + dt.timedelta(days=1))
        self.assertEqual(_attached(self.conn, rid), (None, None))
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertEqual(_attached(self.conn, rid), (None, None))
        # a person can still see it and choose it
        self.assertIn("post-z1", _offered(self.conn, rid))
        # the hold's removal landing later changes nothing: the refusal
        # stays on the retired hold (the re-anchor's guess could land on a
        # different same-cents purchase), and a retired refused hold still
        # keeps its look-alike off
        sync_base.mark_removed(self.conn, ["hold-z1"])
        self.assertEqual(_memory(self.conn, rid), ["hold-z1"])
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertEqual(_attached(self.conn, rid), (None, None))

    def test_a_retired_refused_hold_still_keeps_its_twin_off(self):
        # the hold is already gone, with no twin found at the time: the
        # posted row arriving later is still that charge
        rid = self._refused_hold("hold-z2")
        sync_base.mark_removed(self.conn, ["hold-z2"])
        _post(self.conn, "post-z2", 42.17, "MAPLE LEAF BISTRO",
              date=DAY + dt.timedelta(days=2))
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertEqual(_attached(self.conn, rid), (None, None))

    def test_a_same_price_charge_a_week_away_is_still_paired(self):
        # the twin rule stops short of a week, like the re-anchor's: a
        # charge exactly a week on is another purchase, and inside the
        # automatic window it still takes the receipt
        rid = self._refused_hold("hold-z3")
        _post(self.conn, "later-z3", 42.17, "MAPLE LEAF BISTRO",
              date=DAY + dt.timedelta(days=7))
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertEqual(_attached(self.conn, rid), ("later-z3", "auto"))

    def test_a_refused_posted_charge_does_not_block_the_next_days_twin(self):
        # the same coffee bought two days running: the person took the
        # receipt off Monday's posted charge, and Tuesday's identical one
        # is a purchase of its own that the rules may pair
        rid = _snap(self.conn, "Corner Cafe", 4.50)
        _post(self.conn, "mon-z5", 4.50, "CORNER CAFE", date=DAY)
        self.assertEqual(_attached(self.conn, rid), ("mon-z5", "auto"))
        self.assertEqual(receipt_match.unmatch(self.conn, rid), "mon-z5")
        _post(self.conn, "tue-z5", 4.50, "CORNER CAFE",
              date=DAY + dt.timedelta(days=1))
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertEqual(_attached(self.conn, rid), ("tue-z5", "auto"))

    def test_a_same_price_charge_on_another_account_is_still_paired(self):
        rid = self._refused_hold("hold-z4")
        _post(self.conn, "other-z4", 42.17, "MAPLE LEAF BISTRO",
              date=DAY + dt.timedelta(days=1), account="chk")
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertEqual(_attached(self.conn, rid), ("other-z4", "auto"))


    def test_a_tipped_posting_of_a_refused_hold_is_not_paired(self):
        # no pending link: the hold is retired with nothing naming the
        # posting, and the posting carries the tip
        rid = self._refused_hold("hold-t1")
        sync_base.mark_removed(self.conn, ["hold-t1"])
        _post(self.conn, "post-t1", 50.60, "MAPLE LEAF BISTRO",
              date=DAY + dt.timedelta(days=2))
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertEqual(_attached(self.conn, rid), (None, None))
        self.assertIn("post-t1", _offered(self.conn, rid))

    def test_a_posting_in_the_band_at_another_store_is_not_a_twin(self):
        # the tip band is read with the store: a different business's
        # charge that happens to fall in it is not the hold's settlement
        rid = self._refused_hold("hold-t2")
        sync_base.mark_removed(self.conn, ["hold-t2"])
        _post(self.conn, "other-t2", 50.60, "HARBOR HARDWARE",
              date=DAY + dt.timedelta(days=2))
        cands = {c["txn_id"]: c for c in receipt_match.candidates(
            self.conn, self._row(rid))}
        self.assertFalse(cands["other-t2"]["refused_twin"])

    def test_a_refused_hold_that_was_its_own_purchase_lets_the_next_go(self):
        # Monday's coffee is a pending hold the matcher paired the receipt
        # with; the person took it off — the receipt is Tuesday's
        tue = DAY
        mon = DAY - dt.timedelta(days=1)
        rid = _snap(self.conn, "Corner Cafe", 4.50, date=tue)
        _post(self.conn, "mon-hold", 4.50, "CORNER CAFE", pending=True,
              date=mon)
        self.assertEqual(_attached(self.conn, rid), ("mon-hold", "auto"))
        self.assertEqual(receipt_match.unmatch(self.conn, rid), "mon-hold")
        # Tuesday's own charge arrives while Monday's hold is still open:
        # it could be Monday's settlement, so it waits
        _post(self.conn, "tue-post", 4.50, "CORNER CAFE", date=tue)
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertEqual(_attached(self.conn, rid), (None, None))
        # Monday settles on Monday under its own id and the hold drops
        # with no link: Tuesday's charge is Tuesday's
        _post(self.conn, "mon-post", 4.50, "CORNER CAFE", date=mon)
        sync_base.mark_removed(self.conn, ["mon-hold"])
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertEqual(_attached(self.conn, rid), ("tue-post", "auto"))

    def test_a_refused_holds_own_settlement_stays_off(self):
        # the other direction: Monday's settlement is still the charge the
        # person refused, and Tuesday's does not unblock it
        tue = DAY
        mon = DAY - dt.timedelta(days=1)
        rid = _snap(self.conn, "Corner Cafe", 4.50, date=tue)
        _post(self.conn, "mon-hold2", 4.50, "CORNER CAFE", pending=True,
              date=mon)
        self.assertEqual(receipt_match.unmatch(self.conn, rid), "mon-hold2")
        _post(self.conn, "mon-post2", 4.50, "CORNER CAFE", date=mon)
        sync_base.mark_removed(self.conn, ["mon-hold2"])
        cands = {c["txn_id"]: c for c in receipt_match.candidates(
            self.conn, self._row(rid))}
        self.assertTrue(cands["mon-post2"]["refused_twin"])
        receipt_match.match_waiting(self.conn, today=CLOSED)
        self.assertNotEqual(_attached(self.conn, rid)[0], "mon-post2")

    def _row(self, rid):
        return self.conn.execute(
            f"SELECT {receipt_match._WAITING_COLS} FROM receipts"
            " WHERE id = %s", (rid,)).fetchone()


class ARefusalIsCheckedAtThePairingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _enable_llm(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_stale_read_cannot_pair_a_refusal_moved_to_a_new_id(self):
        rid = _snap(self.conn)
        add_txn(self.conn, DAY, 42.17, "MAPLE LEAF BISTRO", txn_id="old-s")
        receipt_match.match_waiting(self.conn)
        self.assertEqual(receipt_match.unmatch(self.conn, rid), "old-s")
        # the matcher reads the receipt (refusing "old-s") ...
        stale = self.conn.execute(
            f"SELECT {receipt_match._WAITING_COLS} FROM receipts"
            " WHERE id = %s", (rid,)).fetchone()
        # ... a reconnect renames the charge and carries the refusal ...
        self.assertTrue(adopt._rewrite_txn_id(self.conn, "old-s", "new-s"))
        # ... and the matcher goes on with what it read
        cands = receipt_match.candidates(
            self.conn, stale, before=receipt_match.AUTO_DAYS_BEFORE,
            after=receipt_match.AUTO_DAYS_AFTER)
        self.assertEqual([c["txn_id"] for c in cands], ["new-s"])
        self.assertIsNone(receipt_match._try_one(
            self.conn, stale, cands, CLOSED))
        self.assertEqual(_attached(self.conn, rid), (None, None))
        # a person may still choose it
        self.assertTrue(receipt_match.match_by_hand(self.conn, rid, "new-s"))

if __name__ == "__main__":
    unittest.main()
