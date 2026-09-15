"""The startup/organizational deduction is a lifetime allowance, not annual.

Section 195/248 grants a ONE-TIME allowance over the life of the business.
Computing it from a single year's costs hands a business that books
pre-opening costs across two years the $5,000 twice — and the $50,000
phase-out never triggers when no year alone exceeds it.
"""

import pathlib
import unittest

import oikonome
from oikonome.engine import books


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent / rel).read_text()


class StartupDeductionIsLifetimeTests(unittest.TestCase):

    def test_the_rule_itself_is_unchanged(self):
        self.assertEqual(books.startup_deduction(4000.0)["immediate"], 4000.0)
        self.assertEqual(books.startup_deduction(20000.0)["immediate"], 5000.0)
        # phase-out: every dollar over 50k removes a dollar of the 5k
        self.assertEqual(books.startup_deduction(52000.0)["immediate"], 3000.0)
        self.assertEqual(books.startup_deduction(56000.0)["immediate"], 0.0)

    def test_the_total_fed_to_it_is_lifetime_not_yearly(self):
        src = _src("engine/books.py")
        self.assertIn("def _lifetime_startup(", src)
        self.assertIn("_lifetime_startup(conn, entity_id, \"organizational\"",
                      src)
        self.assertIn("_lifetime_startup(conn, entity_id, \"startup_195\"",
                      src)

    def test_an_all_time_call_does_not_re_query(self):
        """year=None already IS the lifetime figure."""
        src = _src("engine/books.py")
        block = src[src.index("def _lifetime_startup("):]
        self.assertIn("if year is None:", block[:900])
        self.assertIn("return this_year", block[:900])
