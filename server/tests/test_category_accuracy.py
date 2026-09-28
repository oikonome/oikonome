"""Max-accuracy category policy: Plaid mirror columns + confidence gating."""

import unittest

from oikonome.engine.categories import effective, plaid_is_trusted
from oikonome.sync.base import Transaction, upsert_transactions
from oikonome.sync import plaid as plaid_mod

from .util import make_db


class CategoryPolicyUnitTests(unittest.TestCase):
    def test_effective_override_wins(self):
        self.assertEqual(effective("MEDICAL", "FOOD_AND_DRINK"), "MEDICAL")
        self.assertEqual(effective(None, "FOOD_AND_DRINK"), "FOOD_AND_DRINK")

    def test_plaid_trusted(self):
        self.assertTrue(plaid_is_trusted("HIGH"))
        self.assertTrue(plaid_is_trusted("VERY_HIGH"))
        self.assertFalse(plaid_is_trusted("LOW"))
        self.assertFalse(plaid_is_trusted("MEDIUM"))
        self.assertFalse(plaid_is_trusted(None))
        self.assertFalse(plaid_is_trusted(""))


class PlaidMirrorUpsertTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_plaid_norm_and_upsert_preserves_mirror(self):
        raw = {
            "transaction_id": "pl-1",
            "account_id": "card",
            "date": "2026-07-01",
            "amount": 10.0,
            "name": "COFFEE",
            "merchant_name": "Coffee Shop",
            "pending": False,
            "personal_finance_category": {
                "primary": "FOOD_AND_DRINK",
                "detailed": "FOOD_AND_DRINK_COFFEE",
                "confidence_level": "HIGH",
            },
        }
        t = plaid_mod._norm_txn(raw)
        self.assertEqual(t.category_plaid, "FOOD_AND_DRINK")
        self.assertEqual(t.category_plaid_confidence, "HIGH")
        upsert_transactions(self.conn, [t])
        row = self.conn.execute(
            "SELECT category_primary, category_plaid, category_plaid_confidence "
            "FROM transactions WHERE id='pl-1'").fetchone()
        self.assertEqual(row["category_primary"], "FOOD_AND_DRINK")
        self.assertEqual(row["category_plaid"], "FOOD_AND_DRINK")
        self.assertEqual(row["category_plaid_confidence"], "HIGH")

    def test_csv_upsert_does_not_wipe_plaid_mirror(self):
        # first: plaid-shaped row
        upsert_transactions(self.conn, [Transaction(
            id="x1", account_id="card", date=__import__("datetime").date(2026, 7, 1),
            amount=5.0, name="A", merchant_name="A",
            category_primary="MEDICAL", category_detailed="MEDICAL_OTHER_MEDICAL",
            category_plaid="MEDICAL", category_plaid_detailed="MEDICAL_OTHER_MEDICAL",
            category_plaid_confidence="HIGH",
            raw={"personal_finance_category": {"primary": "MEDICAL",
                                               "confidence_level": "HIGH"}},
        )])
        # second: non-plaid importer (no plaid fields) must not NULL the mirror
        upsert_transactions(self.conn, [Transaction(
            id="x1", account_id="card", date=__import__("datetime").date(2026, 7, 2),
            amount=5.0, name="A", merchant_name="A",
            category_primary="GENERAL_MERCHANDISE",
            raw={},
        )])
        row = self.conn.execute(
            "SELECT category_plaid, category_plaid_confidence, category_primary "
            "FROM transactions WHERE id='x1'").fetchone()
        self.assertEqual(row["category_plaid"], "MEDICAL")
        self.assertEqual(row["category_plaid_confidence"], "HIGH")
        self.assertEqual(row["category_primary"], "GENERAL_MERCHANDISE")


if __name__ == "__main__":
    unittest.main()
