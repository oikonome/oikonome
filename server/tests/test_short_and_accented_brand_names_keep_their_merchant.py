"""The string clean keeps the part of a name that tells one business from
another, and gives one business one spelling.

* An ampersand or dots between one- or two-letter pieces are part of the
  name: "Q&X BANK" and "Q.R. BANK" are different banks, "Q&Z" and "X&Y"
  different grocers. Scrubbed to spaces, the short pieces were dropped and
  both fell to the shared word ("Bank", "Supermarket") — one merchant
  showing another business's charges in its total, one category answer
  covering both.
* An accented letter is folded to its plain letter, not deleted: "Café Contoso"
  from the feed and "CAFE CONTOSO" from the card line are one merchant, not
  "Caf Contoso" beside "Cafe Contoso" with the spend split between them.
* A household whose ledger was cleaned the old way is moved by the next
  merchant pass: rows go to the merchant the new name names, the emptied
  merchant points at it, and a rule under the old name follows.
"""
import unittest

from oikonome.engine import merchant_dedup, merchant_identity
from oikonome.engine.merchant_dedup import canonical_merchant

from .util import add_txn, make_db


class ShortBrandNameTests(unittest.TestCase):
    def test_ampersand_brands_stay_apart(self):
        self.assertEqual(canonical_merchant("Q&X BANK"), "Q&X Bank")
        self.assertEqual(canonical_merchant("Q&Z SUPERMARKET"),
                         "Q&Z Supermarket")
        self.assertEqual(canonical_merchant("X&Y MARKET #12"),
                         "X&Y Market")
        self.assertEqual(canonical_merchant("Q&W PHOTO"), "Q&W Photo")
        self.assertEqual(canonical_merchant("QZ&R WIRELESS"), "QZ&R Wireless")
        self.assertEqual(canonical_merchant("V & W RESTAURANT"),
                         "V&W Restaurant")
        self.assertNotEqual(canonical_merchant("D&X FRESH MARKET"),
                            canonical_merchant("THE FRESH MARKET #123"))

    def test_an_ampersand_between_words_is_still_punctuation(self):
        self.assertEqual(canonical_merchant("CEDAR PINE & HOME"),
                         "Cedar Pine Home")
        self.assertEqual(canonical_merchant("ALDER&BIRCH"), "Alder Birch")

    def test_dotted_initials_are_one_word(self):
        self.assertEqual(canonical_merchant("Q.R. BANK ACH"),
                         canonical_merchant("QR BANK"))
        self.assertEqual(canonical_merchant("J.R. NOODLE'S #123"),
                         canonical_merchant("JR NOODLES"))
        self.assertNotEqual(canonical_merchant("Q.R. BANK"),
                            canonical_merchant("Q&X BANK"))

    def test_a_short_leading_number_is_the_brand_beside_a_generic_word(self):
        self.assertEqual(canonical_merchant("88 GAS 0123"), "88 Gas")
        # before a bank-mechanics word the number is a reference
        self.assertEqual(canonical_merchant("ATM WITHDRAWAL"),
                         "Atm Withdrawal")

    def test_a_short_trailing_number_or_letter_keeps_a_lone_word_apart(self):
        # the brand is the generic word AND the token after it; cut to the
        # word alone, a numbered chain fuses with every shop of that word
        self.assertEqual(canonical_merchant("MARKET 41 #5813"), "Market 41")
        self.assertEqual(canonical_merchant("MOTEL 9 #7716"), "Motel 9")
        self.assertEqual(canonical_merchant("SUPER 7 #6658"), "Super 7")
        self.assertEqual(canonical_merchant("PIZZA 37"), "Pizza 37")
        self.assertEqual(canonical_merchant("HOOPLA Q #5813"), "Hoopla Q")
        self.assertNotEqual(canonical_merchant("MARKET 41 #5813"),
                            canonical_merchant("THE MARKET #12"))
        self.assertNotEqual(canonical_merchant("HOOPLA Q #5813"),
                            canonical_merchant("HOOPLA"))
        # two locations of one business still fold: the store number is cut
        self.assertEqual(canonical_merchant("MOTEL 9 #7716"),
                         canonical_merchant("MOTEL 9 #3307"))
        self.assertEqual(canonical_merchant("HOOPLA Q #5813"),
                         canonical_merchant("HOOPLA Q 3307"))
        self.assertEqual(canonical_merchant("PIZZA 37 #12"),
                         canonical_merchant("PIZZA 37 #98"))
        # a long number is a store or reference number, never the brand
        self.assertEqual(canonical_merchant("ACME STORE 6658"), "Acme Store")
        self.assertEqual(canonical_merchant("QUILLO 0042"), "Quillo")
        # beside a bank-mechanics word the number is a reference
        self.assertNotEqual(canonical_merchant("TRANSFER 12"), "Transfer 12")


    def test_a_leading_letter_keeps_a_lone_word_apart(self):
        # the brand is the letter AND the word after it; dropped as a bank
        # prefix, two stores differing only by that letter fuse into one
        self.assertEqual(canonical_merchant("Q DEPOT #5813"), "Q Depot")
        self.assertEqual(canonical_merchant("V DEPOT 0042"), "V Depot")
        self.assertNotEqual(canonical_merchant("Q DEPOT #5813"),
                            canonical_merchant("V DEPOT 0042"))
        self.assertNotEqual(canonical_merchant("Q ZORBIT"),
                            canonical_merchant("V ZORBIT"))
        self.assertNotEqual(canonical_merchant("Q DEPOT"),
                            canonical_merchant("DEPOT"))
        # the letter survives a processor prefix and a starred spelling
        self.assertEqual(canonical_merchant("SQ *Q DEPOT"), "Q Depot")
        self.assertEqual(canonical_merchant("Q*ZORBIT"), "Q Zorbit")
        # two locations of one business still fold: the store number is cut
        self.assertEqual(canonical_merchant("Q DEPOT #5813"),
                         canonical_merchant("Q DEPOT 3307"))
        # beside a name of two words the letter is still a prefix
        self.assertEqual(canonical_merchant("N QUILLO MARKET"),
                         "Quillo Market")

    def test_a_line_with_no_name_in_it_never_names_a_merchant_the_api_refuses(self):
        # when no word survives the clean the line itself becomes the name;
        # a control character in it would make the merchant unopenable
        # (the merchant endpoints refuse a name carrying one)
        for line in ("\x01\x02 ##", "!!!\x07", "#\x1f#"):
            got = canonical_merchant(line)
            self.assertTrue(got)
            self.assertFalse(any(ord(ch) < 32 or 127 <= ord(ch) <= 159
                                 for ch in got), repr(got))


