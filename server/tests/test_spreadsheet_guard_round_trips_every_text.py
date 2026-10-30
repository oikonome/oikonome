"""The full-data export's spreadsheet guard comes off a restore exactly.

The export puts an apostrophe in front of a cell, or a piece after ';', a
tab or a line break, that a spreadsheet would read as a formula; the same
ZIP is the restore input, so the restore takes those marks off. A mark has
to be told apart from an apostrophe the household typed ("a;'=b", a
signature line "'-- me"), so the export adds one more in front of one of
those, and the restore removes exactly one: every text comes back byte for
byte. An archive written before the piece guard existed gets the reverse
it was written for, which never touches text after a ';'.
"""

import random
import unittest

from oikonome.sync import restore
from oikonome.web.pages import _csv_cell

ALPHABET = list(";\t\n\r'= +-@\"a﻿")


class SpreadsheetGuardRoundTrips(unittest.TestCase):
    def _round(self, s):
        tok = restore._GUARD_V2.set(True)
        try:
            return restore._unescape(_csv_cell(s))
        finally:
            restore._GUARD_V2.reset(tok)

    def test_typed_apostrophes_survive(self):
        for s in ("a;'=b", "Call\n'+1 217 555", "note:\n'-- sig",
                  "a;\t'=b", "'=a", "'-ACH", "''=1", "'\tabc",
                  "\t'\t=1", "x;\"=1", "a;'x;=1", "-ACH DEBIT"):
            self.assertEqual(self._round(s), s, repr(s))

    def test_every_short_text_round_trips(self):
        rnd = random.Random(1729)
        for _ in range(20000):
            s = "".join(rnd.choice(ALPHABET)
                        for _ in range(rnd.randint(0, 9)))
            self.assertEqual(self._round(s), s, repr(s))

    def test_the_guard_still_neutralises_every_piece(self):
        self.assertEqual(_csv_cell("A;=1+1"), "A;'=1+1")
        self.assertEqual(_csv_cell("a;'=b"), "a;''=b")

    def test_an_older_archive_keeps_its_own_reverse(self):
        # written by the head rule alone: no piece marks, no doubling
        self.assertFalse(restore._GUARD_V2.get())
        self.assertEqual(restore._unescape("a;'=b"), "a;'=b")
        self.assertEqual(restore._unescape("'-ACH DEBIT"), "-ACH DEBIT")
        self.assertEqual(restore._unescape("''=1"), "''=1")


if __name__ == "__main__":
    unittest.main()
