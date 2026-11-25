"""A restored cell far longer than anything the export writes is refused
before any pattern or parser reads it, and in bounded time.

The restore runs inside the web process, and a regular expression holds
the interpreter lock while it runs: one figure whose pattern backtracks on
a long run of digits froze every household the process serves for as long
as it took, which for a cell near the reader's size limit was hours. No
figure, count or day the export writes is longer than a few dozen
characters, so a longer cell is refused by its length first, whatever
pattern reads the short ones.
"""

import time
import unittest

from oikonome.sync import restore

# well past any real cell, well short of the reader's size limit
_LONG = 100_000
_BOUND = 0.25     # seconds; the refusal itself takes microseconds


class ALongCell(unittest.TestCase):
    def _refused_quickly(self, call, exc):
        t = time.perf_counter()
        with self.assertRaises(exc):
            call()
        took = time.perf_counter() - t
        self.assertLess(took, _BOUND, f"took {took:.3f} s")
        return took

    def test_a_long_amount_is_not_a_number(self):
        for v in ("1" * _LONG + "x", "1" * _LONG, " " * _LONG + "1",
                  "1." + "1" * _LONG + "x"):
            with self.subTest(v=v[:8]):
                self._refused_quickly(lambda: restore._money(v),
                                      restore._NotANumber)

    def test_a_long_count_is_not_a_number(self):
        v = "1" * _LONG + "x"
        self._refused_quickly(lambda: restore._i(v, 0), restore._NotANumber)

    def test_a_long_bounded_figure_is_not_a_number(self):
        v = "1" * _LONG + "x"
        self._refused_quickly(
            lambda: restore._numeric(v, "miles", 10, 1), restore._NotANumber)

    def test_a_long_almost_day_is_unreadable(self):
        for v in ("2026-01-01T" + "1" * _LONG, "2026-01-01" + " " * _LONG + "x",
                  "2026-01-01 10:00:00" + "0" * _LONG):
            with self.subTest(v=v[:12]):
                self._refused_quickly(lambda: restore._req_date(v),
                                      restore._Unreadable)
                t = time.perf_counter()
                self.assertIsNone(restore._ts(v))
                self.assertIsNone(restore._opt_date(v))
                self.assertLess(time.perf_counter() - t, _BOUND)

    def test_a_column_does_not_take_a_long_figure_or_day(self):
        cases = ({"udt_name": "int4", "precision": None, "scale": None,
                  "chars": None}, "1" * _LONG), (
                 {"udt_name": "numeric", "precision": 14, "scale": 2,
                  "chars": None}, "1" * _LONG + "x"), (
                 {"udt_name": "date", "precision": None, "scale": None,
                  "chars": None}, "2026-01-01" + "1" * _LONG), (
                 {"udt_name": "timestamptz", "precision": None, "scale": None,
                  "chars": None}, "2026-01-01T" + "1" * _LONG)
        for col, v in cases:
            with self.subTest(col=col["udt_name"]):
                t = time.perf_counter()
                self.assertFalse(restore._takes(col, v))
                self.assertLess(time.perf_counter() - t, _BOUND)

    def test_the_figure_pattern_is_linear_even_without_the_cap(self):
        # the length cap is the guard; the pattern must not need it
        v = "1" * 20_000 + "x"
        t = time.perf_counter()
        self.assertIsNone(restore._FIGURE.fullmatch(v))
        self.assertLess(time.perf_counter() - t, 0.05)

    def test_every_figure_and_day_the_export_writes_still_reads(self):
        self.assertEqual(restore._f("0.30000000000000004"),
                         0.30000000000000004)
        self.assertEqual(restore._f("-1.2345678901234567e-05"),
                         -1.2345678901234567e-05)
        self.assertEqual(restore._i("12.0"), 12)
        self.assertIsNotNone(
            restore._ts("2026-03-02 10:00:00.123456+05:30"))


if __name__ == "__main__":
    unittest.main()
