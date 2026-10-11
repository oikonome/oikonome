"""Resolving an import holds a fixed number of locks, however many names it mints.

A file import resolves all of its rows in one pass that commits once, at
the end. If minting each new merchant took a lock of its own, the pass
would hold one lock-table entry per new payee until that commit, and a
large enough import would exhaust the server's shared lock table for every
household on it. Every mint serialises on the household's one identity
lock instead.
"""
import datetime as dt
import unittest

from oikonome.engine import merchant_identity
from oikonome.sync import base

from .util import make_db

DAY = dt.date(2026, 3, 1)
_FIRST = ("Amber", "Birch", "Cedar", "Dune", "Ember", "Fern", "Grove",
          "Harbor")
_SECOND = ("Lantern", "Meadow", "Orchard", "Pebble", "Quill", "Ridge",
           "Saddle", "Thistle")


class ResolveLockCountTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _locks_held_resolving(self, n_names: int, tag: str) -> int:
        rows = [base.Transaction(
            id=f"csv:{tag}{i}", account_id="chk", date=DAY, amount=1.0 + i,
            name=f"{tag} {_FIRST[i % 8]} {_SECOND[i // 8]} Bakery")
            for i in range(n_names)]
        with self.conn.transaction():
            # the import's own transaction, as when a caller resolves inside
            # one: everything resolve takes is still held here
            base.upsert_transactions(self.conn, rows)
            merchant_identity.resolve(
                self.conn, txn_ids=[r.id for r in rows])
            return self.conn.execute(
                "SELECT count(*) AS n FROM pg_locks WHERE locktype='advisory'"
                " AND pid = pg_backend_pid()").fetchone()["n"]

    def test_lock_count_does_not_grow_with_new_names(self):
        few = self._locks_held_resolving(2, "Sparse")
        many = self._locks_held_resolving(40, "Plenty")
        self.assertEqual(few, many)
        self.assertLessEqual(many, 2)

    def test_each_new_name_still_gets_one_merchant(self):
        self._locks_held_resolving(5, "Single")
        n = self.conn.execute(
            "SELECT count(*) AS n FROM merchants WHERE name ILIKE 'single %%'"
        ).fetchone()["n"]
        self.assertEqual(n, 5)


if __name__ == "__main__":
    unittest.main()
