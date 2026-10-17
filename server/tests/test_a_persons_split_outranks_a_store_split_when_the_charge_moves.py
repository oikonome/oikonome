"""A person's split follows their charge onto the row it lives on, even
when a store's split already sits there.

A store's parts (source = the store) are its matcher's reading of a
receipt, recomputed from scratch on every run; a person's parts (source
'user') are their answer and outrank them. When the row a person split is
retired and the charge lives on under another id — a hold that posts, a
charge the bank re-numbers — the live row may already carry the receipt's
parts. Blocked by them, the person's split would stay stranded on the
retired row, which no rollup reads: their answer silently gone from
Spending. So the store's parts give way and the person's move.

Between two splits of the same rank nothing changes: the live row's own
split is never mixed with, or replaced by, the retired row's."""

import unittest

from oikonome.engine import splits
from oikonome.sync import base as sync_base
from oikonome.sync.base import Transaction

from .util import TODAY, add_txn, make_db, write_config

FOOD, HOME, OTHER = "FOOD_AND_DRINK", "HOME_IMPROVEMENT", "GENERAL_MERCHANDISE"
MINE = [{"category": FOOD, "amount": 70.0}, {"category": HOME, "amount": 30.0}]
RECEIPT = [("Costco - Home", 60.0), ("Costco - Food & Drink", 40.0)]


def _store_split(conn, txn, parts=RECEIPT, kind="costco"):
    for line, (cat, amt) in enumerate(parts, 1):
        conn.execute(
            "INSERT INTO transaction_splits (txn_id, line, category, amount,"
            " source) VALUES (%s, %s, %s, %s, %s)", (txn, line, cat, amt, kind))


def _parts(conn, txn):
    return [(r["category"], r["amount"], r["source"]) for r in conn.execute(
        "SELECT category, amount, source FROM transaction_splits "
        "WHERE txn_id=%s ORDER BY line", (txn,)).fetchall()]


def _mine():
    return [(p["category"], p["amount"], "user") for p in MINE]


class MoveSplitTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.old = add_txn(self.conn, TODAY.replace(day=5), 100.0,
                           "COSTCO WHSE", primary=OTHER, txn_id="mv_old")
        self.new = add_txn(self.conn, TODAY.replace(day=5), 100.0,
                           "COSTCO WHSE", primary=OTHER, txn_id="mv_new")

    def test_a_persons_split_displaces_a_receipts_on_the_live_row(self):
        splits.set_split(self.conn, self.old, MINE)
        _store_split(self.conn, self.new)
        self.assertEqual(splits.move_split(self.conn, self.old, self.new), 2)
        self.assertEqual(_parts(self.conn, self.new), _mine())
        self.assertEqual(_parts(self.conn, self.old), [])

    def test_a_receipts_split_never_displaces_a_persons(self):
        _store_split(self.conn, self.old)
        splits.set_split(self.conn, self.new, MINE)
        self.assertEqual(splits.move_split(self.conn, self.old, self.new), 0)
        self.assertEqual(_parts(self.conn, self.new), _mine())

    def test_two_receipt_splits_are_never_mixed(self):
        _store_split(self.conn, self.old)
        _store_split(self.conn, self.new, [("Costco - Home", 50.0),
                                           ("Costco - Food & Drink", 50.0)])
        self.assertEqual(splits.move_split(self.conn, self.old, self.new), 0)
        self.assertEqual([p[1] for p in _parts(self.conn, self.new)],
                         [50.0, 50.0])


class SettlementCarryTests(unittest.TestCase):
    """The pending→posted carry in the sync applies the same rank."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)

    def _upsert(self, tid, pending, raw=None):
        sync_base.upsert_transactions(self.conn, [Transaction(
            id=tid, account_id="chk", date=TODAY, amount=100.0,
            name="COSTCO WHSE", pending=pending, category_primary=OTHER,
            raw=raw or {})])

    def test_a_split_made_on_the_hold_displaces_the_receipts_on_the_posted_row(self):
        self._upsert("st:p1", True)
        splits.set_split(self.conn, "st:p1", MINE)
        self._upsert("st:s1", False)
        _store_split(self.conn, "st:s1")
        self._upsert("st:s1", False, {"pending_transaction_id": "st:p1"})
        self.assertEqual(_parts(self.conn, "st:s1"), _mine())
        self.assertEqual(_parts(self.conn, "st:p1"), [])

    def test_a_receipts_split_on_the_hold_never_displaces_a_persons(self):
        self._upsert("st:p2", True)
        _store_split(self.conn, "st:p2")
        self._upsert("st:s2", False)
        splits.set_split(self.conn, "st:s2", MINE)
        self._upsert("st:s2", False, {"pending_transaction_id": "st:p2"})
        self.assertEqual(_parts(self.conn, "st:s2"), _mine())

    def test_the_hold_row_is_locked_before_its_parts_are_carried(self):
        """Every split writer takes the charge's row before its parts; the
        carry moves the hold's parts and only retires the hold at the end,
        so it must hold the hold's row from the start."""
        self._upsert("st:p3", True)
        splits.set_split(self.conn, "st:p3", MINE)
        seen = []
        real = self.conn

        class Recording:
            def execute(self, sql, *a, **kw):
                seen.append((" ".join(str(sql).split()), a))
                return real.execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(real, name)

        sync_base.upsert_transactions(Recording(), [Transaction(
            id="st:s3", account_id="chk", date=TODAY, amount=100.0,
            name="COSTCO WHSE", pending=False, category_primary=OTHER,
            raw={"pending_transaction_id": "st:p3"})])
        self.assertEqual(_parts(self.conn, "st:s3"), _mine())
        carry = next(i for i, (s, _) in enumerate(seen)
                     if s.startswith("UPDATE transaction_splits"))
        lock = next((i for i, (s, a) in enumerate(seen)
                     if s.startswith("SELECT 1 FROM transactions")
                     and "FOR UPDATE" in s and a
                     and "st:p3" in (a[0][0] or [])), None)
        self.assertIsNotNone(lock, "the hold's row was never locked")
        self.assertLess(lock, carry)


if __name__ == "__main__":
    unittest.main()
