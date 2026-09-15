"""the ledger row carries ONE control, and every action is named.

The row used to render three always-on controls — a biz toggle plus a 📝 and
a 📎 at 30% opacity — on every row, i.e. ~300 buttons on a 100-row page, most
of them meaning "nothing here yet", and what each one did lived in a
`title` tooltip, which a touch device never shows.

One ⋯ opens a strip naming every action in words. These assertions are on the
source because the guarantee is structural — a future edit that puts a bare
icon button back on the row, or that lets an action exist only as a tooltip,
should fail here rather than in a phone screenshot months later.
"""

import pathlib
import re
import unittest

import oikonome


def _src(rel: str) -> str:
    root = pathlib.Path(oikonome.__file__).parent.parent.parent / "webapp" / "src"
    return (root / rel).read_text()


class RowActionsTests(unittest.TestCase):
    def setUp(self):
        try:
            self.txn = _src("components/TxnTable.tsx")
        except FileNotFoundError:                      # slim checkout
            self.skipTest("webapp/ not present")

    def test_the_row_has_a_single_menu_control(self):
        self.assertIn('className="row-menu"', self.txn,
                      "the row's one control is missing")
        # the trio it replaced must not come back as always-rendered chrome
        self.assertNotIn('className="clip-btn"', self.txn,
                         "clip-btn is the old per-row ghost button")
        self.assertNotIn('className={"biz-toggle"', self.txn,
                         "the biz toggle belongs in the ⋯ strip now")

    def test_every_strip_action_is_named_in_words(self):
        strip = self.txn[self.txn.index("row-actions"):]
        strip = strip[:strip.index("</td></tr>")]
        for phrase in ("business expense", "note", "receipt",
                       "make recurring", "reimbursement"):
            self.assertIn(phrase, strip,
                          f"the strip must name {phrase!r} in words, not by icon")

    def test_the_menu_has_an_accessible_name_and_expanded_state(self):
        self.assertIn("aria-label={`actions for", self.txn)
        # the row is memoized, so the open state arrives as a prop — the
        # invariant is that the ⋯ button announces expanded/collapsed
        self.assertIn("aria-expanded={menuOpen}", self.txn)

    def test_category_reads_as_editable_without_a_tooltip(self):
        """A muted string that happened to be a <select> announced itself only
        on hover. The chip class carries the dotted underline + caret."""
        self.assertIn('className="cat-chip"', self.txn)
        self.assertIn("aria-label={`category:", self.txn)

    def test_the_reimbursement_flag_has_exactly_one_door(self):
        """It moved to the strip; leaving it in the category menu too is how
        two paths to one action drift apart."""
        self.assertNotIn('<option value="__flag__">', self.txn)
        self.assertNotIn('<option value="__unflag__">', self.txn)
        self.assertIn('"__flag__"', self.txn)          # still reachable

    def test_the_phone_grid_gives_the_menu_its_own_column(self):
        css = _src("index.css")
        self.assertIn('"date merch amt  clip"', css)
        self.assertIn('"cat  cat   cat  clip"', css)
        m = re.search(r"\.txn-table \.row-menu\{([^}]*)\}", css)
        self.assertIsNotNone(m, "no phone touch target for the row menu")
        self.assertIn("2.75rem", m.group(1), "touch target under 44px")
