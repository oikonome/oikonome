"""Text reads at 4.5:1 and a field's edge at 3:1, in both themes of both apps.

Every ratio here is computed from the theme tokens themselves (the web's
`webapp/src/index.css` and the phone's `mobile/src/lib/theme.ts`) with the
WCAG 2.x relative-luminance formula, so a token edited later is measured
as it ships. It covers the text tokens on a card, the page and a hovered
row; status pills, measured with their tint laid over the card colour, so
they read the same on any surface; the amber notice; the footer; field
edges; the phone's destructive and lockout buttons; and the Bills
calendar's past days, which carry no opacity (specs/accessibility.md,
requirement 5).
"""

import re
import unittest

from tests.a11y_source import MOB, WEB, elements, sources

TEXT, EDGE = 4.5, 3.0
SURFACES = ("card", "bg", "hover")
# the sRGB luminance weights of red, green and blue (WCAG 2.x), in thousandths
WEIGHTS = (212.6 / 1000, 715.2 / 1000, 72.2 / 1000)


def _rgb(c):
    c = c.strip()
    if c.startswith("rgba("):
        r, g, b, a = (float(x) for x in c[5:-1].split(","))
        return (r, g, b), a
    h = c.lstrip("#")
    if len(h) == 3:
        h = "".join(ch * 2 for ch in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)), 1.0


def over(fg, bg):
    """`fg` (hex or rgba) painted over the opaque `bg`, as hex."""
    (f, a), (b, _) = _rgb(fg), _rgb(bg)
    return "#" + "".join(f"{round(f[i] * a + b[i] * (1 - a)):02x}" for i in range(3))


def _lum(c):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return sum(w * ch(v) for w, v in zip(WEIGHTS, _rgb(c)[0]))


def ratio(a, b):
    x, y = sorted((_lum(a), _lum(b)), reverse=True)
    return (x + 0.05) / (y + 0.05)


# ---- the web's tokens -------------------------------------------------

_CSS = (WEB / "index.css").read_text()


def _block(selector):
    i = _CSS.index(selector + "{")
    return _CSS[i:_CSS.index("}", i)]


def _vars(block):
    block = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    return {k: v.strip() for k, v in re.findall(r"--([\w-]+):([^;}]+)", block)}


WEB_DARK = _vars(_block(":root"))
WEB_LIGHT = {**WEB_DARK, **_vars(_block(":root[data-theme=light]"))}
WEB_OS_LIGHT = {**WEB_DARK, **_vars(_block("  :root:not([data-theme=dark])"))}


def resolve(theme, value):
    value = value.strip()
    while (m := re.fullmatch(r"var\(--([\w-]+)(?:,[^)]*)?\)", value)):
        value = theme[m.group(1)].strip()
    return value


def rule(selector):
    m = re.search(r"(?:^|[}\s])" + re.escape(selector) + r"\{([^}]*)\}", _CSS)
    assert m, selector
    return dict(re.findall(r"([\w-]+):([^;]+)", m.group(1)))


# ---- the phone's tokens -----------------------------------------------

_TS = (MOB / "lib" / "theme.ts").read_text()


def _palette(name):
    body = _TS[_TS.index(f"const {name} = {{"):]
    body = body[:body.index("\n};")]
    return dict(re.findall(r"^\s*(\w+):\s*\"(#[0-9a-fA-F]{3,6})\"", body, re.M))


PHONE_DARK = _palette("DARK")
PHONE_LIGHT = {**PHONE_DARK, **_palette("LIGHT")}

# the phone's names for the web's tokens
PHONE_NAME = {"ink": "text", "mut": "mut", "green": "good", "amber": "warn",
              "red": "bad", "blue": "accent", "card": "card", "bg": "bg",
              "hover": "hover", "field-line": "field", "line": "border",
              "entity": "entity", "danger": "danger"}

# (theme, web token, surface) triples allowed below 4.5:1; empty: every
# text token passes.
HELD: set = set()

THEMES = (("dark", WEB_DARK), ("light", WEB_LIGHT), ("light (os)", WEB_OS_LIGHT))


