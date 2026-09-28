"""Clearing a category the app chose must stay cleared.

A bill's stamp, an Amazon/Costco item match and a store's "Unmatched"
placeholder are all re-derived on a schedule: a clear that only removes the
override is undone the next time that layer runs, within the hour. So the
clear is recorded as the person's own answer of "no category" — the empty
pin (manual_categories, bill_id NULL) over a NULL override still carrying
their kind, which outranks every automatic writer.

Undoing a person's OWN pin is the opposite case and stays a true reset: the
pin and the kind both go, and the automatic layers have the row back.

The NULL override is load-bearing twice over: the row reads through to the
aggregator's live category (an override frozen at today's primary would hide
every later merchant-rule improvement), and nothing may report that the
person "set" a category they explicitly declined to set.
"""

import datetime as dt
import unittest

from oikonome.engine import bills, categories, store_match
from oikonome.web import data
from oikonome.web.api import _category_why

from .util import TODAY, add_bill, add_txn, make_db, write_config


def _row(conn, tid):
    return conn.execute(
        """SELECT t.category_override, t.override_source,
                  COALESCE(t.category_override, t.category_primary) AS effective,
                  m.transaction_id IS NOT NULL AS pinned, m.category AS pin,
                  m.bill_id
             FROM transactions t
             LEFT JOIN manual_categories m ON m.transaction_id = t.id
            WHERE t.id=%s""", (tid,)).fetchone()


def _amazon_order(conn, date, amount, category, key="ord-1"):
    conn.execute(
        """INSERT INTO amazon_orders (dedup_key, account, date, amount, category,
                                      is_refund, payment_method)
           VALUES (%s,'test',%s,%s,%s,0,%s)
           ON CONFLICT (tenant_id, dedup_key) DO NOTHING""",
        (key, date, -abs(amount), category, "Visa ending in 1234"))


class ClearingAnOverride(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.amz = add_txn(self.conn, TODAY - dt.timedelta(days=2), 42.17,
                           "AMAZON.COM*AB12CD", merchant="Amazon",
                           primary="GENERAL_MERCHANDISE")
        self.prime = add_txn(self.conn, TODAY - dt.timedelta(days=4), 14.99,
                             "AMAZON PRIME*XY", merchant="Amazon Prime",
                             primary="GENERAL_MERCHANDISE")
        for tid, name in ((self.amz, "Amazon"), (self.prime, "Amazon Prime")):
            mid = self.conn.execute(
                "INSERT INTO merchants (name, name_source) VALUES (%s,'layer1') "
                "RETURNING id", (name,)).fetchone()["id"]
            self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                              (mid, tid))

    def tearDown(self):
        self.conn.close()

    def _prime_bill(self):
        add_bill(self.conn, "Amazon Prime", 14.99, frequency="MONTHLY",
                 next_due=TODAY, merchant="amazon prime",
                 txn_category="ENTERTAINMENT", merchants=["Amazon Prime"])

    def test_clearing_an_item_match_survives_the_next_store_pass(self):
        _amazon_order(self.conn, TODAY - dt.timedelta(days=3), 42.17, "Books")
        store_match.run_match(self.conn, store_match.AMAZON)
        self.assertEqual(_row(self.conn, self.amz)["category_override"],
                         "Amazon - Books")
        data.clear_category(self.conn, self.amz)
        store_match.run_match(self.conn, store_match.AMAZON)
        r = _row(self.conn, self.amz)
        self.assertEqual((r["category_override"], r["override_source"]),
                         (None, "user"))
        self.assertEqual(r["effective"], "GENERAL_MERCHANDISE")

    def test_clearing_the_unmatched_placeholder_survives_the_next_store_pass(self):
        store_match.run_match(self.conn, store_match.AMAZON)
        self.assertEqual(_row(self.conn, self.prime)["category_override"],
                         "Amazon - Unmatched")
        data.clear_category(self.conn, self.prime)
        store_match.run_match(self.conn, store_match.AMAZON)
        r = _row(self.conn, self.prime)
        self.assertEqual((r["category_override"], r["override_source"]),
                         (None, "user"))

    def test_clearing_a_bill_stamp_twice_still_survives_the_bill(self):
        """The undo path calls the clear on rows it already cleared. A
        second clear must not delete the marker the first one left, or the
        bill takes the row back on its next pass."""
        self._prime_bill()
        bills.apply_txn_categories(self.conn)
        self.assertEqual(_row(self.conn, self.prime)["override_source"], "bill")
        data.clear_category(self.conn, self.prime)
        data.clear_category(self.conn, self.prime)
        bills.apply_txn_categories(self.conn)
        r = _row(self.conn, self.prime)
        self.assertEqual((r["category_override"], r["override_source"]),
                         (None, "user"))
        self.assertEqual(r["pin"], "")
        self.assertIsNone(r["bill_id"])

    def test_a_cleared_row_still_follows_the_aggregators_category(self):
        """The clear removes the override, it does not freeze the category
        underneath: a primary that improves later shows through."""
        self._prime_bill()
        bills.apply_txn_categories(self.conn)
        data.clear_category(self.conn, self.prime)
        self.conn.execute(
            "UPDATE transactions SET category_primary='ENTERTAINMENT', "
            "category_source='rule_user' WHERE id=%s", (self.prime,))
        self.assertEqual(_row(self.conn, self.prime)["effective"],
                         "ENTERTAINMENT")

    def test_a_cleared_row_does_not_claim_the_person_set_a_category(self):
        self._prime_bill()
        bills.apply_txn_categories(self.conn)
        data.clear_category(self.conn, self.prime)
        rows, *_ = data.search_transactions(self.conn, "AMAZON PRIME")
        r = dict(next(x for x in rows if x["id"] == self.prime))
        self.assertFalse(r["override_manual"])
        self.assertIsNone(r["category_override"])
        self.assertNotEqual(_category_why(r), "you set it on this transaction")

    def test_undoing_a_persons_own_pin_hands_the_row_back(self):
        """The mirror case: their pin is theirs to remove entirely, and the
        automatic layers resume — no marker, no kind, no empty pin left."""
        data.set_category(self.conn, self.prime, "TRAVEL")
        data.clear_category(self.conn, self.prime)
        r = _row(self.conn, self.prime)
        self.assertEqual((r["category_override"], r["override_source"]),
                         (None, None))
        self.assertFalse(r["pinned"])
        store_match.run_match(self.conn, store_match.AMAZON)
        self.assertEqual(_row(self.conn, self.prime)["category_override"],
                         "Amazon - Unmatched")

    def test_a_restored_cleared_row_gets_its_kind_back(self):
        """An archive written before overrides carried their kind restores
        the empty pin but no kind. The classifier has to read the pin: a
        cleared row with no kind is one the next bill pass restamps."""
        self._prime_bill()
        bills.apply_txn_categories(self.conn)
        data.clear_category(self.conn, self.prime)
        self.conn.execute("UPDATE transactions SET override_source=NULL")
        self.assertEqual(categories.backfill_override_source(self.conn), 1)
        self.assertEqual(_row(self.conn, self.prime)["override_source"], "user")
        bills.apply_txn_categories(self.conn)
        self.assertIsNone(_row(self.conn, self.prime)["category_override"])


