"""A bill keyed on a cut card number still finds its rows, whole or cut.

A bill whose merchant key reads as a phrase ("ox mobile …0002": its
leading word is too short to be a token) matches by a word-bounded
substring, and that substring holds the number's digits. A file row stores
the number cut ("OX MOBILE …0002"), a feed row keeps the bank's spelling
("OX MOBILE 4000 0000 0000 0002", or the digits unbroken), and the key
itself is stored either way: cut by the bill pass and the migration, whole
where a person typed it. So both sides are compared cut — the phrase and
the row's text — in Python and in the SQL that pre-filters for it, and a
key matches the same rows whichever way either side spells the number.
Every number here is a test-card shape, invented.
"""
import datetime as dt
import unittest

from oikonome.engine import bills, budget

from .util import add_bill, add_txn, make_db

TODAY = dt.date(2026, 8, 20)
WHOLE_KEY = "ox mobile 4000 0000 0000 0002"
CUT_KEY = "ox mobile …0002"
LEAD_WHOLE_KEY = "4000 0000 0000 0002 ox"
LEAD_CUT_KEY = "…0002 ox"

ROWS = {
    "cut": "OX MOBILE …0002 AUTOPAY",
    "grouped": "OX MOBILE 4000 0000 0000 0002 AUTOPAY",
    "dashed": "OX MOBILE 4000-0000-0000-0002 AUTOPAY",
    "unbroken": "OX MOBILE 4000000000000002 AUTOPAY",
}
OTHERS = {
    "other_card": "OX MOBILE 5500 0000 0000 0004 AUTOPAY",
    "box": "BOX MOBILE …0002 AUTOPAY",
}
LEAD_ROWS = {
    "lead_cut": "…0002 OX PAYMENT",
    "lead_grouped": "4000 0000 0000 0002 OX PAYMENT",
    "lead_unbroken": "4000000000000002 OX PAYMENT",
}


class _Ledger(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        for i, (tid, name) in enumerate(
                {**ROWS, **OTHERS, **LEAD_ROWS}.items()):
            add_txn(self.conn, dt.date(2026, 8, 1 + i), 40.00, name,
                    txn_id=tid)

    def _python(self, key):
        match = budget.merchant_matcher(key, None)
        return {r["id"] for r in self.conn.execute(
            f"SELECT t.id, {budget.MATCH_PAYEE_SQL} AS payee, t.name "
            "FROM transactions t").fetchall()
            if match(budget.match_text(r["payee"], r["name"]))}

    def _sql(self, key):
        frag, params = budget.merchant_match_sql(key, None)
        return {r["id"] for r in self.conn.execute(
            f"SELECT t.id FROM transactions t WHERE {frag}", params)}


class ThePhraseComparesCut(_Ledger):
    def test_a_cut_key_matches_cut_and_whole_rows_in_python(self):
        self.assertEqual(self._python(CUT_KEY), set(ROWS))

    def test_a_whole_key_matches_the_same_rows_as_its_cut(self):
        self.assertEqual(self._python(WHOLE_KEY), set(ROWS))

    def test_a_key_leading_with_the_number_matches_either_spelling(self):
        self.assertEqual(self._python(LEAD_CUT_KEY), set(LEAD_ROWS))
        self.assertEqual(self._python(LEAD_WHOLE_KEY), set(LEAD_ROWS))

    def test_the_sql_pre_filter_admits_what_python_accepts(self):
        for key in (CUT_KEY, WHOLE_KEY, LEAD_CUT_KEY, LEAD_WHOLE_KEY):
            with self.subTest(key=key):
                self.assertEqual(self._sql(key), self._python(key))

    def test_a_phrase_without_digits_reads_the_row_as_before(self):
        want = set(ROWS) | {"other_card"}
        self.assertEqual(self._python("ox mobile"), want)
        self.assertEqual(self._sql("ox mobile"), want)


class TheBillFindsItsRows(_Ledger):
    def test_the_history_page_lists_cut_and_whole_rows(self):
        for key in (CUT_KEY, WHOLE_KEY):
            with self.subTest(key=key):
                self.conn.execute("DELETE FROM bills")
                add_bill(self.conn, "Ox Mobile", 40, merchant=key,
                         merchants=[])
                # a text-matched bill: no identity at all
                self.conn.execute(
                    "UPDATE bills SET raw = raw - 'merchant_refs'"
                    " - 'merchant_names' - 'merchant_pinned'"
                    " - 'merchant_offered'")
                hist = bills.merchant_history(self.conn, "Ox Mobile",
                                              today=TODAY)
                self.assertEqual({t["txn_id"] for t in hist["txns"]},
                                 set(ROWS))

    def test_discovery_claims_the_rows_a_key_names_either_way(self):
        # bill_displays decides the phrase over each merchant key in SQL;
        # each row here is its own display name, so the names it returns
        # are the rows the key claims
        want = {n.lower() for n in ROWS.values()}
        for key in (CUT_KEY, WHOLE_KEY):
            with self.subTest(key=key):
                got = budget.bill_displays(
                    self.conn, [{"payee": "Ox Mobile", "merchant": key}],
                    TODAY)["Ox Mobile"]
                self.assertEqual({g.lower() for g in got} & want, want, got)


if __name__ == "__main__":
    unittest.main()
