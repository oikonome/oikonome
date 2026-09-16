"""Four hands write category_override; each keeps its rank.

A person's pin, a store's item match, a bill's stamp on the rows it
matches, and a store's "Unmatched" placeholder all write category_override.
The kind of an override is a fact on the row (override_source), each
writer states its own, and a writer never overwrites a kind that outranks
it: a person > an item match > a bill. "Unmatched" is the absence of an
item match and yields like an empty slot.
"""

import datetime as dt
import unittest

from oikonome.engine import bills, categories, store_match
from oikonome.web import data
from oikonome.web.api import _category_why

from .util import TODAY, add_bill, add_txn, make_db, write_config


def _row(conn, tid):
    return conn.execute(
        "SELECT category_override, override_source FROM transactions WHERE id=%s",
        (tid,)).fetchone()


def _amazon_order(conn, date, amount, category, key="ord-1"):
    conn.execute(
        """INSERT INTO amazon_orders (dedup_key, account, date, amount, category,
                                      is_refund, payment_method)
           VALUES (%s,'test',%s,%s,%s,0,%s)
           ON CONFLICT (tenant_id, dedup_key) DO NOTHING""",
        (key, date, -abs(amount), category, "Visa ending in 1234"))


class OverrideRank(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.amz = add_txn(self.conn, TODAY - dt.timedelta(days=2), 42.17,
                           "AMAZON.COM*AB12CD", merchant="Amazon",
                           primary="GENERAL_MERCHANDISE")
        self.prime = add_txn(self.conn, TODAY - dt.timedelta(days=4), 14.99,
                             "AMAZON PRIME*XY", merchant="Amazon Prime",
                             primary="GENERAL_MERCHANDISE")
        # a bill's identity is merchant rows, so the rows must have them
        for tid, name in ((self.amz, "Amazon"), (self.prime, "Amazon Prime")):
            mid = self.conn.execute(
                "INSERT INTO merchants (name, name_source) VALUES (%s,'layer1') "
                "RETURNING id", (name,)).fetchone()["id"]
            self.conn.execute("UPDATE transactions SET merchant_id=%s WHERE id=%s",
                              (mid, tid))

    def tearDown(self):
        self.conn.close()

    def test_rank_table(self):
        self.assertTrue(categories.override_outranks(None, "bill"))
        self.assertTrue(categories.override_outranks("bill", "bill"))
        self.assertTrue(categories.override_outranks("bill", "amazon"))
        self.assertTrue(categories.override_outranks("bill", "user"))
        self.assertFalse(categories.override_outranks("user", "bill"))
        self.assertFalse(categories.override_outranks("user", "amazon"))
        self.assertFalse(categories.override_outranks("amazon", "bill",
                                                      "Amazon - Books"))
        # the placeholder yields to a bill and to a real match
        self.assertTrue(categories.override_outranks("amazon", "bill",
                                                     "Amazon - Unmatched"))
        self.assertTrue(categories.override_outranks("amazon", "amazon",
                                                     "Amazon - Unmatched"))
        self.assertTrue(categories.override_outranks("amazon", "user",
                                                     "Amazon - Unmatched"))

    def test_a_pin_states_its_kind_and_a_store_match_leaves_it(self):
        data.set_category(self.conn, self.amz, "ENTERTAINMENT")
        self.assertEqual(dict(_row(self.conn, self.amz)),
                         {"category_override": "ENTERTAINMENT",
                          "override_source": "user"})
        _amazon_order(self.conn, TODAY - dt.timedelta(days=3), 42.17, "Books")
        store_match.run_match(self.conn, store_match.AMAZON)
        self.assertEqual(dict(_row(self.conn, self.amz)),
                         {"category_override": "ENTERTAINMENT",
                          "override_source": "user"})
        # the un-pinned Prime charge got the placeholder, and says so
        self.assertEqual(dict(_row(self.conn, self.prime)),
                         {"category_override": "Amazon - Unmatched",
                          "override_source": "amazon"})

    def test_an_item_match_outranks_a_bills_stamp_both_ways(self):
        add_bill(self.conn, "Amazon", 42.0, frequency="MONTHLY", next_due=TODAY,
                 merchant="amazon", txn_category="GENERAL_MERCHANDISE",
                 merchants=["Amazon"])
        bills.apply_txn_categories(self.conn)
        self.assertEqual(_row(self.conn, self.amz)["override_source"], "bill")
        # the store's record arrives after the bill stamped the row
        _amazon_order(self.conn, TODAY - dt.timedelta(days=3), 42.17, "Books")
        store_match.run_match(self.conn, store_match.AMAZON)
        self.assertEqual(dict(_row(self.conn, self.amz)),
                         {"category_override": "Amazon - Books",
                          "override_source": "amazon"})
        # and the bill's next pass does not take it back
        bills.apply_txn_categories(self.conn)
        self.assertEqual(_row(self.conn, self.amz)["override_source"], "amazon")
        # archiving the bill reverts only the bill's own marks
        self.conn.execute("UPDATE bills SET active=0")
        bills.apply_txn_categories(self.conn)
        self.assertEqual(_row(self.conn, self.amz)["override_source"], "amazon")

    def test_a_bill_stamp_replaces_the_stores_placeholder(self):
        store_match.run_match(self.conn, store_match.AMAZON)
        self.assertEqual(_row(self.conn, self.prime)["category_override"],
                         "Amazon - Unmatched")
        add_bill(self.conn, "Amazon Prime", 14.99, frequency="MONTHLY",
                 next_due=TODAY, merchant="amazon prime",
                 txn_category="ENTERTAINMENT", merchants=["Amazon Prime"])
        bills.apply_txn_categories(self.conn)
        self.assertEqual(dict(_row(self.conn, self.prime)),
                         {"category_override": "ENTERTAINMENT",
                          "override_source": "bill"})
        # and the next store pass does not put the placeholder back
        store_match.run_match(self.conn, store_match.AMAZON)
        self.assertEqual(_row(self.conn, self.prime)["override_source"], "bill")

    def test_clearing_a_pin_clears_its_kind(self):
        data.set_category(self.conn, self.amz, "ENTERTAINMENT")
        data.clear_category(self.conn, self.amz)
        self.assertEqual(dict(_row(self.conn, self.amz)),
                         {"category_override": None, "override_source": None})

    def test_clearing_an_automatic_override_keeps_the_persons_kind(self):
        """The mirror of the case above: removing a category the app chose
        is itself the person's answer, and the kind has to stay on the row
        or the layer that chose it writes the same thing again. What it
        outranks is checked in test_clearing_an_override_stays_cleared."""
        store_match.run_match(self.conn, store_match.AMAZON)
        self.assertEqual(_row(self.conn, self.prime)["override_source"],
                         "amazon")
        data.clear_category(self.conn, self.prime)
        self.assertEqual(dict(_row(self.conn, self.prime)),
                         {"category_override": None, "override_source": "user"})

    def test_the_why_names_the_hand_that_won(self):
        add_bill(self.conn, "Amazon", 42.0, frequency="MONTHLY", next_due=TODAY,
                 merchant="amazon", txn_category="GENERAL_MERCHANDISE",
                 merchants=["Amazon"])
        bills.apply_txn_categories(self.conn)
        _amazon_order(self.conn, TODAY - dt.timedelta(days=3), 42.17, "Books")
        store_match.run_match(self.conn, store_match.AMAZON)
        rows, *_ = data.search_transactions(self.conn, "AMAZON.COM")
        r = dict(next(x for x in rows if x["id"] == self.amz))
        self.assertEqual(_category_why(r), "matched to an Amazon order")
        data.set_category(self.conn, self.amz, "ENTERTAINMENT")
        rows, *_ = data.search_transactions(self.conn, "AMAZON.COM")
        r = dict(next(x for x in rows if x["id"] == self.amz))
        self.assertEqual(_category_why(r), "you set it on this transaction")

    def test_an_archive_without_kinds_is_classified_on_arrival(self):
        """Rows restored from an older backup carry overrides but no kind;
        the override's own prefix implies it."""
        data.set_category(self.conn, self.amz, "ENTERTAINMENT")
        self.conn.execute(
            "UPDATE transactions SET category_override='Amazon - Unmatched' "
            "WHERE id=%s", (self.prime,))
        self.conn.execute("UPDATE transactions SET override_source=NULL")
        self.assertEqual(categories.backfill_override_source(self.conn), 2)
        self.assertEqual(_row(self.conn, self.amz)["override_source"], "user")
        self.assertEqual(_row(self.conn, self.prime)["override_source"], "amazon")


if __name__ == "__main__":
    unittest.main()
