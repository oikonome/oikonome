"""Every control can be reached and pressed without a mouse.

An `<a>` with no href, or a `<tr onClick>`, is never a Tab stop, so the
Accounts ✎, "more ways to connect", the Spending "all N" toggle and the
Cash Flow year rows could not be opened from a keyboard; an `href="#"`
link that acts as a button is announced as a link. On the phone, a ✕
nested inside a field's Pressable is unreachable: a screen reader focuses
the outer touchable only. Each is now a real button (web: `button.as-link`,
which keeps the anchor's look; phone: a sibling Pressable with a name and
hitSlop). (specs/accessibility.md, requirement 3)
"""

import re
import unittest

from tests.a11y_source import MOB, WEB, elements, sources

_STATIC = ("tr", "td", "div", "span", "li", "p", "img", "b", "strong",
           "section", "ul", "table", "tbody", "h1", "h2", "h3", "h4", "header")

# A touchable inside a Pressable is reachable only through the outer one.
_PHONE_NESTED_LATER: set[str] = set()


class EveryControlIsReachable(unittest.TestCase):
    def test_no_web_link_without_a_destination_acts_as_a_button(self):
        bad = [e.where() for p in sources(WEB)
               for e in elements(p, ("a",))
               if e.has("onClick") and e.attr("href") in (None, '"#"')]
        self.assertEqual(bad, [])

    def test_no_web_element_takes_a_click_the_keyboard_cannot_give(self):
        """A plain element may carry onClick only as a mouse shortcut for a
        control inside it, as a scripted button (role, tabIndex and a key
        handler), or as a scrim hidden from assistive technology."""
        bad = []
        for p in sources(WEB):
            for e in elements(p, _STATIC):
                if not e.has("onClick"):
                    continue
                if e.attr("aria-hidden") == '"true"':
                    continue
                if e.has("role") and e.has("tabIndex") and e.has("onKeyDown"):
                    continue
                if re.search(r"<(button|input|select|a\s[^>]*href)", e.children):
                    continue
                bad.append(f"{e.where()} <{e.tag}>")
        self.assertEqual(bad, [])

    def test_the_named_controls_are_buttons_with_their_state(self):
        acc = (WEB / "pages" / "Accounts.tsx").read_text()
        self.assertRegex(acc, r'<button type="button" className="as-link mut acct-edit"'
                              r'[^>]*aria-expanded=\{editing\}')
        self.assertRegex(acc, r'aria-expanded=\{more\}\s+onClick=\{\(\) => setMore')
        sp = (WEB / "pages" / "Spending.tsx").read_text()
        self.assertIn('className="as-link" aria-expanded={allMerch}', sp)
        cf = (WEB / "pages" / "CashFlow.tsx").read_text()
        row = cf[cf.index("function YearRow("):]
        row = row[:row.index("\n}\n")]
        self.assertRegex(row, r'<button type="button" className="as-link" '
                              r'aria-expanded=\{open\}')
        self.assertIn("e.stopPropagation(); toggle();", row)
        css = (WEB / "index.css").read_text()
        self.assertIn("button:where(.as-link){", css)
        self.assertIn("button:not(.linkish):not(.as-link)", css)

    def test_no_phone_touchable_hides_inside_another(self):
        bad = []
        for p in sources(MOB):
            src = p.read_text()
            for e in elements(p, ("Pressable", "TouchableOpacity"), src):
                inner = [c for c in elements(p, ("Pressable", "TouchableOpacity", "Text"),
                                             e.children)
                         if c.has("onPress", "onLongPress")]
                if inner and e.where() not in _PHONE_NESTED_LATER:
                    bad.append(e.where())
        self.assertEqual(bad, [])

    def test_the_phone_field_clear_is_its_own_named_button(self):
        for name, label in (("select.tsx", "{`Clear ${title}`}"),
                            ("date-picker.tsx", '"Clear date"')):
            src = (MOB / "components" / name).read_text()
            tag = src[src.index("<Pressable style={s.clear}"):]
            tag = tag[:tag.index("</Pressable>")]
            for want in ("hitSlop={8}", 'accessibilityRole="button"',
                         "accessibilityLabel=" + label, ">✕</Text>"):
                self.assertIn(want, tag, name)


if __name__ == "__main__":
    unittest.main()
