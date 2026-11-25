"""Web and phone print one field in one set of words.

The Month view's scaling line, an envelope's spill, Help's empty search
and the Matched footnote were worded one way on the web and another on the
phone; the phone also printed words of its own where the server sends
none (unknown income, a year with no bank data).
"""
import pathlib
import unittest

import oikonome

_ROOT = pathlib.Path(oikonome.__file__).parent.parent.parent


def _read(rel: str) -> str:
    p = _ROOT / rel
    if not p.exists():
        raise unittest.SkipTest(f"{rel} not present")
    return p.read_text()


class OneWordingTests(unittest.TestCase):
    def test_the_scaling_line_is_one_sentence(self):
        want = "Variable budgets auto-scaled from"
        self.assertNotIn("Heavy bill month", _read("webapp/src/pages/MonthLens.tsx"))
        self.assertIn(want, _read("webapp/src/pages/MonthLens.tsx"))
        self.assertIn(want, _read("mobile/src/app/month.tsx"))

    def test_an_envelope_spill_reads_past_the_pool(self):
        """One wording for a spill everywhere: "past the pool, counted in
        spending"."""
        self.assertNotIn("} overflow)", _read("webapp/src/pages/MonthLens.tsx"))
        for path in ("webapp/src/pages/MonthLens.tsx",
                     "mobile/src/app/month.tsx"):
            self.assertIn("past the pool, counted in spending", _read(path))
            self.assertNotIn("· over +", _read(path))

    def test_an_empty_help_search_names_what_was_typed(self):
        self.assertIn("No topics match “", _read("webapp/src/pages/Help.tsx"))
        self.assertIn("No topics match “", _read("mobile/src/app/help.tsx"))

    def test_the_matched_footnote_is_one_sentence(self):
        want = "real spending and"
        self.assertIn(want, _read("webapp/src/pages/Reimburse.tsx"))
        self.assertIn(want, _read("mobile/src/app/reimburse.tsx"))
        self.assertNotIn("real money again", _read("mobile/src/app/reimburse.tsx"))

    def test_the_phone_prints_no_words_the_server_did_not_send(self):
        self.assertNotIn('?? "income unknown"', _read("mobile/src/app/month.tsx"))
        self.assertNotIn("income and savings are unknown",
                         _read("mobile/src/app/year.tsx"))


    def test_only_a_month_under_way_is_so_far(self):
        """A planned (future) month is neither "so far" nor "Month to
        date" — nothing in it has happened yet."""
        for rel in ("webapp/src/pages/MonthLens.tsx", "mobile/src/app/month.tsx"):
            src = _read(rel)
            self.assertNotIn('status !== "final" && " so far"', src, rel)
            self.assertNotIn('status !== "final" ? " so far"', src, rel)
            self.assertNotIn('=== "final"\n                ? "Spending by category" : "Month to date', src, rel)
            self.assertIn('"in_progress"', src, rel)


if __name__ == "__main__":
    unittest.main()
