"""Every file importer answers `imported` = the rows it added and
`matched` = the lines the ledger already held, so the same file imported
twice answers (n, 0) and then (0, n).

"Already held" is the importer's own id for the line: a content hash of
account, date, amount and name with an occurrence number for QIF, Mint,
YNAB, the competitor exports and statement PDFs (and CSV), the bank's
FITID for OFX. Fixtures are the importers' own test files' invented data.
"""

import unittest

from oikonome.sync import (competitors, mintimport, pdfimport, qifimport,
                           ynabimport)

from .test_competitors import COPILOT, MONARCH, SIMPLIFI
from .test_legacy_imports import MINT, QIF, YNAB
from .test_pdfimport import make_pdf
from .util import make_db, write_config

STATEMENT = ("Statement Period 01/01/2026 - 01/31/2026\n"
             "01/05 CORNER GROCER 13.00\n"
             "01/06 HARDWARE BARN 30.00\n"
             "01/07 BOOKSHOP 8.50\n")


class EachImporterTwice(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _twice(self, run):
        first, again = run(), run()
        n = first["imported"]
        self.assertGreater(n, 0)
        self.assertEqual(first["matched"], 0)
        self.assertEqual((again["imported"], again["matched"]), (0, n))

    def test_qif(self):
        self._twice(lambda: qifimport.import_qif(self.conn, "chk", QIF))

    def test_mint(self):
        self._twice(lambda: mintimport.import_mint(self.conn, "chk", MINT))

    def test_ynab(self):
        self._twice(lambda: ynabimport.import_ynab(self.conn, "chk", YNAB))

    def test_monarch(self):
        self._twice(lambda: competitors.import_monarch(
            self.conn, "chk", MONARCH))

    def test_copilot(self):
        self._twice(lambda: competitors.import_copilot(
            self.conn, "chk", COPILOT))

    def test_simplifi(self):
        self._twice(lambda: competitors.import_simplifi(
            self.conn, "chk", SIMPLIFI))

    def test_statement_pdf(self):
        pdf = make_pdf(STATEMENT)
        self._twice(lambda: pdfimport.import_pdf(self.conn, "chk", pdf))


if __name__ == "__main__":
    unittest.main()
