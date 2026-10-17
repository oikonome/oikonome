"""A store's parts are cut to the amount the charge carried when the
matcher read it, and only for a row the match itself took.

* A sync that posts a new amount between that read and the write must not
  leave parts adding up to a total the bank no longer charges: the row is
  skipped and the next run splits what it now says.
* A row whose whole-row category the store's rank could not displace (an
  override of a kind the store does not outrank) gets no parts either —
  the parts are the store's reading at a finer grain, and would otherwise
  win every rollup over the category the store was not allowed to write.
"""
import unittest
from unittest import mock

from oikonome.engine import costco_match, store_match

from .test_store_receipt_splits_the_charge import add_receipt, parts
from .util import add_txn, make_db, write_config

ITEMS = [{"category": "Food & Drink", "amount": 60},
         {"category": "Home", "amount": 40}]


class ReceiptPartsGuardTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_charge_reposted_mid_run_gets_no_stale_parts(self):
        add_receipt(self.conn, "r1", "2025-07-10", -100.0, "Food & Drink", ITEMS)
        t = add_txn(self.conn, "2025-07-10", 100.0, "COSTCO WHSE")
        real = store_match.record_parts

        def repost_then_cut(items, charge, prefix):
            got = real(items, charge, prefix)
            # the bank posts the final amount while the matcher works
            self.conn.execute(
                "UPDATE transactions SET amount = 120.0 WHERE id = %s", (t,))
            return got

        with mock.patch.object(store_match, "record_parts", repost_then_cut):
            res = costco_match.run_match(self.conn)
        self.assertEqual(res["split"], 0)
        self.assertEqual(parts(self.conn, t), [])

    def test_a_row_the_match_could_not_take_gets_no_parts(self):
        add_receipt(self.conn, "r1", "2025-07-10", -100.0, "Food & Drink", ITEMS)
        t = add_txn(self.conn, "2025-07-10", 100.0, "COSTCO WHSE")
        # an override of a kind the store does not outrank (one an older
        # archive carried in)
        self.conn.execute(
            "UPDATE transactions SET category_override = 'Groceries', "
            "override_source = 'archive' WHERE id = %s", (t,))
        costco_match.run_match(self.conn)
        row = self.conn.execute(
            "SELECT category_override, override_source FROM transactions "
            "WHERE id = %s", (t,)).fetchone()
        self.assertEqual((row["category_override"], row["override_source"]),
                         ("Groceries", "archive"))
        self.assertEqual(parts(self.conn, t), [])

    def test_a_bills_stamp_gives_way_to_the_match_and_its_parts(self):
        add_receipt(self.conn, "r1", "2025-07-10", -100.0, "Food & Drink", ITEMS)
        t = add_txn(self.conn, "2025-07-10", 100.0, "COSTCO WHSE")
        self.conn.execute(
            "UPDATE transactions SET category_override = 'Membership', "
            "override_source = 'bill' WHERE id = %s", (t,))
        costco_match.run_match(self.conn)
        self.assertEqual([(c, a) for c, a, _ in parts(self.conn, t)],
                         [("Costco - Food & Drink", 60.0), ("Costco - Home", 40.0)])


if __name__ == "__main__":
    unittest.main()
