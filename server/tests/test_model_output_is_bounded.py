"""What a language model returns is data, not a decision.

- A classifier's late answer never displaces a rule a person wrote while
  the batch was in flight.
- A vision model's receipt fields are coerced to the prompted shape — a
  merchant that came back as an object, a total that came back as
  Infinity — before anything is written.
- A typed line-item search is a literal, not a LIKE pattern.
"""

import math
import unittest

from oikonome.engine import llm_categorize, receipts

from .util import make_db, write_config


class ModelWriteBackTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _row(self, merchant):
        return self.conn.execute(
            "SELECT category_primary, source FROM merchant_categories "
            "WHERE merchant=%s", (merchant,)).fetchone()

    def test_a_user_rule_survives_a_late_model_answer(self):
        llm_categorize.upsert_user_rule(self.conn, "corner cafe", "FOOD_AND_DRINK")
        llm_categorize.record_model_category(self.conn, "corner cafe",
                                             "ENTERTAINMENT")
        row = self._row("corner cafe")
        self.assertEqual(row["category_primary"], "FOOD_AND_DRINK")
        self.assertEqual(row["source"], "user")

    def test_a_model_rule_is_still_refreshed(self):
        llm_categorize.record_model_category(self.conn, "bolt bikes",
                                             "TRANSPORTATION")
        llm_categorize.record_model_category(self.conn, "bolt bikes",
                                             "ENTERTAINMENT")
        self.assertEqual(self._row("bolt bikes")["category_primary"],
                         "ENTERTAINMENT")


class VisionOutputTests(unittest.TestCase):
    def test_receipt_meta_is_coerced_to_the_prompted_shape(self):
        meta = receipts._clean_receipt_meta({
            "merchant": {"name": "Kroger", "address": "1 Main St"},
            "date": 20240105, "total": math.inf, "tax": "1.20",
            "tip": float("nan")})
        self.assertEqual(meta["merchant"], "Kroger")
        self.assertEqual(meta["date"], "20240105")
        self.assertIsNone(meta["total"])
        self.assertEqual(meta["tax"], 1.2)
        self.assertIsNone(meta["tip"])

    def test_check_amount_must_be_finite(self):
        self.assertIsNone(receipts._clean_check_meta({"amount": "inf"})["amount"])
        self.assertEqual(receipts._clean_check_meta({"amount": -12.5})["amount"],
                         12.5)

    def test_non_finite_numbers_never_pass(self):
        for bad in (math.nan, math.inf, -math.inf, "NaN", None, "x", [1],
                    int("9" * 400)):      # a barcode read as a price
            self.assertIsNone(receipts._finite(bad))
        self.assertEqual(receipts._finite("3.5"), 3.5)


class SearchLiteralTests(unittest.TestCase):
    def test_wildcards_are_escaped(self):
        self.assertEqual(receipts._like_literal("100% off_now\\"),
                         "100\\% off\\_now\\\\")


if __name__ == "__main__":
    unittest.main()
