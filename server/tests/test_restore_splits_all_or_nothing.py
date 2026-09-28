"""A restored split lands whole or not at all.

A split is one statement about one charge: these parts, adding up to it.
Writing an archive's parts line by line with ON CONFLICT DO NOTHING would
mix two different splits of the same charge into parts summing past it;
the sync's stale-split pass would then delete every part, the current
split included. An archived split that never added up to its charge would
likewise be dropped silently.

So per transaction: an existing split is kept as-is and the archive's is
skipped; otherwise the archive's parts must validate against the restored
charge as a whole, or none of them are written.
"""

import csv
import io
import unittest
import zipfile

from oikonome.engine import splits
from oikonome.sync import restore

from .util import TODAY, add_txn, make_db, write_config

FOOD, OTHER, HOME = "FOOD_AND_DRINK", "GENERAL_MERCHANDISE", "HOME_IMPROVEMENT"


def _splits_zip(rows: list[dict]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["txn_id", "line", "category",
                                          "amount"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
        z.writestr("transaction_splits.csv", s.getvalue())
    return buf.getvalue()


class RestoreSplitGroupTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.addCleanup(self.conn.close)
        self.txn = add_txn(self.conn, TODAY.replace(day=5), 100.0,
                           "ACME MARKET", primary=OTHER, account="chk")

    def test_an_existing_split_is_kept_whole_not_merged_line_by_line(self):
        splits.set_split(self.conn, self.txn, [
            {"category": FOOD, "amount": 60}, {"category": OTHER, "amount": 40}])
        counts = restore.restore_zip(self.conn, _splits_zip([
            {"txn_id": self.txn, "line": 1, "category": OTHER, "amount": 50},
            {"txn_id": self.txn, "line": 2, "category": FOOD, "amount": 20},
            {"txn_id": self.txn, "line": 3, "category": HOME, "amount": 30},
        ]))
        self.assertEqual(splits.for_txn(self.conn, self.txn),
                         [{"category": FOOD, "amount": 60.0},
                          {"category": OTHER, "amount": 40.0}])
        self.assertEqual(splits.stale(self.conn), [],
                         "nothing is left for the sync to delete")
        self.assertNotIn("transaction_splits", counts)
        self.assertEqual(counts.get("already_present"), 3)

    def test_a_split_that_does_not_add_up_is_not_restored_at_all(self):
        counts = restore.restore_zip(self.conn, _splits_zip([
            {"txn_id": self.txn, "line": 1, "category": FOOD, "amount": 60},
            {"txn_id": self.txn, "line": 2, "category": OTHER, "amount": 70},
        ]))
        self.assertEqual(splits.for_txn(self.conn, self.txn), [])
        self.assertNotIn("transaction_splits", counts)

    def test_a_repeated_category_is_not_restored_at_all(self):
        restore.restore_zip(self.conn, _splits_zip([
            {"txn_id": self.txn, "line": 1, "category": FOOD, "amount": 50},
            {"txn_id": self.txn, "line": 2, "category": FOOD, "amount": 50},
        ]))
        self.assertEqual(splits.for_txn(self.conn, self.txn), [])

    def test_a_valid_split_lands_whole_in_line_order(self):
        counts = restore.restore_zip(self.conn, _splits_zip([
            {"txn_id": self.txn, "line": 2, "category": OTHER, "amount": 25},
            {"txn_id": self.txn, "line": 1, "category": FOOD, "amount": 75},
        ]))
        self.assertEqual(splits.for_txn(self.conn, self.txn),
                         [{"category": FOOD, "amount": 75.0},
                          {"category": OTHER, "amount": 25.0}])
        self.assertEqual(counts["transaction_splits"], 2)
        # restoring the same archive again is a no-op, not a merge
        again = restore.restore_zip(self.conn, _splits_zip([
            {"txn_id": self.txn, "line": 1, "category": FOOD, "amount": 75},
            {"txn_id": self.txn, "line": 2, "category": OTHER, "amount": 25},
        ]))
        self.assertNotIn("transaction_splits", again)

    def test_a_split_on_a_row_the_split_endpoint_would_refuse_is_skipped(self):
        """An archive can hold a split on a row that is no longer spending
        (a refund, a transfer). Restoring it would count parts that the next
        sync's stale sweep then deletes; restore applies the same
        spending-row rule as the split endpoint and skips them."""
        refund = add_txn(self.conn, TODAY.replace(day=6), -100.0,
                         "ACME MARKET REFUND", primary=OTHER, account="chk")
        counts = restore.restore_zip(self.conn, _splits_zip([
            {"txn_id": refund, "line": 1, "category": FOOD, "amount": -60},
            {"txn_id": refund, "line": 2, "category": OTHER, "amount": -40},
        ]))
        self.assertEqual(splits.for_txn(self.conn, refund), [])
        self.assertNotIn("transaction_splits", counts)
        self.assertEqual(counts.get("already_present"), 2)


if __name__ == "__main__":
    unittest.main()
