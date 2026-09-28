"""Budget: the answer before the editor, and every number said once.

The page must not lead with the planner FORM and bury "am I on plan"
beneath it — editing is the rare act, checking is the daily one. And the
plan card must not state each figure three times over: the balance-bar
legend, the bar tooltips, and the panel inputs. Pinned in source because
both are layout guarantees no runtime assertion can reach.
"""

import pathlib
import unittest

import oikonome


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


class BudgetOrderTests(unittest.TestCase):
    def setUp(self):
        try:
            self.page = _src("pages/Budget.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_tracking_comes_before_the_planner(self):
        self.assertLess(self.page.index("This month"),
                        self.page.index("Your plan"),
                        "the daily question must not sit below the rare one")

    def test_the_hero_is_the_shared_plan_flow_line(self):
        """The same sentence Today and the email speak — including the savings
        term, whose absence made the email's flow land on a number its own
        terms could not produce."""
        hero = self.page[:self.page.index("This month")]
        for term in ("in", "bills", "to spend", "saved", "excess"):
            self.assertIn(f"> {term}</span>", hero.replace('"', ""),
                          f"the flow line is missing {term!r}")

    def test_the_excess_caption_is_not_repeated_under_the_bars(self):
        self.assertNotIn("income minus typical", self.page,
                         "the hero's last term already says this")


class PlannerLegendTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("components/BudgetPlanner.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_no_second_legend_restates_the_bars(self):
        """A legend row listing each segment's dollars and percent restates
        figures the bars carry inline and the panels hold as editable
        inputs."""
        self.assertNotIn("Savings / Investment <b style=", self.src)
        self.assertNotIn('{x.label} <b style={{ color: "var(--ink)" }}>',
                         self.src)

    def test_the_two_facts_the_bars_cannot_show_survive(self):
        self.assertIn("Excess cash", self.src)
        self.assertIn("Plan exceeds income by", self.src)


class BucketRulesTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("components/BudgetFineTune.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_it_is_a_full_card_named_for_what_it_is(self):
        self.assertIn("<h2 style={{ marginTop: 0 }}>Bucket rules", self.src)
        self.assertNotIn("<details className=\"card\">", self.src,
                         "the rules card is not a collapsed detail")

    def test_no_multiselect_and_no_comma_separated_merchant_text(self):
        """⌘/Ctrl-click is unusable on touch, and comma text hides typos."""
        self.assertNotIn("<select multiple", self.src)
        self.assertNotIn("comma-separated", self.src)

    def test_categories_and_merchants_are_removable_chips(self):
        self.assertIn('className="chip-x"', self.src)
        self.assertIn('className="addchip"', self.src)

    def test_one_editor_opens_at_a_time(self):
        self.assertIn("const [editing, setEditing] = useState<number | null>(null)",
                      self.src)
        self.assertIn('className="row-menu"', self.src)


class BillTweaksTests(unittest.TestCase):
    def test_it_is_a_line_not_a_card(self):
        try:
            src = _src("components/BillTweaksCard.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")
        self.assertNotIn('className="card"', src,
                         "a whole card to restate what the Bills page owns")
        self.assertIn("if (!parts.length) return null;", src,
                      "it must say nothing when there is nothing to say")
