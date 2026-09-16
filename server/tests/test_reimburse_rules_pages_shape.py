"""Reimbursements + Categorization rules.

Reimbursements states its own number — "how much am I owed?" is the reason
the list exists. A partial expectation is shown as progress on the rows that
have one, not as permanent form controls on every row.

Rules opens with the rules themselves, keeps its rarest action (rename a
category) below the content, and says how much each rule actually does.
"""

import pathlib
import unittest

import oikonome


def _src(rel: str) -> str:
    return (pathlib.Path(oikonome.__file__).parent.parent.parent
            / "webapp" / "src" / rel).read_text()


class ReimburseShapeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Reimburse.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_page_states_what_you_are_owed(self):
        self.assertIn("const owed", self.src)
        self.assertIn("owed back across", self.src)

    def test_each_charge_says_how_long_it_has_waited(self):
        """The fact that turns a list into a to-do — derivable from the date
        column, and useless until it is stated."""
        self.assertIn("function waited(", self.src)
        self.assertIn("waitedLabel(t.date)", self.src)

    def test_the_title_is_not_repeated_as_a_card_heading(self):
        self.assertNotIn("Awaiting reimbursement", self.src,
                         "the h1 and the card h2 must not say the same thing")
        self.assertIn("<h1>Reimbursements</h1>", self.src)
        self.assertEqual(1, self.src.count("<h1>"))

    def test_partial_is_progress_not_permanent_form_controls(self):
        self.assertIn("got {money(got)} of {money(expect)}", self.src)
        self.assertIn('className="minibar"', self.src)
        self.assertIn("edit expected", self.src)
        # no always-present checkbox: turning partial on lives in the strip
        self.assertIn("only part comes back", self.src)

    def test_the_row_has_one_menu_not_a_button_row(self):
        self.assertIn('className="row-menu"', self.src)
        self.assertIn('className="row-actions"', self.src)

    def test_each_candidate_deposit_states_why_it_is_a_candidate(self):
        self.assertIn("function why(", self.src)
        self.assertIn("exact amount", self.src)
        self.assertIn("days later", self.src)


class RulesShapeTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Rules.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_hero_states_the_count_and_its_provenance_split(self):
        self.assertIn("rule{total === 1", self.src)
        self.assertIn("yours ·", self.src)
        self.assertIn("built-in", self.src)

    def test_search_sits_above_the_groups_it_filters(self):
        hero = self.src[self.src.index("const total"):self.src.index("empty ? (")]
        self.assertIn("search merchants or categories", hero)

    def test_the_two_opening_paragraphs_are_gone(self):
        self.assertNotIn("Every rule the system is applying and why.", self.src)
        self.assertIn("Rules are learned,\n          never hand-written.",
                      self.src)

    def test_renaming_a_category_moved_below_the_content(self):
        self.assertLess(self.src.index("RuleGroup source=\"user\""),
                        self.src.index("Rename a custom category"),
                        "the page's rarest action came before its content")

    def test_a_rule_says_how_much_it_does(self):
        self.assertIn("Matches", self.src)
        self.assertIn("{r.disabled ? \"—\" : r.count}", self.src)

    def test_each_group_carries_its_own_explanation(self):
        self.assertIn("inferred by the local model; correcting one makes it yours",
                      self.src)
        self.assertIn("shipped starting points", self.src)

    def test_category_is_the_same_chip_the_ledger_uses(self):
        self.assertIn('className="cat-chip"', self.src)
        # unscoped, so both pages get the one style
        self.assertIn(".cat-chip{", _src("index.css"))

    def test_three_row_buttons_became_one_menu(self):
        self.assertIn('className="row-menu"', self.src)
        self.assertNotIn(">delete</button>", self.src)
        self.assertIn("off</span>", self.src)
