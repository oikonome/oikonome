"""Over a window longer than a year the left-over-by-month chart names
every label's year, keeps its last label inside the drawing and writes no
amounts on bars too thin to carry them; Net Worth's short windows label
dates rather than two year marks at the left edge. An all-time chart read
"Mar Dec Sep Jun" with no year anywhere and amounts printed across bars.
The width rules are exercised by the phone's pure-logic tests.
"""
import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _fn(src: str, name: str) -> str:
    at = src.index(f"export function {name}(")
    return re.sub(r"\s+", " ", src[at:src.index("\n}\n", at)])


class LongRangeAxes(unittest.TestCase):
    def setUp(self):
        r = lambda rel: (_ROOT / rel).read_text()   # noqa: E731
        self.web_page = r("webapp/src/pages/CashFlow.tsx")
        self.mob_page = r("mobile/src/app/cashflow.tsx")
        self.web_bars = r("webapp/src/components/CashFlowCharts.tsx")
        self.mob_bars = r("mobile/src/components/net-bars.tsx")
        self.web_lib = r("webapp/src/moneymath.ts")
        self.mob_lib = r("mobile/src/lib/pure.ts")

    def test_past_a_year_every_label_names_its_year(self):
        for src, who in ((self.web_page, "web"), (self.mob_page, "phone")):
            self.assertTrue(
                "months !== null && months <= 12 ? pretty(key) "
                ": monthWithYear(key)" in src, who)
            self.assertFalse('key.slice(5, 7) === "01" || i === 0' in src,
                             who)

    def test_the_bars_follow_one_width_rule(self):
        for name in ("netBarsLabelWidth", "netBarsShowValues"):
            self.assertEqual(_fn(self.web_lib, name), _fn(self.mob_lib, name),
                             name)
        for src, who in ((self.web_bars, "web"), (self.mob_bars, "phone")):
            self.assertTrue("Math.ceil(labelW / slot)" in src, who)
            self.assertTrue("show && values" in src, who)
            self.assertTrue("x={labelX(x)}" in src, who)
            self.assertTrue("(n - 1 - i) % every === 0" in src, who)

    def test_net_worth_marks_years_only_over_several(self):
        web = (_ROOT / "webapp/src/pages/NetWorth.tsx").read_text()
        self.assertTrue('xticks={sliced.length > 24 ? "years" : undefined}'
                        in web)


if __name__ == "__main__":
    unittest.main()
