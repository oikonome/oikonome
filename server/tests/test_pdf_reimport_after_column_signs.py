"""Reading signs from a statement's columns must not change the ids of
rows an earlier import of the same statement made: names and stored lines
come from the plain text as they always did, and a row the columns now
sign the other way is corrected in place, never added beside its old
twin."""

import unittest
from unittest import mock

from oikonome.sync import pdfimport

from .test_pdfimport import HEADER, PERIOD, make_layout_pdf
from .util import make_db, write_config


def _statement():
    return make_layout_pdf([[
        PERIOD, HEADER,
        # a description drawn as two runs: the layout rendering spaces
        # them apart, the plain text does not
        (720, [(36, "06/03"), (100, "GROCERY MART"), (200, "SPRINGFIELD"),
               (380, "42.50"), (540, "1,957.50")]),
        # no bank keyword says which way this went; the column does
        (708, [(36, "06/05"), (100, "IRA DISTRIBUTION"), (460, "1,000.00"),
               (540, "2,957.50")]),
        (696, [(36, "06/07"), (100, "CHECK 1024"), (380, "500.00"),
               (540, "2,457.50")]),
    ]])


class ReimportAfterColumnSignsTests(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _rows(self):
        return sorted((r["name"], float(r["amount"]), r["line"])
                      for r in self.conn.execute(
                          "SELECT name, amount, raw->>'line' AS line "
                          "FROM transactions WHERE account_id='chk'"))

    def test_names_and_lines_come_from_the_plain_text(self):
        text, hints = pdfimport.extract_statement(_statement())
        self.assertEqual(text, pdfimport.extract_text(_statement()))
        self.assertTrue(any(hints))
        names = [r["name"] for r in pdfimport.parse_statement(
            text, kind="bank", hints=hints)["rows"]]
        self.assertIn("GROCERY MART SPRINGFIELD", names)

    def test_reimport_of_a_statement_imported_before_column_signs(self):
        """The first import reads the statement as an importer without
        column signs did (plain text, keywords only). Importing it again
        must leave one row per line, each correctly signed: a changed
        name or a flipped sign hashes to a new id, and the old row would
        stay beside the new one, counting the money twice."""
        pdf = _statement()
        plain = pdfimport.extract_text(pdf)
        with mock.patch.object(pdfimport, "extract_statement",
                               return_value=(plain, None)), \
                mock.patch.object(pdfimport, "_balance_hints",
                                  return_value={}):
            pdfimport.import_pdf(self.conn, "chk", pdf, amount_sign="bank")
        before = self._rows()
        self.assertIn(("IRA DISTRIBUTION", 1000.0), [r[:2] for r in before])
        pdfimport.import_pdf(self.conn, "chk", pdf, amount_sign="bank")
        after = self._rows()
        self.assertEqual(len(after), 3, after)
        got = {name: amount for name, amount, _ in after}
        self.assertEqual(got, {"GROCERY MART SPRINGFIELD": 42.50,
                               "IRA DISTRIBUTION": -1000.00,
                               "CHECK 1024": 500.00})
        # and a third import changes nothing
        pdfimport.import_pdf(self.conn, "chk", pdf, amount_sign="bank")
        self.assertEqual(self._rows(), after)


class SameRowSidebarTests(unittest.TestCase):
    """A box beside the table (rewards, a summary) drawn on a
    transaction's row joins that row's line. Its number must never take
    the column sign meant for the transaction's own amount."""

    def test_three_column_row_reads_the_nonzero_cell_under_its_column(self):
        header = (" " * 6 + "Description" + " " * 20 + "Withdrawals"
                  + " " * 5 + "Deposits" + " " * 7 + "Balance")
        row = ("06/05 PAYROLL" + " " * 25 + "0.00" + " " * 10 + "2,000.00"
               + " " * 7 + "5,432.10")
        self.assertEqual(pdfimport._column_hints([header, row]),
                         [None, "in"])

    def test_a_sidebar_number_on_the_row_gives_no_column_sign(self):
        pdf = make_layout_pdf([[
            PERIOD, HEADER,
            # a waived fee: 0.00 under Withdrawals, then the balance, then
            # a rewards figure printed in a box to the right of the table
            (720, [(36, "06/03"), (100, "SERVICE FEE WAIVED"),
                   (380, "0.00"), (540, "1,950.00"), (590, "125.00")]),
            (708, [(36, "06/05"), (100, "IRA DISTRIBUTION"),
                   (460, "1,000.00"), (540, "2,950.00")]),
        ]])
        text, hints = pdfimport.extract_statement(pdf)
        pairs = list(zip(text.splitlines(), hints))
        self.assertEqual([h for ln, h in pairs if "WAIVED" in ln], [None])
        self.assertEqual([h for ln, h in pairs if "IRA" in ln], ["in"])


if __name__ == "__main__":
    unittest.main()
