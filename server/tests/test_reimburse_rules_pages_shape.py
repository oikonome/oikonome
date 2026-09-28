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
        # the wording lives in the helper both clients share (moneymath.ts,
        # twinned in mobile's pure.ts), fed the remainders the server sends
        m = self.src[self.src.index("function Matcher("):]
        self.assertIn("reimbCandidateWhy(c, live)", m)
        helper = _src("moneymath.ts")
        self.assertIn("exact amount", helper)
        self.assertIn("days later", helper)

    def test_the_headline_counts_each_charge_against_its_expected_amount(self):
        """A partial expectation (the insurer's share) is what is owed, as
        the Today strip and the email say; summing the charges' face value
        overstated it and then dropped straight to zero when the expected
        amount arrived and cleared the flag."""
        self.assertIn("const owed = reimbOwed(pending)", self.src)
        self.assertNotIn("a + Math.abs(t.amount)", self.src)

    def test_a_failed_link_keeps_the_matcher_open(self):
        """A failed link must not close the matcher and throw away the
        ticked deposits; a failed candidate search must say so rather than
        render nothing at all."""
        m = self.src[self.src.index("function Matcher("):]
        self.assertNotIn("onError: (e) => onDone(", m)
        self.assertIn("onError: (e) => setLinkErr(errText(e))", m)
        self.assertIn("cand.isError", m)


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

    def test_the_rule_picker_offers_no_flow_category(self):
        """/api/rules/set refuses a transfer, income or loan-payment rule,
        so offering those only produced an error after the pick."""
        self.assertIn("spendPickerGroups(cats)", self.src)
        self.assertNotIn("cats?.plaid_flow.map", self.src)

    def test_the_split_editor_offers_no_flow_category(self):
        """A split part cannot be a flow category either (the split
        endpoint refuses it), so the split editor gets its own list."""
        tbl = _src("components/TxnTable.tsx")
        self.assertIn("<SplitPanel t={t} catGroups={splitCatGroups}", tbl)
        self.assertIn("spendPickerGroups(cats.data)", tbl)



class ReimburseMatcherHonestyTests(unittest.TestCase):
    """The matcher links exactly what it says it will, names where each
    deposit landed, and a failed write never looks like a no-op."""

    def setUp(self):
        try:
            self.src = _src("pages/Reimburse.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_the_running_total_counts_ticks_the_filter_hides(self):
        """A filter change swaps the visible rows but not the selection; a
        total summed from the visible rows left a hidden tick out of
        "selected $X" while the button still linked it."""
        self.assertIn("useState<Map<string, number>>", self.src)
        self.assertIn("[...picked.values()].reduce(", self.src)
        self.assertNotIn("filter((c) => picked.has(c.id))", self.src)
        self.assertIn("hidden by the filter", self.src)
        self.assertIn("ids: [...picked.keys()]", self.src)

    def test_each_candidate_names_its_account_and_pending_state(self):
        """Candidates span every checking and savings account plus the
        charge's card; a bare payee cannot tell two of them apart."""
        m = self.src[self.src.index("function Matcher("):]
        self.assertIn("{c.account && <div", m)
        self.assertIn("c.pending ?", m)

    def test_a_deposit_before_the_charge_says_how_long_before(self):
        """A deposit months early is the least likely refund; labelling it
        "same day or earlier" made it read like the closest match."""
        helper = _src("moneymath.ts")
        self.assertNotIn("same day or earlier", self.src + helper)
        self.assertIn("days earlier", helper)
        self.assertIn('"same day"', helper)

    def test_unflag_and_partial_writes_report_a_failure(self):
        row = self.src[self.src.index("function PendingRow("):
                       self.src.index("function Matcher(")]
        unflag = row[row.index("const unflag"):row.index("const setPartial")]
        self.assertIn("onError", unflag)
        self.assertIn("toast(", unflag)
        partial = row[row.index("const setPartial"):row.index("const got")]
        on_err = partial[partial.index("onError"):]
        self.assertIn("toast(", on_err[:on_err.index("onSuccess")])


class RulesWritesAreConfirmedAndReportedTests(unittest.TestCase):
    def setUp(self):
        try:
            self.src = _src("pages/Rules.tsx")
        except FileNotFoundError:
            self.skipTest("webapp/ not present")

    def test_deleting_a_rule_asks_first(self):
        """A delete cannot be undone; every other destructive action on the
        web, and the mobile rules screen, asks before it acts."""
        self.assertIn("onDelete={() => confirmDelete(r.merchant)}", self.src)
        self.assertNotIn("onDelete={() => del.mutate(", self.src)
        cd = self.src[self.src.index("const confirmDelete"):]
        self.assertIn("window.confirm(", cd[:400])

    def test_a_refused_disable_or_delete_says_so(self):
        dis = self.src[self.src.index("const disable = useMutation"):
                       self.src.index("const del = useMutation")]
        self.assertIn("onError", dis)
        dl = self.src[self.src.index("const del = useMutation"):
                      self.src.index("const confirmDelete")]
        self.assertIn("onError", dl)

    def test_turning_a_rule_back_on_refreshes_the_ledger(self):
        """Re-enabling re-applies the rule to past transactions, so the
        ledger and the verdict are stale after it, like after an edit."""
        dis = self.src[self.src.index("const disable = useMutation"):
                       self.src.index("const del = useMutation")]
        self.assertIn('queryKey: ["txns"]', dis)
        self.assertIn('queryKey: ["today"]', dis)
