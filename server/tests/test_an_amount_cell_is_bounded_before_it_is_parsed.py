"""An amount cell is refused by length before any pattern reads it.

The thousands-separator and decimal-comma patterns backtrack polynomially
on a long run of digits and separators, so a crafted cell could hold an
import for seconds per row. No real amount is longer than a couple of
dozen characters; a longer cell is an error, answered at once."""
import time
import unittest

from oikonome.sync import csvimport


class AmountCellIsBounded(unittest.TestCase):
    def test_a_long_cell_is_refused_at_once(self):
        crafted = "1," + "111," * 20_000 + "x"
        t0 = time.monotonic()
        with self.assertRaises(ValueError):
            csvimport.parse_money_text(crafted)
        self.assertLess(time.monotonic() - t0, 0.05)

    def test_a_cell_past_the_bound_is_refused_even_when_well_formed(self):
        # before the bound this parsed as a number; a cell this long is
        # never a real amount
        with self.assertRaises(ValueError):
            csvimport.parse_money_text("1," + "000," * 12 + "000")

    def test_real_amounts_still_parse(self):
        # placeholder digits (1-2-3-4-5): a statement-precision figure here
        # reads as money copied off a real statement
        self.assertEqual(csvimport.parse_money_text("123,456.78"), 123456.78)
        self.assertEqual(csvimport.parse_money_text("-123.456,78"), -123456.78)
        self.assertEqual(csvimport.parse_money_text("12,50"), 12.5)
        # the longest sensible amount is well inside the bound
        self.assertEqual(csvimport.parse_money_text("-123,456,789,012,345.67"),
                         -123456789012345.67)
