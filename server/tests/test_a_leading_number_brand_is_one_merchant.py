"""A short number at the front of a name, followed by a brand word, is part
of the brand: "7 ELEVEN", "7-ELEVEN" and "SEVEN ELEVEN" are one store, "99
RANCH MARKET" is not "Ranch Market". Read as a store number, the digits were
cut and one brand fell into several merchants ("Eleven", "7-Eleven", "Seven
Eleven"), or fused with every other business sharing the word that was left.

A long leading number is still a store or house number, and a short one
followed only by generic words or more digits is still a reference.
"""
import unittest

from oikonome.engine import merchant_dedup, merchant_identity
from oikonome.engine.merchant_dedup import canonical_merchant

from .util import add_txn, make_db


class LeadingNumberBrandTests(unittest.TestCase):
    def test_every_spelling_of_a_numbered_brand_is_one_key(self):
        keys = {canonical_merchant(s, ("Springfield",)) for s in (
            "7 ELEVEN 34512 SPRINGFIELD ZZ", "7-ELEVEN #34512",
            "SEVEN ELEVEN 34512", "7 ELEVEN", "7ELEVEN 34512",
            "SEVEN-ELEVEN #2291")}
        self.assertEqual(keys, {"7-Eleven"})

    def test_a_numbered_brand_before_a_generic_word_is_one_key(self):
        self.assertEqual({canonical_merchant(s) for s in (
            "76 FUEL 1182", "76-FUEL #1182", "76 FUEL")}, {"76 Fuel"})

    def test_a_numbered_brand_keeps_its_number_before_a_longer_name(self):
        self.assertEqual({canonical_merchant(s) for s in (
            "99 RANCH MARKET 05", "99 RANCH MARKET #1408",
            "99-RANCH MARKET 05")}, {"99-Ranch Market"})
        # the number is what tells this market from any other Ranch Market
        self.assertNotEqual(canonical_merchant("99 RANCH MARKET 05"),
                            canonical_merchant("RANCH MARKET 05"))

    def test_a_long_leading_number_is_still_a_store_number(self):
        self.assertEqual(canonical_merchant("4512 MAIN ST DELI"), "Main Deli")
        self.assertEqual(canonical_merchant("4512 QUILLO DELI"), "Quillo Deli")

    def test_a_short_number_before_a_street_is_an_address(self):
        self.assertEqual(canonical_merchant("12 MAIN ST DELI"),
                         canonical_merchant("4512 MAIN ST DELI"))

    def test_a_short_number_before_generic_words_or_digits_is_a_reference(self):
        self.assertFalse(canonical_merchant("12 PAYMENT 4411")
                         .startswith("12-"))
        self.assertEqual(canonical_merchant("05 QUILLO DELI"), "Quillo Deli")
        self.assertEqual(canonical_merchant("12 GAS STATION 4411"),
                         "Gas Station")

    def test_a_numbered_account_line_is_untouched(self):
        self.assertEqual(canonical_merchant("ACH 4111 1111 1111 1234 ACME"),
                         "Acme")
        # the card-number cut owns this shape: cut to its last four
        self.assertEqual(canonical_merchant("12 4111111111111234"),
                         "12 \u20261234")


class LeadingNumberBrandLedgerTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _merchant_of(self, tid):
        return self.conn.execute(
            "SELECT m.name FROM transactions t JOIN merchants m "
            "ON m.id = t.merchant_id WHERE t.id = %s", (tid,)).fetchone()["name"]

    def test_every_spelling_lands_on_one_merchant_row(self):
        for i, line in enumerate(("7 ELEVEN 34512", "7-ELEVEN #34512",
                                  "SEVEN ELEVEN 34512")):
            add_txn(self.conn, f"2026-08-0{i + 1}", 4.0 + i, line,
                    txn_id=f"se{i}")
        merchant_dedup.apply(self.conn)
        merchant_identity.resolve(self.conn)
        self.assertEqual({self._merchant_of(f"se{i}") for i in range(3)},
                         {"7-Eleven"})

    def test_a_ledger_cleaned_the_old_way_is_moved_to_the_one_merchant(self):
        add_txn(self.conn, "2026-08-01", 4.0, "7 ELEVEN 34512", txn_id="old")
        add_txn(self.conn, "2026-08-02", 5.0, "7-ELEVEN #34512", txn_id="hy")
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method, "
            "as_of) VALUES ('7 ELEVEN 34512', 'Eleven', 'layer1', now())")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("old"), "Eleven")

        merchant_dedup.apply(self.conn)

        self.assertEqual(self._merchant_of("old"), "7-Eleven")
        self.assertEqual(self._merchant_of("hy"), "7-Eleven")
        self.assertEqual(merchant_dedup.apply(self.conn), 0)


if __name__ == "__main__":
    unittest.main()