class TextAndFieldsHaveContrast(unittest.TestCase):
    def test_the_two_light_blocks_of_the_web_are_one_palette(self):
        self.assertEqual(_vars(_block(":root[data-theme=light]")),
                         _vars(_block("  :root:not([data-theme=dark])")))

    def test_web_text_tokens_read_on_every_surface(self):
        bad = []
        for name, t in THEMES:
            for fg in ("ink", "mut", "green", "amber", "red", "blue", "entity"):
                for bg in SURFACES:
                    r = ratio(resolve(t, t[fg]), resolve(t, t[bg]))
                    if r < TEXT and (name.split()[0], fg, bg) not in HELD:
                        bad.append(f"{name} {fg} on {bg}: {r:.2f}")
        self.assertEqual(bad, [])

    def test_web_status_pills_read_on_their_own_tint(self):
        """A pill lays its tint over the card colour itself, so it reads the
        same on a card, the page and a hovered row."""
        bad = []
        for name, t in THEMES:
            for kind in "gabrme":
                r_ = rule(f".pill.{kind}")
                fg = resolve(t, r_["color"])
                m = re.fullmatch(r"linear-gradient\((rgba\([^)]*\)),\1\),var\(--card\)",
                                 r_["background"].strip())
                self.assertIsNotNone(m, f".pill.{kind} {r_['background']}")
                under = over(m.group(1), resolve(t, t["card"]))
                if (r := ratio(fg, under)) < TEXT:
                    bad.append(f"{name} .pill.{kind}: {r:.2f}")
        self.assertEqual(bad, [])

    def test_past_calendar_days_keep_their_number_readable(self):
        r_ = rule(".cal-cell.past")
        self.assertNotIn("opacity", r_)
        for name, t in THEMES:
            # the date is --mut straight on the card the grid sits in
            self.assertGreaterEqual(ratio(t["mut"], t["card"]), TEXT, name)

    def test_the_amber_notice_reads_in_both_themes(self):
        r_ = rule(".note")
        self.assertNotIn("#", r_["color"], "the notice's ink is a theme token")
        for name, t in THEMES:
            for bg in ("card", "bg"):
                under = over(r_["background"], resolve(t, t[bg]))
                self.assertGreaterEqual(ratio(resolve(t, r_["color"]), under), TEXT,
                                        f"{name} on {bg}")

    def test_the_footer_is_not_dimmed(self):
        foot = list(elements(WEB / "App.tsx", ("footer",)))
        self.assertTrue(foot)
        for e in foot:
            self.assertNotIn("opacity", e.attrs)
        for name, t in THEMES:
            self.assertGreaterEqual(ratio(t["mut"], t["bg"]), TEXT, name)

    def test_web_fields_draw_their_edge_at_three_to_one(self):
        for sel in ("input,select", "textarea"):
            self.assertIn("var(--field-line)", rule(sel)["border"], sel)
        for name, t in THEMES:
            for bg in SURFACES:
                self.assertGreaterEqual(ratio(t["field-line"], t[bg]), EDGE, f"{name} {bg}")

    def test_web_labels_on_fills_read(self):
        """White on the primary and the destructive fill; the budget bar's
        amber segments carry a percentage in the card colour."""
        for name, t in THEMES:
            self.assertGreaterEqual(ratio("#ffffff", t["btn"]), TEXT, name)
            self.assertGreaterEqual(ratio("#ffffff", t["danger"]), TEXT, name)
            self.assertGreaterEqual(ratio(t["card"], t["amber"]), TEXT, name)
        bp = (WEB / "components" / "BudgetPlanner.tsx").read_text()
        self.assertIn('color: "var(--card)", fontSize: 10,', bp)
        for p in sources(WEB):
            for e in elements(p, ("button",)):
                self.assertNotIn('background: "var(--red)"', e.attrs, e.where())

    def test_phone_text_tokens_read_on_every_surface(self):
        bad = []
        for name, t in (("dark", PHONE_DARK), ("light", PHONE_LIGHT)):
            for fg in ("text", "mut", "good", "warn", "bad", "accent", "entity"):
                web = next(k for k, v in PHONE_NAME.items() if v == fg)
                for bg in SURFACES:
                    r = ratio(t[fg], t[bg])
                    if r < TEXT and (name, web, bg) not in HELD:
                        bad.append(f"{name} {fg} on {bg}: {r:.2f}")
            for bg in SURFACES:
                if (r := ratio(t["field"], t[bg])) < EDGE:
                    bad.append(f"{name} field edge on {bg}: {r:.2f}")
            for fill, ink in (("btn", "text"), ("danger", "onDanger"),
                              ("warn", "bg"), ("bad", "bg"), ("accent", "bg")):
                if (r := ratio(t[ink], t[fill])) < TEXT:
                    bad.append(f"{name} {ink} on {fill}: {r:.2f}")
        self.assertEqual(bad, [])

    def test_the_phone_palette_is_the_webs(self):
        for web, phone in (("dark", PHONE_DARK), ("light", PHONE_LIGHT)):
            t = WEB_DARK if web == "dark" else WEB_LIGHT
            for w, m in PHONE_NAME.items():
                self.assertEqual(resolve(t, t[w]).lower(), phone[m].lower(), f"{web} {w}")

    def test_phone_fields_and_buttons_use_their_tokens(self):
        bad = []
        for p in sources(MOB):
            src = p.read_text()
            for e in elements(p, ("TextInput",), src):
                st = e.attr("style") or ""
                if "C.border" in st:
                    bad.append(f"{e.where()} border")
                for key in re.findall(r"\b\w+\.(\w+)\b", st):
                    m = re.search(r"\n\s*" + key + r":\s*\{([^}]*)\}", src)
                    if m and "borderColor: C.border" in m.group(1):
                        bad.append(f"{e.where()} {key}")
            for e in elements(p, ("Pressable",), src):
                if "backgroundColor: C.bad" in e.attrs:
                    bad.append(f"{e.where()} destructive fill")
                if ("backgroundColor: C.danger" in e.attrs
                        and "color: C.onDanger" not in e.children):
                    bad.append(f"{e.where()} destructive label")
        for name in ("select.tsx", "date-picker.tsx"):
            f = (MOB / "components" / name).read_text()
            box = f[f.index("  field: {"):]
            self.assertIn("borderColor: C.field", box[:box.index("}")], name)
        lock = (MOB / "components" / "lockout.tsx").read_text()
        self.assertNotIn("#04121d", lock)
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
