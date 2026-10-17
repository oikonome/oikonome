"""An order's item with an empty or null title is never listed to the
model, so no answer can stamp it: the queue must not count it, or the order
is sent to the model again every night, for good, at a paid call each."""
import unittest

from oikonome.engine import llm_categorize

from .test_amazon_items_get_their_own_categories import add_order
from .util import make_db


class UntitledItemTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_an_answered_order_with_an_untitled_item_is_not_queued(self):
        add_order(self.conn, "blank", [
            {"title": "lamp", "price": 20.0, "category": "Home"},
            {"title": "oat bars", "price": 10.0, "category": "Food & Drink"},
            {"title": "", "price": 1.0}], category="Home", source="llm")
        add_order(self.conn, "null", [
            {"title": "lamp", "price": 20.0, "category": "Home"},
            {"title": None, "price": 1.0},
            {"title": None, "price": 2.0}], category="Home", source="llm")
        self.assertEqual(llm_categorize.amazon_pending(self.conn), [])

    def test_an_untitled_item_does_not_make_a_single_item_order_look_mixed(self):
        add_order(self.conn, "one", [{"title": "lamp", "price": 20.0},
                                     {"title": ""}],
                  category="Home", source="llm")
        self.assertEqual(llm_categorize.amazon_pending(self.conn), [])

    def test_titled_items_still_unanswered_still_queue(self):
        add_order(self.conn, "mixed", [{"title": "lamp", "price": 20.0},
                                       {"title": "oat bars", "price": 10.0},
                                       {"title": ""}],
                  category="Home", source="llm")
        self.assertEqual([r["dedup_key"] for r in
                          llm_categorize.amazon_pending(self.conn)], ["mixed"])


if __name__ == "__main__":
    unittest.main()
