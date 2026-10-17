"""A CSV's header is bounded before anything is built over it.

Format detection, auto-mapping and the mapping card all read the header
before any importer runs, so the importers' own column cap comes too late:
one line of tens of millions of commas becomes tens of millions of cells,
walked by every detector, stored with the staged file and sent back to the
browser. The first line is read under a character budget and refused when
it passes it, the column cap applies at the door, and the mapping card's
sample rows are cut to the header's width.
"""

import unittest

from oikonome.sync import csvimport
from oikonome.web import bulk_import, pages

from .util import make_db, write_config


class HeaderBudgetTests(unittest.TestCase):
    def test_an_oversized_first_line_is_refused_without_parsing_it(self):
        text = "," * (csvimport.MAX_HEADER_CHARS + 10) + "\n1,2\n"
        with self.assertRaisesRegex(ValueError, "first line"):
            csvimport.read_header(text)

    def test_a_header_past_the_column_cap_is_refused(self):
        text = ",".join(f"c{i}" for i in range(csvimport.MAX_COLUMNS + 1))
        with self.assertRaisesRegex(ValueError, "columns"):
            csvimport.read_header(text + "\n")

    def test_an_ordinary_header_reads_as_before(self):
        text = 'Date,"Desc\nription",Amount\n2026-07-01,x,-1.00\n'
        self.assertEqual(csvimport.read_header(text),
                         ["Date", "Desc\nription", "Amount"])
        self.assertEqual(csvimport.read_header(""), [])
        self.assertEqual(csvimport.read_header("a,b"), ["a", "b"])

    def test_sample_rows_are_cut_to_the_header_and_stop_at_the_budget(self):
        text = ("A,B\n" + "1,2,3,4\n"
                + "," * (csvimport.MAX_SAMPLE_CHARS + 10) + "\n5,6\n")
        self.assertEqual(csvimport.sample_rows(text, ["A", "B"]),
                         [["1", "2"]])

    def test_folder_analysis_does_not_parse_an_oversized_header(self):
        data = ("," * (csvimport.MAX_HEADER_CHARS + 10) + "\n").encode()
        self.assertEqual(bulk_import._ynab_accounts(data), 0)


class ImportDoorTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _dispatch(self, text):
        return pages._dispatch_import_unlocked(
            self.conn, "chk", "bank", "wide.csv", text.encode(),
            role="owner")

    def test_an_oversized_header_is_an_import_error_and_stages_nothing(self):
        text = "," * (csvimport.MAX_HEADER_CHARS + 10) + "\nx,y\n"
        result, need = self._dispatch(text)
        self.assertIsNone(need)
        self.assertIn("first line", result.get("error", ""))
        n = self.conn.execute(
            "SELECT count(*) AS n FROM import_staging").fetchone()["n"]
        self.assertEqual(n, 0)

    def test_the_mapping_card_sample_is_as_wide_as_the_header(self):
        text = "Foo,Bar\n" + "a,b," + "," * 5000 + "\n"
        result, need = self._dispatch(text)
        self.assertIsNotNone(need, result)
        self.assertEqual(need["header"], ["Foo", "Bar"])
        self.assertEqual(need["sample"], [["a", "b"]])


if __name__ == "__main__":
    unittest.main()
