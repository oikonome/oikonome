"""Net Worth and Cash Flow print a negative percent and a negative dollar
figure with the same minus the change beside them wears. A line read
"−$120 · -7.0% over this month": a true minus on the dollars, a hyphen on
the percent, and "over this month" where a person says "this month". The
helpers are exercised by the phone's pure-logic tests; this pins both
clients to them.
"""
import pathlib
import re
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _fn(src: str, name: str) -> str:
    at = src.index(f"export function {name}(")
    return re.sub(r"\s+", " ", src[at:src.index("\n}\n", at)])


class OneMinusPerLine(unittest.TestCase):
    def setUp(self):
        r = lambda rel: (_ROOT / rel).read_text()   # noqa: E731
        self.web_nw = r("webapp/src/pages/NetWorth.tsx")
        self.web_cf = r("webapp/src/pages/CashFlow.tsx")
        self.mob_nw = r("mobile/src/app/networth.tsx")
        self.mob_cf = r("mobile/src/app/cashflow.tsx")
        self.web_lib = r("webapp/src/moneymath.ts")
        self.mob_lib = r("mobile/src/lib/pure.ts")

    def test_the_helpers_are_the_same_on_both_clients(self):
        for name in ("signedPct", "signedMoney"):
            self.assertEqual(_fn(self.web_lib, name), _fn(self.mob_lib, name),
                             name)
        self.assertEqual(
            _fn((_ROOT / "webapp/src/components/RangePicker.tsx").read_text(),
                "overCaption").replace("Range", "TrendRange"),
            _fn(self.mob_lib, "overCaption"))

    def test_no_percent_is_signed_with_a_bare_plus_or_nothing(self):
        for src, who in ((self.web_nw, "web"), (self.mob_nw, "phone")):
            self.assertFalse('>= 0 ? "+" : ""}' in src, who)
            self.assertFalse('>= 0 ? "+" : ""}$' in src, who)
            self.assertTrue("signedPct(w.pct)" in src, who)
            self.assertTrue("overCaption(range)" in src, who)

    def test_a_crypto_return_reads_to_one_decimal_like_the_table_above(self):
        self.assertFalse("c.pct.toFixed(0)" in self.web_nw)
        self.assertFalse("c.pct.toFixed(0)" in self.mob_nw)

    def test_a_negative_cost_wears_a_true_minus(self):
        for src, who in ((self.web_cf, "web"), (self.mob_cf, "phone")):
            self.assertTrue("signedMoney(net, " in src, who)
            self.assertTrue("signedMoney(vsEarned, " in src, who)


if __name__ == "__main__":
    unittest.main()
