"""Both clients print three figures the server decides, and work none out.

An income scenario's month ("Active now: $X/mo"), an envelope's bar and a
merge offer's "After merge" box each used to be arithmetic in the client:
a cadence table, a division by a client-picked divisor, two sides added
with a "+" typed in front of a change that can be below zero. The server
now sends `monthly`, `period_frac` and `merged`; a client that is handed
none prints nothing rather than a second answer of its own.
"""

import pathlib
import unittest

import oikonome

_ROOT = pathlib.Path(oikonome.__file__).parent.parent.parent


def _src(*rel: str) -> str:
    return _ROOT.joinpath(*rel).read_text()


class TheWeb(unittest.TestCase):
    def test_the_scenario_month_is_the_servers(self):
        src = _src("webapp", "src", "components", "IncomeScenarioCard.tsx")
        self.assertIn("live.monthly", src)
        self.assertNotIn("weekly: 4", src)

    def test_the_envelope_bar_is_the_servers_fraction(self):
        src = _src("webapp", "src", "pages", "Bills.tsx")
        self.assertIn("b.period_frac", src)
        self.assertNotIn("/ b.amount", src)

    def test_the_merged_box_is_the_servers(self):
        src = _src("webapp", "src", "pages", "Merchants.tsx")
        self.assertIn("m.total_delta", src)
        self.assertNotIn("f.total + i.total", src)


class ThePhone(unittest.TestCase):
    def setUp(self):
        try:
            self.budget = _src("mobile", "src", "app", "budget.tsx")
            self.bills = _src("mobile", "src", "app", "(tabs)", "bills.tsx")
            self.merchants = _src("mobile", "src", "app", "merchants.tsx")
        except FileNotFoundError:
            self.skipTest("mobile/ not present")

    def test_the_scenario_month_is_the_servers(self):
        # the server's month whenever the key is sent; the old conversion
        # only for a server that sends none
        self.assertIn("live.monthly !== undefined ? live.monthly", self.budget)
        self.assertIn("older-server path: stands in for `monthly`", self.budget)

    def test_the_envelope_bar_is_the_servers_fraction(self):
        self.assertIn("<Meter frac={b.period_frac ??", self.bills)
        self.assertIn("b.period_frac === undefined && !!b.pool", self.bills)

    def test_the_merged_box_is_the_servers(self):
        self.assertIn("m.total_delta", self.merchants)
        self.assertNotIn("+{money(f.total", self.merchants)


if __name__ == "__main__":
    unittest.main()
