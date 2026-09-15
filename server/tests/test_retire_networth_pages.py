"""Retire + Net Worth: the answer first, and no second hero.

The retirement age is the number the Retire page exists to produce, so it
comes before the inputs rather than sitting as one of four equal tiles under
two form cards. Net Worth gives its total more weight than the components
that make it up, and no card below grows a headline that competes with the
page's own.
"""

import pathlib
import unittest

import oikonome


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


class RetireTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Retirement.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_answer_comes_before_the_inputs(self):
        self.assertLess(self.src.index("Earliest retirement at"),
                        self.src.index("adjust ▾"),
                        "the page's own answer must precede its form")

    def test_assumptions_collapse_to_one_line(self):
        self.assertIn('className="assume"', self.src)
        self.assertNotIn("<h2>Assumptions</h2>", self.src)
        self.assertNotIn("<h2>Monthly saving</h2>", self.src)

    def test_the_permanent_legend_card_is_gone(self):
        """Terms are explained under the table that uses them, once — not
        in two paragraphs standing above it."""
        self.assertNotIn("<h2>How to read this</h2>", self.src)
        self.assertIn("largest annual draw that just lasts to", self.src)

    def test_the_target_is_stated_once_not_in_six_headings(self):
        self.assertLessEqual(self.src.count("{money(r.spend)}/yr"), 1,
                             "every other surface should say 'target'")
        self.assertIn("Earliest at target", self.src)
        self.assertIn("<th>At target?</th>", self.src)

    def test_a_failing_stress_row_colours_its_number(self):
        self.assertIn('s.max_spend_at_ref < r.spend ? "neg" : ""', self.src)

    def test_the_projection_chart_has_no_zoom_affordance(self):
        """The site has one timeframe vocabulary, the RangePicker — no
        chart advertises pan/zoom."""
        self.assertNotIn("zoomable", self.src)
        self.assertNotIn("scroll to zoom", self.src)


class NetWorthTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/NetWorth.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_one_hero_with_components_as_tiles(self):
        self.assertIn("Total net worth", self.src)
        self.assertIn('className="tile"', self.src)
        self.assertNotIn("financial + real estate + vehicles − mortgage",
                         self.src, "the tiles ARE the breakdown")

    def test_the_month_delta_comes_from_the_trend_already_shipped(self):
        self.assertIn("t[t.length - 1][1] - t[t.length - 2][1]", self.src)

    def test_the_profit_card_does_not_grow_a_second_hero(self):
        self.assertNotIn('fontSize: "2rem", fontWeight: 800', self.src)
        self.assertNotIn("Invested → Value", self.src,
                         "that repeated the table's own Total row")

    def test_the_chart_legend_became_a_boundary_label(self):
        self.assertNotIn("reconstructed (best guess)", self.src)
        self.assertNotIn("recorded (monthly balances + nightly snapshots)",
                         self.src)
        self.assertIn("reconstructed from holdings", self.src,
                      "the caveat survives, once, in the footnote")

    def test_the_property_table_drops_its_duplicate_net_row(self):
        self.assertNotIn("<b>Net</b>", self.src,
                         "the hero tile already states property net")
