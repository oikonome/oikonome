"""A phrase holding digits reads a row cut, and only a row that could hold it is cut.

A bill phrase with a digit in it ('24 hour fitness', 'ox …0002') is
compared with a row's text after every card or account number in the row
is cut to its last four (budget._phrase_text). The cut is slow per row, so
it must not run over rows that cannot match: every word of a cut text is
spelled in the row as it stands (a cut keeps a number's last four beside
what follows it), so a row that does not spell the phrase's longest word
cannot hold the phrase and is answered without the cut. The answer must be
the cut comparison's answer for every row — in Python, in the SQL
pre-filter over ledger rows, and in the SQL claim over merchant names.
Every name and number here is invented; the numbers are test-card shapes.
"""
import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import budget
from oikonome.sync import card_number

from .util import add_txn, make_db

PHRASES = [
    "24 Hour Fitness",          # digits, no group of four
    "7-Eleven",
    "A1 Storage",
    "Ox Mobile …0002",          # a cut number
    "Ox Mobile 4000 0000 0000 0002",
    "…0002 ox",
    "123456 12345 Zq Depot",    # the 6 and 5 groups of a 15-digit layout
    "Ox Mobile",                # no digits at all
]

ROWS = [
    "24 HOUR FITNESS CLUB DUES",
    "24 HOUR FITNESS12345678 DUES",     # number glued to the word
    "ACH 24 HOUR FITNESS 0012345678",
    "ACH PAYROLL 1234567890 ACME",
    "ZELLE TO RIVERBEND 4111111111111111",
    "7-ELEVEN 01234 SPRINGFIELD",
    "7 ELEVEN 12345678",
    "A1 STORAGE UNIT 44 9876543210",
    "OX MOBILE …0002 AUTOPAY",
    "OX MOBILE 4000 0000 0000 0002 AUTOPAY",
    "OX MOBILE 4000-0000-0000-0002",
    "OX MOBILE 4000000000000002",
    "OX MOBILE 5500 0000 0000 0004",
    "BOX MOBILE …0002",
    "4000 0000 0000 0002 OX PAYMENT",
    "REF 1234 123456 12345 ZQ DEPOT",
    "REF 123456 12345 ZQ DEPOT",
    "PLAIN GROCER",
    "",
]


def _cut_reading(phrase, text):
    """The comparison the matcher makes, spelled without any shortcut."""
    return phrase in budget._text_norm(budget._phrase_text(text))


class AppliesTheCutAnswer(unittest.TestCase):
    def test_python_agrees_with_the_cut_comparison_on_every_row(self):
        for merchant in PHRASES:
            phrase = budget._merchant_phrase(merchant)
            self.assertIsNotNone(phrase, merchant)
            for text in ROWS:
                with self.subTest(phrase=phrase, text=text):
                    want = (_cut_reading(phrase, text)
                            if budget._DIGIT.search(phrase)
                            else phrase in budget._text_norm(text))
                    self.assertEqual(budget._phrase_in(phrase, text.lower()),
                                     want)


class CutsOnlyRowsThatCouldHoldIt(unittest.TestCase):
    def test_a_row_without_the_phrases_words_is_never_cut(self):
        phrase = budget._merchant_phrase("24 Hour Fitness")
        with mock.patch.object(budget.card_number, "cut",
                               wraps=card_number.cut) as cut:
            self.assertFalse(budget._phrase_in(
                phrase, "ach payroll 1234567890 acme"))
        self.assertEqual(cut.call_count, 0)

    def test_a_row_with_the_phrases_words_is_still_cut(self):
        phrase = budget._merchant_phrase("24 Hour Fitness")
        self.assertTrue(budget._phrase_in(
            phrase, "24 hour fitness12345678 dues"))


class _Ledger(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        for i, name in enumerate(ROWS):
            add_txn(self.conn, dt.date(2026, 8, 1) + dt.timedelta(days=i),
                    10.0 + i, name or "-", txn_id=f"r{i:02d}")


class TheSqlAgrees(_Ledger):
    def _python(self, merchant):
        match = budget.merchant_matcher(merchant, None)
        return {r["id"] for r in self.conn.execute(
            f"SELECT t.id, {budget.MATCH_PAYEE_SQL} AS payee, t.name "
            "FROM transactions t").fetchall()
            if match(budget.match_text(r["payee"], r["name"]))}

    def _reference(self, merchant):
        phrase = budget._merchant_phrase(merchant)
        read = (_cut_reading if budget._DIGIT.search(phrase)
                else lambda p, t: p in budget._text_norm(t))
        return {r["id"] for r in self.conn.execute(
            f"SELECT t.id, {budget.MATCH_PAYEE_SQL} AS payee, t.name "
            "FROM transactions t").fetchall()
            if read(phrase, budget.match_text(r["payee"], r["name"]))}

    def test_the_ledger_pre_filter_returns_the_cut_answer(self):
        for merchant in PHRASES:
            frag, params = budget.merchant_match_sql(merchant, None)
            got = {r["id"] for r in self.conn.execute(
                f"SELECT t.id FROM transactions t WHERE {frag}", params)}
            with self.subTest(merchant=merchant):
                self.assertEqual(got, self._reference(merchant))
                self.assertEqual(got, self._python(merchant))

    def test_the_merchant_name_claim_returns_the_cut_answer(self):
        names = [n.lower() for n in ROWS]
        for merchant in PHRASES:
            frag, params = budget.merchant_claim_sql(merchant, None)
            rows = self.conn.execute(
                f"""SELECT name FROM (
                      SELECT name,
                             regexp_split_to_array(btrim(regexp_replace(
                                 name, '[^a-z]+', ' ', 'g')), ' ') AS runs,
                             ' ' || btrim(regexp_replace(
                                 name, '[^a-z0-9]+', ' ', 'g')) || ' ' AS norm,
                             {budget.cut_norm_sql('name')} AS cnorm
                        FROM unnest(%s::text[]) AS u(name)) x
                     WHERE {frag}""", [names, *params]).fetchall()
            phrase = budget._merchant_phrase(merchant)
            want = {n for n in names
                    if (_cut_reading(phrase, n)
                        if budget._DIGIT.search(phrase)
                        else phrase in budget._text_norm(n))}
            with self.subTest(merchant=merchant):
                self.assertEqual({r["name"] for r in rows}, want)


if __name__ == "__main__":
    unittest.main()
