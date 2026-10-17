"""A store's split is the matcher's reading of a receipt, not a person's
answer — the person's own doors treat it that way.

A store matcher carves a matched charge into the categories its receipt's
items name (source = the store) and recomputes those parts from scratch
on every run; a person's split (source = 'user') outranks them. So:

- a category rename/merge rewrites only the person's parts. A store split
  holding the old name is dropped for the matcher to read again: renamed
  in place, a merge could fold it to one part and turn it into a pin —
  the row permanently the person's, from an edit they never made.
- a person filing (or clearing) a charge that carries store parts drops
  those parts: the parts outrank the row's category in every rollup, so
  left in place the person's answer read as ignored until the next run.
- a mailed "file under" button is not refused as "split since the email
  went out" for a store's parts — that sentence is about a person's.
"""

import datetime as dt
import unittest

from oikonome.notify import mailact
from oikonome.web import data

from .util import TODAY, add_txn, make_db, write_config

FOOD, HOME = "FOOD_AND_DRINK", "HOME_IMPROVEMENT"


def _store_split(conn, txn, parts, kind="amazon"):
    for line, (cat, amt) in enumerate(parts, 1):
        conn.execute(
            "INSERT INTO transaction_splits (txn_id, line, category, amount,"
            " source) VALUES (%s, %s, %s, %s, %s)", (txn, line, cat, amt, kind))
    conn.execute("UPDATE transactions SET category_override=%s, "
                 "override_source=%s WHERE id=%s", (parts[0][0], kind, txn))


class StoreSplitTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.txn = add_txn(self.conn, TODAY.replace(day=5), 100.0,
                           "AMAZON MKTPLACE", account="chk")

    def _parts(self, txn=None):
        return [(r["category"], r["amount"], r["source"]) for r in self.conn.execute(
            "SELECT category, amount, source FROM transaction_splits "
            "WHERE txn_id=%s ORDER BY line", (txn or self.txn,)).fetchall()]

    def _pin(self, txn=None):
        return self.conn.execute(
            "SELECT category, bill_id FROM manual_categories "
            "WHERE transaction_id=%s", (txn or self.txn,)).fetchone()

    def test_a_merge_that_folds_a_store_split_does_not_pin_the_row(self):
        _store_split(self.conn, self.txn, [("Hobby", 60.0), (HOME, 40.0)])
        data.rename_category(self.conn, "Hobby", HOME)
        self.assertIsNone(self._pin(), "a store split became a person's pin")
        self.assertEqual(self._parts(), [])
        src = self.conn.execute("SELECT override_source FROM transactions "
                                "WHERE id=%s", (self.txn,)).fetchone()
        self.assertEqual(src["override_source"], "amazon")

    def test_a_rename_leaves_store_parts_to_the_matcher_and_renames_the_persons(self):
        from oikonome.engine import splits
        mine = add_txn(self.conn, TODAY.replace(day=6), 50.0, "ACME", account="chk")
        splits.set_split(self.conn, mine, [{"category": "Hobby", "amount": 30},
                                           {"category": FOOD, "amount": 20}])
        _store_split(self.conn, self.txn, [("Hobby", 60.0), (FOOD, 40.0)])
        r = data.rename_category(self.conn, "Hobby", "Crafts")
        self.assertEqual(self._parts(mine), [("Crafts", 30.0, "user"),
                                             (FOOD, 20.0, "user")])
        self.assertEqual(self._parts(), [])
        self.assertEqual(r["splits"], 1)

    def test_filing_a_store_split_charge_drops_the_store_parts(self):
        _store_split(self.conn, self.txn, [(FOOD, 60.0), (HOME, 40.0)])
        data.set_category(self.conn, self.txn, "GENERAL_MERCHANDISE")
        self.assertEqual(self._parts(), [])

    def test_clearing_a_store_split_charge_drops_the_store_parts(self):
        _store_split(self.conn, self.txn, [(FOOD, 60.0), (HOME, 40.0)])
        data.clear_category(self.conn, self.txn)
        self.assertEqual(self._parts(), [])

    def test_filing_keeps_a_persons_split(self):
        from oikonome.engine import splits
        splits.set_split(self.conn, self.txn, [{"category": FOOD, "amount": 60},
                                               {"category": HOME, "amount": 40}])
        data.set_category(self.conn, self.txn, "GENERAL_MERCHANDISE")
        self.assertEqual(len(self._parts()), 2)

    def test_a_mailed_button_is_not_refused_as_split_for_store_parts(self):
        _store_split(self.conn, self.txn, [(FOOD, 60.0), (HOME, 40.0)])
        exp = (dt.datetime.now(dt.timezone.utc)
               + dt.timedelta(days=mailact.TTL_DAYS)).timestamp()
        tok = {"value": "GENERAL_MERCHANDISE", "target": self.txn, "exp": exp}
        row = self.conn.execute(
            "SELECT category_override, category_primary FROM transactions "
            "WHERE id=%s", (self.txn,)).fetchone()
        moot = mailact._cat_moot(self.conn, tok, {"payee": "Amazon", **row})
        self.assertIsNotNone(moot)
        self.assertNotIn("was split", moot["sentence"])

    def test_a_rename_locks_the_split_charges_before_their_parts(self):
        """Every split writer takes the charge's row before its parts (the
        store matcher among them); a rename folding parts and then pinning
        the charge must not hold the two the other way round."""
        _store_split(self.conn, self.txn, [("Hobby", 60.0), (HOME, 40.0)])
        seen = []
        real = self.conn

        class Recording:
            def execute(self, sql, *a, **kw):
                seen.append(" ".join(str(sql).split()))
                return real.execute(sql, *a, **kw)

            def __getattr__(self, name):
                return getattr(real, name)

        data.rename_category(Recording(), "Hobby", HOME)
        first_parts = next(i for i, s in enumerate(seen)
                           if s.startswith(("DELETE FROM transaction_splits",
                                            "UPDATE transaction_splits",
                                            "WITH o AS")))
        lock = next((i for i, s in enumerate(seen)
                     if s.startswith("SELECT 1 FROM transactions")
                     and "transaction_splits" in s and "FOR UPDATE" in s),
                    None)
        self.assertIsNotNone(lock, "the split charges were never locked")
        self.assertLess(lock, first_parts)


if __name__ == "__main__":
    unittest.main()
