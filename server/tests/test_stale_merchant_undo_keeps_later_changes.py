"""A merchant-category Undo reverses its own write and nothing newer.

The snapshot lives in the client with no expiry. If another member (or the
other app) sets the same merchant to something else afterwards, a late
press of the first Undo used to delete every user rule under the merchant,
put back the rules from before the FIRST write, and reset every snapshotted
row — silently wiping the later change. Rows and rules that no longer hold
what the undone write set now belong to the later change and stay put."""

import unittest

from oikonome.web import data

from .test_canonical_rules import canon, cat
from .test_llm_categorize import add_raw_txn
from .util import make_db


def _rule(conn, merchant):
    r = conn.execute("SELECT category_primary FROM merchant_categories "
                     "WHERE merchant=%s AND source='user'",
                     (merchant,)).fetchone()
    return r and r["category_primary"]


class StaleMerchantUndoKeepsLaterChanges(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        canon(self.conn, "NORTHWIND COFFEE", "Northwind Coffee")
        add_raw_txn(self.conn, "a", "2025-07-01", 10, "NORTHWIND COFFEE",
                    primary="PERSONAL_CARE", raw={})
        add_raw_txn(self.conn, "b", "2025-07-02", 12, "NORTHWIND COFFEE",
                    primary="PERSONAL_CARE", raw={})

    def tearDown(self):
        self.conn.close()

    def test_a_late_undo_leaves_a_newer_write_alone(self):
        first = data.set_merchant_category(self.conn, "NORTHWIND COFFEE",
                                           "ENTERTAINMENT")
        data.set_merchant_category(self.conn, "NORTHWIND COFFEE", "SHOPPING")
        n = data.undo_merchant_category(self.conn, first["undo"])
        self.assertEqual(n, 0)
        self.assertEqual(cat(self.conn, "a"), "SHOPPING")
        self.assertEqual(cat(self.conn, "b"), "SHOPPING")
        self.assertEqual(_rule(self.conn, "Northwind Coffee"), "SHOPPING")

    def test_rows_moved_since_are_skipped_the_rest_restored(self):
        first = data.set_merchant_category(self.conn, "NORTHWIND COFFEE",
                                           "ENTERTAINMENT")
        # one row re-filed since, by something else (the categorizer, say)
        self.conn.execute("UPDATE transactions SET category_primary="
                          "'GROCERIES' WHERE id='b'")
        n = data.undo_merchant_category(self.conn, first["undo"])
        self.assertEqual(n, 1)
        self.assertEqual(cat(self.conn, "a"), "PERSONAL_CARE")
        self.assertEqual(cat(self.conn, "b"), "GROCERIES")
        # the rule still said what the write set, so it is undone
        self.assertIsNone(_rule(self.conn, "Northwind Coffee"))

    def test_a_prompt_undo_still_restores_everything(self):
        first = data.set_merchant_category(self.conn, "NORTHWIND COFFEE",
                                           "ENTERTAINMENT")
        self.assertEqual(data.undo_merchant_category(self.conn,
                                                     first["undo"]), 2)
        self.assertEqual(cat(self.conn, "a"), "PERSONAL_CARE")
        self.assertIsNone(_rule(self.conn, "Northwind Coffee"))


if __name__ == "__main__":
    unittest.main()
