"""Business money aggregates must drop non-primary (shadow) linked rows.

An account linked through TWO aggregators is two rows in one link group,
and the Business page assigns each row on its own — so both can carry
entity_id. Summing without dropping the non-primary counts one real
balance and one real transaction history twice.
"""

import pathlib
import unittest

import oikonome
from oikonome.engine import books


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent / rel).read_text()


class ShadowAccountDoubleCountTests(unittest.TestCase):
    """Every other money aggregate in this codebase drops the non-primary
    before summing; the business ones must too."""

    def test_the_pnl_predicate_excludes_shadows(self):
        self.assertIn("app.shadow_ids", books._BIZ_TXN)

    def test_the_balance_sheet_excludes_shadows(self):
        src = _src("engine/selfemploy.py")
        block = src[src.index("def balance_sheet("):
                    src.index("def balance_sheet(") + 1400]
        self.assertIn("_NOT_SHADOW", block)

    def test_both_reuse_the_one_predicate(self):
        """Not a second hand-rolled copy — reporting._NOT_SHADOW is the one
        definition, so a change to the rule reaches every caller."""
        self.assertIn("reporting._NOT_SHADOW", _src("engine/books.py"))
        self.assertIn("reporting._NOT_SHADOW", _src("engine/selfemploy.py"))

    def test_the_spa_fallback_excludes_shadows_too(self):
        spa = (pathlib.Path(oikonome.__file__).parent.parent.parent
               / "webapp" / "src" / "pages" / "Business.tsx")
        if not spa.exists():
            self.skipTest("webapp/ not present")
        s = spa.read_text()
        i = s.index("function TaxSetAside")
        self.assertIn("!a.link || a.link.primary", s[i:i + 1200])
