"""Two long labels cannot hold the household's identity lock.

Every question the resolver asks about a bank line and a payee name —
does the line name the payee, may it overrule a one-off guess — compares
each word of one string against every word of the other. That is
quadratic, and both strings can be arbitrary: a file import may map two
free-text columns onto the descriptor and the merchant name, and a
restored archive carries whatever the file said. The comparison runs
inside the per-household identity lock, so an uncut pair does not merely
make one import slow — it blocks every sync and the nightly pass behind it
for as long as the words keep matching.

Labels are therefore cut to the same length the string clean cuts them to.
Nothing a real bank descriptor or payee name carries lives past that
point.
"""

import datetime as dt
import time
import unittest

from oikonome.engine import merchant_dedup, merchant_identity
from oikonome.sync import base

from .util import make_db, write_config

# The worst shape for the comparison: every word of the name IS a word of
# the line, so no word breaks the loop early, and the two strings still
# differ so nothing answers before the words are read.
_WORDS = 20_000
LINE = " ".join(f"juniper{i}" for i in range(_WORDS))
NAME = LINE + " harborlights"

# generous: the cut leaves microseconds of work, while the same pair
# uncut takes tens of seconds and grows with the square of the words
_PROMPT = 2.0


class PathologicalLabelPairTests(unittest.TestCase):
    def test_the_match_answers_promptly_on_two_long_labels(self):
        t0 = time.monotonic()
        merchant_identity._descriptor_names(NAME, LINE)
        merchant_identity._descriptor_names(LINE, NAME)
        self.assertLess(time.monotonic() - t0, _PROMPT)

    def test_the_cut_is_the_one_the_string_clean_makes(self):
        """The two modules read the same labels — a name this cleans is
        matched against the lines it came from — so a length they
        disagreed about would clean one string and match another."""
        self.assertEqual(merchant_identity._MAX_LABEL,
                         merchant_dedup._MAX_LABEL)
        self.assertEqual(merchant_identity._words(NAME),
                         merchant_identity._words(NAME[:1000]))

    def test_an_ordinary_pair_is_untouched_by_the_cut(self):
        self.assertTrue(merchant_identity._descriptor_names(
            "JUNIPERS MARKET SPRINGFIELD OR", "Juniper's Market"))
        self.assertFalse(merchant_identity._descriptor_names(
            "JUNIPERS MARKET SPRINGFIELD OR", "Harbor Lights"))


class PathologicalRowTests(unittest.TestCase):
    """The ingest door every importer and aggregator funnels through is
    where the two strings are brought back to a length a descriptor comes
    in at. Two absurd ones cost more than their own row: the identity key
    is also a btree key in the alias map, and Postgres refuses an index
    entry past a couple of thousand bytes — which aborts the resolve for
    every row the sync delivered, leaving them all unresolved, while the
    word-by-word comparisons hold the household's identity lock."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_row_carrying_such_a_pair_is_stored_and_resolved(self):
        t0 = time.monotonic()
        base.upsert_transactions(self.conn, [base.Transaction(
            id="big", account_id="card", date=dt.date(2026, 2, 11),
            amount=31.40, name=LINE, merchant_name=NAME)])
        self.assertLess(time.monotonic() - t0, _PROMPT)
        row = dict(self.conn.execute(
            "SELECT name, merchant_name, merchant_id FROM transactions "
            " WHERE id = 'big'").fetchone())
        self.assertEqual(row["name"], LINE[:base.MAX_DESCRIPTOR])
        self.assertEqual(row["merchant_name"], NAME[:base.MAX_DESCRIPTOR])
        self.assertIsNotNone(row["merchant_id"],
                             "the resolve the sync ends with did not reach "
                             "the row it delivered")

    def test_an_ordinary_descriptor_is_stored_whole(self):
        line = "JUNIPERS MARKET 0917 RIVERTON"
        base.upsert_transactions(self.conn, [base.Transaction(
            id="small", account_id="card", date=dt.date(2026, 2, 12),
            amount=18.60, name=line, merchant_name="Juniper's Market")])
        self.assertEqual(
            dict(self.conn.execute(
                "SELECT name, merchant_name FROM transactions "
                " WHERE id = 'small'").fetchone()),
            {"name": line, "merchant_name": "Juniper's Market"})


class PathologicalStatementLineTests(unittest.TestCase):
    """A file import's raw is the whole source row, so a column headed
    original_description can carry any cell the row bound allows. The
    Zelle and Venmo counterparty patterns backtrack quadratically on a long
    run of brackets or spaces, so that cell is cut to a descriptor's length
    before anything parses it, and stored at that length too."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_a_huge_statement_line_cell_is_cut_before_it_is_parsed(self):
        cells = {"zel": "zelle payment from " + "(" * 120_000,
                 "ven": "VENMO *" + " " * 120_000 + "1"}
        t0 = time.monotonic()
        base.upsert_transactions(self.conn, [base.Transaction(
            id=f"csv:{k}", account_id="card", date=dt.date(2026, 2, 13),
            amount=-20.0, name="TRANSFER", merchant_name=None,
            raw={"original_description": v}) for k, v in cells.items()])
        self.assertLess(time.monotonic() - t0, _PROMPT)
        for k in cells:
            stored = self.conn.execute(
                "SELECT raw->>'original_description' AS d FROM transactions "
                "WHERE id=%s", (f"csv:{k}",)).fetchone()["d"]
            self.assertLessEqual(len(stored), base.MAX_DESCRIPTOR)

    def test_an_ordinary_zelle_line_still_names_the_counterparty(self):
        base.upsert_transactions(self.conn, [base.Transaction(
            id="csv:zelle-ok", account_id="card", date=dt.date(2026, 2, 14),
            amount=25.0, name="ZELLE", merchant_name=None,
            raw={"original_description":
                 "Zelle payment from A Person (Acme Checking XXXXXX1234) "
                 "to Bea Builder"})])
        name = self.conn.execute(
            "SELECT merchant_name FROM transactions WHERE id='csv:zelle-ok'"
        ).fetchone()["merchant_name"]
        self.assertIn("Bea Builder", name or "")


if __name__ == "__main__":
    unittest.main()
