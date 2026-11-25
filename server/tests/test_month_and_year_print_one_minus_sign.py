"""The Month and Year views print one minus sign, and a kept figure its plus.

The grid printed "−$300" (a true minus) while the same pages printed a
net, left over, savings rate and the biggest charges with a hyphen
("-$300", "-8.0%"), and the web's Year totals printed "$500" where the
phone printed "+$500". Every signed figure on these pages goes through
the shared signedMoney / signedPct twins.
"""
import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_PAGES = {
    "web month": _ROOT / "webapp/src/pages/MonthLens.tsx",
    "web year": _ROOT / "webapp/src/pages/YearLens.tsx",
    "phone month": _ROOT / "mobile/src/app/month.tsx",
    "phone year": _ROOT / "mobile/src/app/year.tsx",
}
# a hand-built sign, a transaction's amount negated in place (money()
# prints a hyphen for a negative), a net or left over through bare money(),
# or a rate glued to "%"
_HAND_SIGNED = [
    re.compile(r"money\(\s*-[rt]\.amount"),
    re.compile(r"\bmoney\(\s*net\b"),
    re.compile(r"\bmoney\(\s*(d\.totals|t)\.saved\b"),
    re.compile(r"\$\{\s*(d\.totals|t)\.rate\s*\}%"),
    re.compile(r"\{\s*t\.rate\s*\}%"),
    re.compile(r">= 0 \? \"\+\" : \"\""),
    re.compile(r">= 0 \? \"\" : \"−\""),
]


class OneMinusSignTests(unittest.TestCase):
    def test_no_signed_figure_is_built_by_hand(self):
        for name, path in _PAGES.items():
            src = path.read_text()
            for pat in _HAND_SIGNED:
                self.assertIsNone(pat.search(src),
                                  f"{name}: {pat.pattern}")

    def test_the_pages_use_the_shared_twins(self):
        for name, path in _PAGES.items():
            self.assertIn("signedMoney(", path.read_text(), name)
        for name in ("web year", "phone year"):
            self.assertIn("signedPct(", _PAGES[name].read_text(), name)


if __name__ == "__main__":
    unittest.main()
