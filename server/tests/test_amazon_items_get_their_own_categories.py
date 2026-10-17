"""The Amazon pass asks about an order's items one by one when it lists
several, so the matcher can split the charge by what was bought; an answer
for the order alone is still read, and every listed item is stamped so a
row is never asked twice for what the model would not say."""
import os
import unittest
from unittest import mock

from oikonome.engine import llm_categorize
from oikonome.engine.compat import as_date, jsonb

from .test_llm_categorize import ENV
from .test_model_answers_are_read_through_their_wrapping import _reply
from .util import make_db

CATS = ["Apparel", "Food & Drink", "Home", "Shopping"]


def add_order(conn, dedup_key, items, category="Shopping", source="rules",
              date="2025-07-08"):
    conn.execute(
        """INSERT INTO amazon_orders (dedup_key, account, date, amount, payee,
               category, category_source, is_refund, items_json)
           VALUES (%s,'acct',%s,-30,'Amazon',%s,%s,0,%s)""",
        (dedup_key, as_date(date), category, source, jsonb(items)))


class PromptAndAnswerTests(unittest.TestCase):
    def test_several_items_are_lettered_and_answered_per_item(self):
        rows = [{"items_json": [{"title": "lamp", "price": 20.0},
                                {"title": "oat bars", "price": 10.0}],
                 "memo": "", "seller": ""},
                {"items_json": [{"title": "socks"}], "memo": "", "seller": ""}]
        self.assertEqual(llm_categorize._amazon_row_text(rows[0]),
                         "(a) lamp; (b) oat bars")
        self.assertEqual(llm_categorize._amazon_row_text(rows[1]), "socks")
        with mock.patch.dict(os.environ, ENV):
            out = llm_categorize._classify_amazon_batch(
                rows, CATS, transport=_reply(
                    '{"1": ["Home", "food & drink"], "2": "Apparel"}'))
        self.assertEqual(out, {1: ["Home", "Food & Drink"], 2: "Apparel"})
        # a list naming a category off the list is no answer
        with mock.patch.dict(os.environ, ENV):
            out = llm_categorize._classify_amazon_batch(
                rows, CATS, transport=_reply('{"1": ["Home", "Weapons"]}'))
        self.assertEqual(out, {})

    def test_the_order_takes_the_category_carrying_most_of_the_price(self):
        row = {"items_json": [{"title": "lamp", "price": 20.0},
                              {"title": "oat bars", "price": 10.0},
                              {"title": "tea", "price": 15.0}],
               "memo": "", "seller": ""}
        cat, items = llm_categorize._amazon_answer(
            row, ["Home", "Food & Drink", "Food & Drink"])
        self.assertEqual(cat, "Food & Drink")
        self.assertEqual([i["category"] for i in items],
                         ["Home", "Food & Drink", "Food & Drink"])
        # an answer for the order only stamps the items null: asked, not said
        cat, items = llm_categorize._amazon_answer(row, "Home")
        self.assertEqual(cat, "Home")
        self.assertEqual([i["category"] for i in items], [None, None, None])
        # a list of the wrong length is read as the order's answer
        cat, items = llm_categorize._amazon_answer(row, ["Home", "Apparel"])
        self.assertEqual(cat, "Home")
        self.assertEqual([i["category"] for i in items], [None, None, None])
        # items past the listed cap were never asked about: stamped null,
        # so the order does not queue again for an answer never requested
        row = {"items_json": [{"title": f"t{i}", "price": 1.0} for i in range(8)],
               "memo": "", "seller": ""}
        cat, items = llm_categorize._amazon_answer(row, ["Home"] * 6)
        self.assertEqual(len(items), 8)
        self.assertEqual([i["category"] for i in items], ["Home"] * 6 + [None, None])


class PendingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_a_classified_order_with_unasked_items_queues_once(self):
        # newest first is the batch order; the assignable categories are
        # the household's own (the ones its orders already carry)
        add_order(self.conn, "mixed", [{"title": "lamp", "price": 20.0},
                                       {"title": "oat bars", "price": 10.0}],
                  category="Home", source="llm", date="2025-07-09")
        add_order(self.conn, "single", [{"title": "lamp", "price": 20.0}],
                  category="Food & Drink", source="llm")
        add_order(self.conn, "fresh", [{"title": "x"}], category="", source="rules")
        # eight items: six listed, two never asked about
        add_order(self.conn, "long", [{"title": f"t{i}", "price": 1.0}
                                      for i in range(8)],
                  category="Home", source="llm", date="2025-07-01")
        pending = {r["dedup_key"] for r in llm_categorize.amazon_pending(self.conn)}
        self.assertEqual(pending, {"mixed", "fresh", "long"})
        # the pass stamps what the model said (or null) — asked, done
        with mock.patch.dict(os.environ, ENV):
            llm_categorize._classify_amazon(
                self.conn, limit=None, batch_size=20, dry_run=False,
                stats={"unparseable": 0, "items_classified": 0},
                transport=_reply('{"1": ["Home", "Food & Drink"], "2": "Shopping", '
                                 '"3": ["Home", "Home", "Home", "Home", "Home", "Home"]}'))
        pending = {r["dedup_key"] for r in llm_categorize.amazon_pending(self.conn)}
        self.assertEqual(pending, set())
        row = self.conn.execute(
            "SELECT category, items_json FROM amazon_orders WHERE dedup_key='mixed'"
        ).fetchone()
        self.assertEqual(row["category"], "Home")
        self.assertEqual([i["category"] for i in row["items_json"]],
                         ["Home", "Food & Drink"])