class AStampWhoseBillIsGone(unittest.TestCase):
    """A bill's stamp outlives the bill it came from until the next pass;
    the pass is what takes it back, and it can only take back what is still
    marked as the bill's."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.prime = add_txn(self.conn, TODAY - dt.timedelta(days=4), 14.99,
                             "AMAZON PRIME*XY", merchant="Amazon Prime",
                             primary="GENERAL_MERCHANDISE")
        mid = self.conn.execute(
            "INSERT INTO merchants (name, name_source) VALUES "
            "('Amazon Prime','layer1') RETURNING id").fetchone()["id"]
        self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                          (mid, self.prime))
        add_bill(self.conn, "Amazon Prime", 14.99, frequency="MONTHLY",
                 next_due=TODAY, merchant="amazon prime",
                 txn_category="ENTERTAINMENT", merchants=["Amazon Prime"])
        bills.apply_txn_categories(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_stamp_is_reverted_once_the_bill_is_gone(self):
        self.conn.execute("DELETE FROM bills")
        bills.apply_txn_categories(self.conn)
        r = self.conn.execute(
            "SELECT category_override, override_source FROM transactions "
            "WHERE id=%s", (self.prime,)).fetchone()
        self.assertEqual((r["category_override"], r["override_source"]),
                         (None, None))

    def test_an_orphaned_stamp_classifies_as_a_bills_so_it_stays_revertible(self):
        """The classifier reads the mark, not the bill: a stamp whose bill
        was deleted is still a bill's stamp, and only that kind is the one
        the next pass may revert. Promoting it to a person's pin would
        strand it on the row forever."""
        self.conn.execute("DELETE FROM bills")
        self.conn.execute("UPDATE transactions SET override_source=NULL")
        categories.backfill_override_source(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT override_source FROM transactions WHERE id=%s",
            (self.prime,)).fetchone()["override_source"], "bill")
        bills.apply_txn_categories(self.conn)
        self.assertIsNone(self.conn.execute(
            "SELECT category_override FROM transactions WHERE id=%s",
            (self.prime,)).fetchone()["category_override"])


if __name__ == "__main__":
    unittest.main()
