"""A person filing a store-split charge while a receipt push re-matches it
waits for the matcher instead of deadlocking with it.

Every writer of a row's split takes the `transactions` row first and the
`transaction_splits` rows after (splits.set_split, splits.clear_split, the
store matcher). Filing or clearing a charge drops the store's parts on it;
done before the row was locked, the person held the parts while the
matcher held the row and wanted the parts — Postgres broke the cycle by
killing one side, and when that was the person their edit was lost to a
server error."""

import threading
import time
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import costco_match
from oikonome.web import data

from .test_store_receipt_splits_the_charge import add_receipt, parts
from .util import add_txn, make_db, write_config

ITEMS = [{"category": "Food & Drink", "amount": 60},
         {"category": "Home", "amount": 40}]


class PersonEditVersusMatcherTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        add_receipt(self.conn, "r1", "2025-07-10", -100.0, "Food & Drink", ITEMS)
        self.txn = add_txn(self.conn, "2025-07-10", 100.0, "COSTCO WHSE")
        costco_match.run_match(self.conn)
        self.assertEqual(len(parts(self.conn, self.txn)), 2)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]

    def _race(self, edit):
        """Run `edit`, starting a matcher push the moment the edit has
        dropped the store's parts, and hold the edit open long enough for
        Postgres's deadlock detector (deadlock_timeout, 1 s by default) to
        fire if the two hold their locks in opposite orders."""
        errors, threads = {}, []

        def matcher():
            other = tenancy.tenant_connect(self.tid)
            try:
                costco_match.run_match(other)
            except Exception as e:                       # noqa: BLE001
                errors["matcher"] = type(e).__name__
            finally:
                other.close()

        real = data._drop_store_parts

        def drop_then_race(conn, txn_id):
            real(conn, txn_id)
            th = threading.Thread(target=matcher)
            threads.append(th)
            th.start()
            time.sleep(1.5)

        with mock.patch.object(data, "_drop_store_parts", drop_then_race):
            try:
                edit()
            except Exception as e:                       # noqa: BLE001
                errors["edit"] = type(e).__name__
        for th in threads:
            th.join(30)
        return errors

    def test_filing_the_charge_waits_for_the_matcher(self):
        errors = self._race(
            lambda: data.set_category(self.conn, self.txn, "Groceries"))
        self.assertEqual(errors, {})
        self.assertEqual(parts(self.conn, self.txn), [])
        row = self.conn.execute(
            "SELECT category_override, override_source FROM transactions "
            "WHERE id=%s", (self.txn,)).fetchone()
        self.assertEqual((row["category_override"], row["override_source"]),
                         ("Groceries", "user"))

    def test_clearing_the_charge_waits_for_the_matcher(self):
        errors = self._race(lambda: data.clear_category(self.conn, self.txn))
        self.assertEqual(errors, {})
        self.assertEqual(parts(self.conn, self.txn), [])
        row = self.conn.execute(
            "SELECT category_override, override_source FROM transactions "
            "WHERE id=%s", (self.txn,)).fetchone()
        self.assertEqual((row["category_override"], row["override_source"]),
                         (None, "user"))


if __name__ == "__main__":
    unittest.main()
