"""A merchant word of any length still reads its rows, in SQL as in Python.

A bill's merchant key is matched in the database by its tokens — maximal
runs of four or more ASCII letters, each required with its edges. Postgres
compiles each token into a regular expression, and past some tens of
thousands of letters it refuses the pattern ("regular expression is too
complex"). A key that long is not a merchant anyone types, but a restored
archive can carry one (specs/invariants-and-fuzzing.md, R3), and then the
history page for that bill answered 500. A long token is asked as a member
of the row's letter runs instead, which is the same test without a
pattern the size of the token: the rows it admits are exactly the ones the
Python matcher keeps, and the page opens.
"""
import datetime as dt
import unittest

from oikonome.engine import bills, budget

from .util import add_bill, add_txn, make_db

TODAY = dt.date(2026, 8, 20)
WORD = "q" * 200_000
ROWS = {
    "alone": f"{WORD} 0042",
    "edged": f"PAY*{WORD}-CARD",
    "inside": f"Z{WORD}",
    "short": "QQQQ MARKET",
}


class _Ledger(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        for i, (tid, name) in enumerate(ROWS.items()):
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


class ALongTokenMatchesAsPythonDoes(_Ledger):
    def test_the_sql_filter_admits_exactly_the_python_rows(self):
        self.assertEqual(self._python(WORD), {"alone", "edged"})
        self.assertEqual(self._sql(WORD), {"alone", "edged"})

    def test_a_long_token_beside_a_short_one_needs_both(self):
        key = f"{WORD} card"
        self.assertEqual(self._sql(key), self._python(key))
        self.assertEqual(self._sql(key), {"edged"})

    def test_both_forms_agree_at_the_length_where_they_meet(self):
        # just under and just over the switch: the regex form and the
        # letter-run form read the same rows
        for n in (budget.REGEX_TOKEN_MAX, budget.REGEX_TOKEN_MAX + 1):
            word = "w" * n
            with self.subTest(n=n):
                self.conn.execute("DELETE FROM transactions")
                for tid, name in {"a": f"{word}", "b": f"X{word}",
                                  "c": f"1{word}2", "d": f"{word}S"}.items():
                    add_txn(self.conn, TODAY, 5.00, name, txn_id=tid)
                self.assertEqual(self._sql(word), self._python(word))
                self.assertEqual(self._sql(word), {"a", "c"})


class TheHistoryPageOpens(_Ledger):
    def test_a_bill_keyed_on_a_very_long_word_lists_its_rows(self):
        add_bill(self.conn, "Long Word", 40, merchant=WORD, merchants=[])
        self.conn.execute(
            "UPDATE bills SET raw = raw - 'merchant_refs'"
            " - 'merchant_names' - 'merchant_pinned' - 'merchant_offered'")
        hist = bills.merchant_history(self.conn, "Long Word", today=TODAY)
        self.assertEqual({t["txn_id"] for t in hist["txns"]},
                         {"alone", "edged"})


if __name__ == "__main__":
    unittest.main()
