"""Two tables end each row with a control — the paycheck choice in Income
by source, "remove" in Stored W-2s — and at phone width their rows were
wider than the card, leaving the control a sideways scroll away. The cells
that only decorate the row (the share bar, the deposit count, the EIN) step
aside there, as the Spending tab's bar already does.
"""
import pathlib
import unittest

_WEB = pathlib.Path(__file__).resolve().parents[2] / "webapp" / "src"


class DecorationStepsAside(unittest.TestCase):
    def test_income_by_source_hides_its_bar_and_count_on_a_phone(self):
        src = (_WEB / "pages" / "CashFlow.tsx").read_text()
        rows = src[src.index("Income by source"):src.index("<CashCosts />")]
        self.assertTrue('<td className="c-sharebar"' in rows)
        self.assertTrue('<td className="num mut hide-m" style={{ fontSize: 12 }}>'
                        '{n}×</td>' in rows)
        css = (_WEB / "index.css").read_text()
        self.assertTrue("td.c-sharebar{display:none}" in css)

    def test_stored_w2s_hide_the_ein_on_a_phone(self):
        src = (_WEB / "pages" / "Import.tsx").read_text()
        self.assertTrue('<span className="sub hide-m"> · EIN {w.ein}</span>'
                        in src)


if __name__ == "__main__":
    unittest.main()
