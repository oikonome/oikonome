"""A money cell written with a decimal comma is read as that amount.

An export from a locale that writes "12,50" or "1.234,56" used to have
every comma deleted, so a $12.50 charge was stored as $1,250 and
"1.234,56" as 1.23456 — plausible, silently wrong numbers that flow into
every total and into deduplication against the bank feed. Each separator
convention is read explicitly; a cell that fits neither is refused.
"""
import unittest

from oikonome.sync import csvimport, ynabimport


class DecimalCommaAmountTests(unittest.TestCase):
    def test_both_conventions_read_as_the_amount_written(self):
        for cell, want in (("-12,50", -12.5), ("1.234,56", 1234.56),
                           ("12,5", 12.5), ("1,234.56", 1234.56),
                           ("$1,234", 1234.0), ("(45.00)", -45.0),
                           ("-12,345.67", -12345.67), ("+5", 5.0)):
            self.assertEqual(csvimport._parse_amount(cell), want, cell)

    def test_a_cell_fitting_neither_convention_is_refused(self):
        for cell in ("12,345,6", "1,2,3", "1,23,456"):
            with self.assertRaises(ValueError, msg=cell):
                csvimport._parse_amount(cell)

    def test_a_ynab_money_cell_reads_the_same_way(self):
        self.assertEqual(ynabimport._money("12,50"), 12.5)
        self.assertEqual(ynabimport._money("$1,234.56"), 1234.56)
        self.assertEqual(ynabimport._money(""), 0.0)


if __name__ == "__main__":
    unittest.main()
