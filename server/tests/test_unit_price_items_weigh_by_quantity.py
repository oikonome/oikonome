"""An online order lists each item at its UNIT price with a quantity; a
warehouse receipt prints each line's total. Both the split a matched
charge takes from its record and the category the order is filed under
must weigh a line by what it cost in all — six of a $2 item weigh $12, not
$2 — or a cheap item bought in bulk loses its share of the charge to one
dearer item bought once."""
import unittest

from oikonome.engine import llm_categorize, store_match

FOOD, HOME = "Amazon - Food & Drink", "Amazon - Home"


class RecordPartsTests(unittest.TestCase):
    def test_a_unit_price_is_weighed_by_its_quantity(self):
        items = [{"title": "oat bars", "price": 2.0, "quantity": 6,
                  "category": "Food & Drink"},
                 {"title": "lamp", "price": 10.0, "quantity": 1,
                  "category": "Home"}]
        got = store_match.record_parts(items, 22.0, "Amazon - ")
        self.assertEqual({p["category"]: p["amount"] for p in got},
                         {FOOD: 12.0, HOME: 10.0})

    def test_a_line_total_is_not_multiplied_again(self):
        items = [{"amount": 30.0, "quantity": 3, "category": "Food & Drink"},
                 {"amount": 10.0, "category": "Home"}]
        got = store_match.record_parts(items, 40.0, "Costco - ")
        self.assertEqual({p["category"]: p["amount"] for p in got},
                         {"Costco - Food & Drink": 30.0, "Costco - Home": 10.0})

    def test_a_bad_quantity_counts_as_one_and_keeps_the_reading(self):
        for q in (None, 0, -2, "lots", float("nan"), 1e308):
            items = [{"price": 15.0, "quantity": q, "category": "Food & Drink"},
                     {"price": 5.0, "category": "Home"}]
            got = store_match.record_parts(items, 20.0, "Amazon - ")
            self.assertEqual({p["category"]: p["amount"] for p in got},
                             {FOOD: 15.0, HOME: 5.0}, q)

    def test_an_unusable_price_still_leaves_the_row_whole(self):
        items = [{"price": "n/a", "quantity": 2, "category": "Food & Drink"},
                 {"price": 5.0, "category": "Home"}]
        self.assertIsNone(store_match.record_parts(items, 20.0, "Amazon - "))


class OrderCategoryTests(unittest.TestCase):
    def test_the_order_is_filed_where_most_of_the_money_went(self):
        row = {"items_json": [{"title": "lamp", "price": 20.0, "quantity": 1},
                              {"title": "oat bars", "price": 4.0, "quantity": 6}],
               "memo": "", "seller": ""}
        cat, items = llm_categorize._amazon_answer(row, ["Home", "Food & Drink"])
        self.assertEqual(cat, "Food & Drink")
        self.assertEqual([i["category"] for i in items], ["Home", "Food & Drink"])

    def test_a_bad_quantity_weighs_the_item_once(self):
        row = {"items_json": [{"title": "lamp", "price": 20.0},
                              {"title": "oat bars", "price": 4.0, "quantity": "x"}],
               "memo": "", "seller": ""}
        cat, _ = llm_categorize._amazon_answer(row, ["Home", "Food & Drink"])
        self.assertEqual(cat, "Home")


if __name__ == "__main__":
    unittest.main()
