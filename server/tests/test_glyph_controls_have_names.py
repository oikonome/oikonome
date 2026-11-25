"""Every control drawn as a glyph says what it does to a screen reader.

A ‹ that moves the month is read as "left-pointing angle quotation mark";
a ✕ is "multiplication x"; `title` is read unreliably and never shown on
touch. So a button, link, summary or tappable text whose only content is
symbols carries a name in plain words (`aria-label` on the web,
`accessibilityLabel` on the phone), and every phone Switch carries the
words of its row: without them focus lands on "switch, off" alone.
(specs/accessibility.md, requirement 1)
"""

import pathlib
import unittest

from tests.a11y_source import MOB, WEB, elements, glyph_only, sources

_WEB_CONTROLS = ("button", "a", "Link", "NavLink", "summary")
_WEB_CLICKABLE = ("span", "div", "td", "tr", "li")


def _web_unnamed():
    out = []
    for p in sources(WEB):
        for e in elements(p, _WEB_CONTROLS + _WEB_CLICKABLE):
            if e.tag in _WEB_CLICKABLE and not e.has("onClick"):
                continue
            if glyph_only(e.text()) and not e.has("aria-label", "aria-labelledby"):
                out.append(f"{e.where()} <{e.tag}> {e.text().strip()!r}")
    return out


def _phone_unnamed():
    out = []
    for p in sources(MOB):
        for e in elements(p, ("Pressable", "TouchableOpacity", "Text")):
            if e.tag == "Text" and not e.has("onPress", "onLongPress"):
                continue
            if glyph_only(e.text()) and not e.has("accessibilityLabel", "aria-label"):
                out.append(f"{e.where()} <{e.tag}> {e.text().strip()!r}")
    return out


class GlyphControlsHaveNames(unittest.TestCase):
    def test_the_reader_sees_a_glyph_control(self):
        """The check reads the shapes the screens use (proved on a
        fixture, so a passing scan is not a scan that saw nothing)."""
        src = (
            '<button onClick={() => go(-1)} title="previous">‹</button>\n'
            '<button aria-label="Next month" onClick={() => go(1)}>›</button>\n'
            '<button onClick={x}>{open ? "▾" : "▸"}</button>\n'
            '<button onClick={x}>{label} ✕</button>\n'
            '<Text style={{ a: 1 }}\n  onPress={() => del(i)}>\n  ✕\n</Text>\n'
            '<Pressable onPress={f}><Text style={s.t}>‹</Text></Pressable>\n'
        )
        p = pathlib.Path("fixture.tsx")
        seen = [(e.tag, glyph_only(e.text()),
                 e.has("aria-label", "accessibilityLabel"))
                for e in elements(p, ("button", "Text", "Pressable"), src)
                if e.tag != "Text" or e.has("onPress")]
        self.assertEqual(seen, [
            ("button", True, False), ("button", True, True),
            ("button", True, False), ("button", False, False),
            ("Text", True, False), ("Pressable", True, False)])

    def test_every_web_glyph_control_has_a_name(self):
        self.assertEqual(_web_unnamed(), [])

    def test_every_phone_glyph_control_has_a_name(self):
        self.assertEqual(_phone_unnamed(), [])

    def test_every_phone_switch_says_what_it_switches(self):
        bare = [e.where() for p in sources(MOB)
                for e in elements(p, ("Switch",))
                if not e.has("accessibilityLabel")]
        self.assertEqual(bare, [])

    def test_the_month_and_year_arrows_say_the_same_words_on_both(self):
        """The lens and ledger steppers are the main control on their
        screens; both apps name them "Previous month" / "Next year"."""
        cases = [
            (WEB / "pages" / "Transactions.tsx", ("Previous month", "Next month")),
            (MOB / "app" / "(tabs)" / "transactions.tsx", ("Previous month", "Next month")),
            (MOB / "app" / "month.tsx", ("Previous month", "Next month")),
            (MOB / "app" / "year.tsx", ("Previous year", "Next year")),
            (MOB / "app" / "(tabs)" / "index.tsx", ("Previous day", "Next day")),
            (WEB / "pages" / "Home.tsx", ("Previous ${lensUnit(lens)}",
                                          "Next ${lensUnit(lens)}")),
        ]
        for path, names in cases:
            src = path.read_text()
            for n in names:
                self.assertIn(n, src, path.name)
        home = (WEB / "pages" / "Home.tsx").read_text()
        self.assertIn('(l === "today" ? "day" : l)', home)


if __name__ == "__main__":
    unittest.main()
