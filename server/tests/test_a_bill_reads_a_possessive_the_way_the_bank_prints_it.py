"""A bill matched by its words finds its charge whichever way the name's
apostrophe was printed.

A person types the bill as "Juniper's Market"; the card line for the
purchase prints "JUNIPERS MARKET". Broken on the apostrophe the first reads
"juniper" and the second "junipers", the two share no word, and the bill
reads as unpaid beside the charge that paid it. An apostrophe joins the
letters around it — the rule the merchant string clean follows — in the
Python matcher and in every SQL form of it, which have to agree or a
pre-filter drops rows its matcher would keep.
"""

import datetime as dt
import unittest

from oikonome.engine import bills, budget

from .util import add_bill, add_txn, make_db

TODAY = dt.date(2026, 8, 20)

# every shape the rule has an opinion about: the three glyphs, a trailing
# possessive, an elided prefix, a run of them, a doubled one, a leading
# quote (a real word break), and initials punctuation has shredded
SAMPLES = ["Juniper's Market", "JUNIPERS MARKET", "Juniper’s Market",
           "Juniperʼs Market", "Junipers' Market", "L'Anfora Trattoria",
           "Rise'n'Shine Diner", "Casa ''Verde Grill", "'Verde Grill",
           "AT&T", "J's", "Juniper's Pizza #12", "Lowe's"]


def _clear_identity(conn):
    conn.execute(
        "UPDATE bills SET raw = raw - 'merchant_refs' - 'merchant_names' "
        "- 'merchant_pinned' - 'merchant_offered'")


class WordsTests(unittest.TestCase):
    def test_both_spellings_are_the_same_words(self):
        self.assertEqual(budget._tokens("Juniper's Market"),
                         budget._tokens("JUNIPERS MARKET"))
        self.assertEqual(budget._tokens("Lowe's"), budget._tokens("LOWES"))
        for glyph in ("'", "’", "ʼ"):
            self.assertEqual(budget._tokens(f"Juniper{glyph}s Market"),
                             {"junipers", "market"}, repr(glyph))

    def test_a_leading_quote_is_still_a_word_break(self):
        self.assertEqual(budget._tokens("Casa 'Verde Grill"),
                         {"casa", "verde", "grill"})

    def test_two_different_payees_still_differ(self):
        self.assertNotEqual(budget._tokens("Juniper's Pizza"),
                            budget._tokens("Marion Pizza"))

    def test_a_phrase_bill_reads_it_the_same_way(self):
        """A leading short word puts the bill in phrase mode, which has its
        own normal form."""
        match = budget.merchant_matcher("jo's diner")
        self.assertTrue(match(budget.match_text("JOS DINER 22", "JOS DINER 22")))
        self.assertFalse(match(budget.match_text("BANJOS DINER", "BANJOS DINER")))

    def test_the_words_of_a_name_join_too(self):
        self.assertEqual(bills._words("Lowe's Fuel"), {"lowes", "fuel"})


class SqlAgreesWithPythonTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        for i, s in enumerate(SAMPLES):
            add_txn(self.conn, "2026-08-01", 10 + i, s.upper(), merchant=s,
                    txn_id=f"s{i}")

    def tearDown(self):
        self.conn.close()

    def _rows(self):
        return self.conn.execute(
            f"SELECT t.id, {budget.MATCH_PAYEE_SQL} AS payee, t.name, "
            f"       {budget.MATCH_TEXT_SQL} AS text, "
            f"       {budget.MATCH_NORM_SQL} AS norm "
            "FROM transactions t").fetchall()

    def test_the_text_and_its_normal_form_are_one_string_each(self):
        for r in self._rows():
            text = budget.match_text(r["payee"], r["name"])
            self.assertEqual(r["text"], text)
            self.assertEqual(r["norm"], budget._text_norm(text))

    def test_every_bill_form_selects_the_rows_its_matcher_accepts(self):
        for merchant in ("juniper's market", "junipers market", "lowes",
                         "lowe's", "jo's diner", "l'anfora trattoria",
                         "juniper's pizza|rise'n'shine diner", "verde grill"):
            match = budget.merchant_matcher(merchant)
            want = {r["id"] for r in self._rows() if match(r["text"])}
            frag, params = budget.merchant_match_sql(merchant)
            got = {r["id"] for r in self.conn.execute(
                f"SELECT t.id FROM transactions t WHERE {frag}", params).fetchall()}
            self.assertEqual(got, want, merchant)


class TextBillTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.paid = add_txn(self.conn, "2026-08-05", 42.10,
                            "JUNIPERS MARKET 22", txn_id="bankline")
        add_txn(self.conn, "2026-08-06", 18.00, "JUNIPER HARDWARE",
                txn_id="other")
        add_bill(self.conn, "Juniper's Market", 42, merchant="juniper's market",
                 merchants=[], txn_category="FOOD_AND_DRINK")
        _clear_identity(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_history_page_lists_the_charge(self):
        hist = bills.merchant_history(self.conn, "Juniper's Market", today=TODAY)
        self.assertEqual({t["txn_id"] for t in hist["txns"]}, {"bankline"})

    def test_the_bills_category_reaches_the_charge(self):
        bills.apply_txn_categories(self.conn)
        got = {r["id"]: r["category_override"] for r in self.conn.execute(
            "SELECT id, category_override FROM transactions").fetchall()}
        self.assertEqual(got, {"bankline": "FOOD_AND_DRINK", "other": None})

    def test_discovery_offers_the_bill_the_merchant_its_charge_is_filed_under(self):
        from oikonome.engine import merchant_dedup
        merchant_dedup.apply(self.conn)
        row = self.conn.execute(
            "SELECT payee, merchant FROM bills").fetchone()
        self.assertEqual(budget.bill_displays(self.conn, [row], TODAY),
                         {"Juniper's Market": frozenset({"Junipers Market"})})


if __name__ == "__main__":
    unittest.main()
