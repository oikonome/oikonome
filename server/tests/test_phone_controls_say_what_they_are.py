"""A phone control says what it is and what state it is in.

A touchable with no role is named by its text, but a screen reader never
says "button". The ☐/☑ glyph checkboxes carry a checkbox role and checked
state, the ▸/▾ rows that open a section carry an expanded state, and
neither reads its glyph aloud by its symbol name. A transaction row has a
role, keeps its decorative marks (⟳ ✂ 🏢 📎) silent, offers multi-select
other than by long press and says its selection in words, not only as a
glyph. The bill row's ✕ and ↺ flags sit outside the row's own touchable,
where a screen reader can reach them. (specs/accessibility.md, requirement 7)
"""

import re
import unittest

from tests.a11y_source import MOB, elements, sources

TOUCH = ("Pressable", "TouchableOpacity")
_SPREAD_ROLE = re.compile(r"\{\.\.\.(checkbox|disclosure)\(")


def _has_role(e):
    return e.has("accessibilityRole", "role") or bool(_SPREAD_ROLE.search(e.attrs))


class PhoneControlsSayWhatTheyAre(unittest.TestCase):
    def test_every_touchable_has_a_role(self):
        bad = [e.where() for p in sources(MOB)
               for e in elements(p, TOUCH) if not _has_role(e)]
        self.assertEqual(bad, [])

    def test_glyph_checkboxes_are_checkboxes_with_a_state_and_a_name(self):
        bad = []
        for p in sources(MOB):
            for e in elements(p, TOUCH + ("Text",)):
                if not e.has("onPress") or not re.search(r'"[☐☑]"', e.children):
                    continue
                if e.tag == "Text" and not re.search(r'"[☐☑]"\s*:\s*"[☐☑]"', e.children):
                    continue
                spread = re.search(r"\{\.\.\.checkbox\(", e.attrs)
                explicit = (e.attr("accessibilityRole") == '"checkbox"'
                            and "checked:" in (e.attr("accessibilityState") or "")
                            and e.has("accessibilityLabel"))
                # a glyph that marks a row (the ledger row's selection, a
                # setup step that is done) rather than a toggle of its own:
                # the row says it in words and carries the state
                marks = (e.has("accessibilityLabel")
                         and ("selected" in (e.attr("accessibilityState") or "")
                              or '"link"' in (e.attr("accessibilityRole") or "")))
                if not (spread or explicit or marks):
                    bad.append(e.where())
        self.assertEqual(bad, [])

    def test_glyph_disclosures_say_whether_they_are_open(self):
        bad = []
        for p in sources(MOB):
            for e in elements(p, TOUCH):
                if not re.search(r'\?\s*"▾ ?"\s*:\s*"▸ ?"', e.children):
                    continue
                if not (re.search(r"\{\.\.\.disclosure\(", e.attrs)
                        or "expanded:" in (e.attr("accessibilityState") or "")):
                    bad.append(e.where())
        self.assertEqual(bad, [])

    def test_the_shared_helpers_set_role_state_and_name(self):
        src = (MOB / "lib" / "a11y.ts").read_text()
        self.assertRegex(src, r'accessibilityRole: "checkbox", accessibilityLabel: label,'
                              r'\s+accessibilityState: \{ checked \}')
        self.assertRegex(src, r'accessibilityRole: "button", accessibilityLabel: label,'
                              r'\s+accessibilityState: \{ expanded \}')

    def test_a_transaction_row_speaks_words_and_offers_select(self):
        src = (MOB / "components" / "txn-row.tsx").read_text()
        row = next(e for e in elements(MOB / "components" / "txn-row.tsx", ("Pressable",), src))
        for want in ('accessibilityRole="button"', "accessibilityLabel={label}",
                     "{ selected }", 'name: "longpress"', '"Deselect" : "Select"',
                     'actionName === "longpress") onLongPress?.(t)'):
            self.assertIn(want, row.attrs)
        self.assertIn("...marks.map((m) => spoken(m.text))", src)
        spoken = src[src.index("function spoken("):]
        spoken = spoken[:spoken.index("\n}\n")]
        for glyph in "⟳↩⚑🏢✂":
            self.assertIn(glyph, spoken)
        self.assertIn('"📎") return "receipt"', spoken)

    def test_the_bill_row_flags_are_buttons_beside_the_row(self):
        p = MOB / "app" / "(tabs)" / "bills.tsx"
        src = p.read_text()
        fn = src[src.index("function BillRow("):]
        fn = fn[:fn.index("\n}\n")]
        press = [e for e in elements(p, ("Pressable",), fn)]
        # the row's own target closes itself: nothing nests inside it
        self.assertIn("style={({ pressed }) => [s.billPress", press[0].attrs)
        self.assertEqual(press[0].children, "")
        self.assertIn("accessibilityLabel={`${b.payee}, ${amountText}", press[0].attrs)
        names = sorted(e.attr("accessibilityLabel") for e in press[1:])
        self.assertEqual(names, ['"Dismiss this hint"', '"Dismiss"', '"Restore this flag"'])
        for e in press[1:]:
            self.assertIn('accessibilityRole="button"', e.attrs)
        self.assertNotRegex(fn, r"<Text[^>]*onPress")


if __name__ == "__main__":
    unittest.main()
