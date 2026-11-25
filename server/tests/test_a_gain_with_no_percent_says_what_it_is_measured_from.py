"""An Over-time gain measured from a point at or below zero has no percent,
and without one nothing says why it can exceed the whole net worth. The
window then says where it starts (specs/counted-figures.md, "A gain with no
percent says what it is measured from").
"""
import pathlib
import unittest

from oikonome.engine import reporting

_ROOT = pathlib.Path(__file__).resolve().parents[2]


class GainSaysItsBase(unittest.TestCase):
    def test_a_window_that_starts_below_zero_says_so(self):
        full = [["2008-03", -4200.0], ["2008-04", -3900.0],
                ["2026-10", 28000.0], ["2026-11", 31000.0]]
        w = reporting._networth_figures(full, full, 0, [], [], [])[
            "trend_windows"]["all"]
        self.assertEqual(w["gain"], 35200.0)       # more than the 31,000 held
        self.assertIsNone(w["pct"])
        self.assertEqual(w["from_text"], "from −$4,200 in 03/08")

    def test_a_window_with_a_percent_needs_no_base(self):
        full = [["2026-09", 40000.0], ["2026-10", 45000.0],
                ["2026-11", 50000.0]]
        for w in reporting._networth_figures(full, full, 0, [], [], [])[
                "trend_windows"].values():
            self.assertIsNone(w["from_text"])

    def test_a_window_that_starts_at_nothing_says_so(self):
        full = [["2026-10", 0.0], ["2026-11", 500.0]]
        w = reporting._trend_window(full, "all")
        self.assertEqual(w["from_text"], "from $0 in 10/26")

    def test_the_base_is_restated_when_the_gain_moves_to_the_accounts(self):
        # a property typed in inside the window: the gain is measured on
        # the financial accounts, which start below zero here
        full = [["2026-09", -2000.0], ["2026-10", 98000.0],
                ["2026-11", 99000.0]]
        fin = [["2026-09", -2000.0], ["2026-10", -2000.0],
               ["2026-11", -1000.0]]
        w = reporting._like_for_like(
            reporting._trend_window(full, "all"), full, fin,
            [("2026-10", "added", "Home")])
        self.assertIsNotNone(w["note"])
        self.assertEqual(w["gain"], 1000.0)
        self.assertEqual(w["from_text"], "from −$2,000 in 09/26")

    def test_both_clients_print_it(self):
        web = (_ROOT / "webapp/src/pages/NetWorth.tsx").read_text()
        mob = (_ROOT / "mobile/src/app/networth.tsx").read_text()
        self.assertTrue("w.from_text" in web)
        self.assertTrue("w.from_text" in mob)


if __name__ == "__main__":
    unittest.main()
