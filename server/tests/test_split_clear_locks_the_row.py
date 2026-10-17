"""Removing a split locks the charge's row before it touches the parts.

Every writer of a row's split — a person setting one, the store matcher
writing a receipt's reading — locks the `transactions` row first and the
`transaction_splits` rows after. A removal that took the parts first and
the charge second would hold the two in the opposite order, and a removal
racing one of those writers would deadlock rather than wait its turn."""

import unittest

import psycopg

from oikonome.db import tenancy
from oikonome.engine import splits

from .util import TODAY, add_txn, make_db, write_config

FOOD, OTHER = "FOOD_AND_DRINK", "GENERAL_MERCHANDISE"


class ClearSplitLockOrder(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.txn = add_txn(self.conn, TODAY.replace(day=5), 120.0,
                           "ACME MARKET", primary=OTHER, account="chk")
        splits.set_split(self.conn, self.txn, [
            {"category": FOOD, "amount": 80}, {"category": OTHER, "amount": 40}])
        self.conn.commit()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def test_the_charge_row_is_locked_while_its_split_is_removed(self):
        probe = tenancy.tenant_connect(self.tid)
        try:
            with self.conn.transaction():
                self.assertEqual(splits.clear_split(self.conn, self.txn),
                                 "user")
                # still inside the removal's transaction: a writer that
                # locks the charge first must wait here, not race ahead
                with self.assertRaises(psycopg.errors.LockNotAvailable):
                    with probe.transaction():
                        probe.execute(
                            "SELECT 1 FROM transactions WHERE id = %s "
                            "FOR UPDATE NOWAIT", (self.txn,))
        finally:
            probe.rollback()
            probe.close()
        self.assertIsNone(splits.source_of(self.conn, self.txn))

    def test_the_charge_is_locked_before_any_part_is_touched(self):
        seen = []
        real = self.conn

        class Recording:
            def execute(self, sql, *a, **kw):
                seen.append(" ".join(str(sql).split()))
                return real.execute(sql, *a, **kw)

            def transaction(self, *a, **kw):
                return real.transaction(*a, **kw)

        splits.clear_split(Recording(), self.txn)
        first_parts = next(i for i, s in enumerate(seen)
                           if "transaction_splits" in s)
        lock = next(i for i, s in enumerate(seen)
                    if "FROM transactions" in s and "FOR UPDATE" in s)
        self.assertLess(lock, first_parts)


if __name__ == "__main__":
    unittest.main()
