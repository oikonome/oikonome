"""Each data row of a CSV import is bounded before it is built.

The header and the mapping card's samples are bounded at the door, but a
short header can still be followed by one enormous data row: csv turns a
line of millions of commas into millions of cells before the row cap has
counted it as one, in the process every household shares. Each line is
read under a character budget and each row may carry only a few cells
past its header; past either the file is refused with a reason, and an
ordinary file — a memo with a stray comma, a quoted multi-line memo —
imports as before.
"""

import unittest

from oikonome.sync import csvimport

from .util import make_db, write_config

MAPPING = {"date": "Date", "name": "Description", "amount": "Amount"}
HEAD = "Date,Description,Amount\n"


class DataRowBoundTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _import(self, text):
        return csvimport.import_csv(self.conn, "chk", text, MAPPING)

    def test_an_oversized_data_line_is_refused_with_its_line_number(self):
        text = (HEAD + "07/01/2026,COFFEE,-4.50\n"
                + "07/02/2026,x,1" + "," * (csvimport.MAX_ROW_CHARS + 10)
                + "\n")
        with self.assertRaisesRegex(ValueError, "line 3 .* longer than"):
            self._import(text)

    def test_a_row_far_wider_than_its_header_is_refused(self):
        text = (HEAD + "07/01/2026,x,1"
                + "," * (csvimport.MAX_ROW_OVERFLOW + 1) + "\n")
        with self.assertRaisesRegex(ValueError, "cells under a 3-column"):
            self._import(text)

    def test_csv_import_bounds_a_quoted_multiline_record(self):
        # Two shapes of one record over many short lines: a quoted field
        # that keeps going, and a quoted cell per line. Each is refused by
        # the record's character budget, before csv has built its cells.
        n = csvimport.MAX_ROW_CHARS  # far more lines than the budget allows
        shapes = {
            "one long field": '07/02/2026,"' + "ab\n" * n + '",1\n',
            "a cell per line": "07/02/2026," + '"ab\n",' * n + "1\n",
        }
        for name, record in shapes.items():
            with self.subTest(name):
                text = HEAD + "07/01/2026,COFFEE,-4.50\n" + record
                with self.assertRaisesRegex(
                        ValueError, "record starting at line 3 .* longer than"):
                    self._import(text)

    def test_the_budget_is_per_record_not_per_file(self):
        # Many ordinary multi-line rows together run far past one record's
        # budget; read straight through the reader, no database needed.
        n = csvimport.MAX_ROW_CHARS // 20
        rows = "".join(f'07/01/2026,"MEMO {i}\nLINE TWO",-1.00\n'
                       for i in range(n))
        reader = csvimport.bounded_dict_reader(HEAD + rows)
        self.assertEqual(sum(1 for _ in reader), n)

    def test_ordinary_rows_still_import(self):
        text = (HEAD
                + "07/01/2026,COFFEE,-4.50,MEMO, CUT\n"     # overflow cells
                + '07/02/2026,"MEMO\nSECOND LINE",-2.00\n'  # quoted newline
                + "07/03/2026,LAST,-1.00")                  # no final \n
        r = self._import(text)
        self.assertEqual(r["imported"], 3, r)


if __name__ == "__main__":
    unittest.main()