class AccentFoldTests(unittest.TestCase):
    def test_accented_and_plain_spellings_are_one_name(self):
        for accented, plain in (("Café Contoso", "CAFE CONTOSO"),
                                ("Jalapeño Grill", "JALAPENO GRILL"),
                                ("Pokémon Center", "POKEMON CENTER"),
                                ("Häagen-Dazs", "HAAGEN-DAZS"),
                                ("Señor Taco", "SENOR TACO")):
            self.assertEqual(canonical_merchant(accented),
                             canonical_merchant(plain), accented)
        self.assertEqual(canonical_merchant("Café Contoso"), "Cafe Contoso")

    def test_descriptor_words_read_through_the_same_fold(self):
        self.assertEqual(merchant_identity._words("Café Contoso"),
                         merchant_identity._words("CAFE CONTOSO"))


class StoredOldCleanMovesTests(unittest.TestCase):
    """Canonicals written by the old clean are recomputed and their rows,
    merchant pointers and rules follow — no twin is left behind."""

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _layer1(self, raw, canon):
        self.conn.execute(
            "INSERT INTO merchant_canonical (raw_merchant, canonical, method) "
            "VALUES (%s,%s,'layer1')", (raw, canon))

    def _merchant_of(self, tid):
        return self.conn.execute(
            "SELECT m.name FROM transactions t JOIN merchants m "
            "ON m.id = t.merchant_id WHERE t.id = %s", (tid,)).fetchone()["name"]

    def _live(self, name):
        return self.conn.execute(
            "SELECT id, merged_into FROM merchants WHERE name = %s",
            (name,)).fetchone()

    def test_a_merchant_two_banks_shared_splits_into_the_two(self):
        add_txn(self.conn, "2026-08-01", 900.0, "Q&X BANK", txn_id="mt")
        add_txn(self.conn, "2026-08-02", 25.0, "Q.R. BANK", txn_id="us")
        self._layer1("Q&X BANK", "Bank")
        self._layer1("Q.R. BANK", "Bank")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("mt"), self._merchant_of("us"))
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES ('Bank', 'LOAN_PAYMENTS', 'user')")

        merchant_dedup.apply(self.conn)

        self.assertEqual(self._merchant_of("mt"), "Q&X Bank")
        self.assertEqual(self._merchant_of("us"), "QR Bank")
        old = self._live("Bank")
        self.assertTrue(old is None or old["merged_into"] is not None)
        # the person's rule follows the merchant the old one was retired into
        self.assertIsNotNone(self.conn.execute(
            "SELECT 1 FROM merchant_categories WHERE merchant IN "
            "('Q&X Bank', 'QR Bank') AND source = 'user'").fetchone())
        self.assertEqual(merchant_dedup.apply(self.conn), 0)

    def test_a_merchant_two_lettered_stores_shared_splits_into_the_two(self):
        add_txn(self.conn, "2026-08-01", 180.0, "Q DEPOT #12", txn_id="qd")
        add_txn(self.conn, "2026-08-02", 45.0, "V DEPOT 0042", txn_id="vd")
        self._layer1("Q DEPOT #12", "Depot")
        self._layer1("V DEPOT 0042", "Depot")
        merchant_identity.resolve(self.conn)
        self.assertEqual(self._merchant_of("qd"), self._merchant_of("vd"))

        merchant_dedup.apply(self.conn)

        self.assertEqual(self._merchant_of("qd"), "Q Depot")
        self.assertEqual(self._merchant_of("vd"), "V Depot")
        self.assertEqual(merchant_dedup.apply(self.conn), 0)

    def test_an_accent_mangled_merchant_folds_into_its_plain_twin(self):
        add_txn(self.conn, "2026-08-01", 12.0, "Café Contoso", txn_id="acc")
        add_txn(self.conn, "2026-08-02", 14.0, "CAFE CONTOSO", txn_id="pln")
        self._layer1("Café Contoso", "Caf Contoso")
        self._layer1("CAFE CONTOSO", "Cafe Contoso")
        merchant_identity.resolve(self.conn)
        self.assertNotEqual(self._merchant_of("acc"), self._merchant_of("pln"))
        self.conn.execute(
            "INSERT INTO merchant_categories (merchant, category_primary, "
            "source) VALUES ('Caf Contoso', 'FOOD_AND_DRINK', 'user')")

        merchant_dedup.apply(self.conn)

        self.assertEqual(self._merchant_of("acc"), "Cafe Contoso")
        self.assertEqual(self._merchant_of("pln"), "Cafe Contoso")
        old = self._live("Caf Contoso")
        self.assertTrue(old is None or old["merged_into"] is not None)
        rule = self.conn.execute(
            "SELECT category_primary FROM merchant_categories "
            "WHERE merchant = 'Cafe Contoso'").fetchone()
        self.assertEqual(rule["category_primary"], "FOOD_AND_DRINK")


if __name__ == "__main__":
    unittest.main()
