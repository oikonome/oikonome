"""A panel that opens over the page takes focus, keeps it, and gives it back.

The step-up sheet left focus on the page behind its scrim, let Tab walk
out of it and dropped focus on <body> when it closed; it guards every
sensitive action, so a keyboard user lost their place on each one. The
merchant panel opened at the end of the page with focus left where it
was, and Tab escaped the phone-width nav drawer into the page it covered.
All three now use one primitive, `useDialog`. On the phone, the sheet's
tap-to-close backdrop was the first thing a screen reader met, ahead of
the sheet's title. (specs/accessibility.md, requirement 6)
"""

import re
import unittest

from tests.a11y_source import MOB, WEB, elements, sources

HOOK = WEB / "components" / "useDialog.ts"


def _call(src, ref):
    m = re.search(r"useDialog\(" + ref + r",\s*\{(.*?)\}\);", src, re.S)
    return m.group(1) if m else None


class DialogsHoldFocus(unittest.TestCase):
    def test_the_primitive_moves_traps_closes_and_returns(self):
        src = HOOK.read_text()
        for want in ("first.focus()",                      # focus moves in
                     'e.key === "Escape"',                  # Escape closes
                     'if (e.key !== "Tab") return;',        # Tab wraps…
                     "stops[stops.length - 1] : stops[0]).focus()",
                     "sib.inert = true",                    # the rest is inert
                     "el.inert = false",                    # …until it closes
                     "back.focus()",                        # focus returns
                     "stack[stack.length - 1] !== me"):     # top dialog only
            self.assertIn(want, src)
        # the undo runs before focus goes back: an inert opener cannot take it
        cleanup = src[src.index("return () => {\n      document.removeEventListener"):]
        self.assertLess(cleanup.index("undoInert()"), cleanup.index("back.focus()"))

    def test_the_step_up_sheet_is_a_modal_dialog(self):
        src = (WEB / "elevation.tsx").read_text()
        self.assertIn("modal: true", _call(src, "sheet") or "")
        self.assertIn("if (!busy) onCancel();", _call(src, "sheet"))
        dlg = [e for e in elements(WEB / "elevation.tsx", ("div",), src)
               if e.attr("role") == '"dialog"']
        self.assertEqual(len(dlg), 1)
        self.assertIn("ref={sheet}", dlg[0].attrs)
        self.assertIn("tabIndex={-1}", dlg[0].attrs)
        self.assertNotIn('addEventListener("keydown"', src)

    def test_the_merchant_panel_is_a_dialog(self):
        src = (WEB / "pages" / "Merchants.tsx").read_text()
        self.assertIsNotNone(_call(src, "panel"))
        aside = [e for e in elements(WEB / "pages" / "Merchants.tsx", ("aside",), src)]
        self.assertEqual(len(aside), 1)
        for want in ('role="dialog"', "ref={panel}", "tabIndex={-1}"):
            self.assertIn(want, aside[0].attrs)
        fn = src[src.index("function MerchantPanel("):]
        self.assertNotIn('addEventListener("keydown"', fn[:fn.index("\n}\n")])

    def test_the_nav_drawer_is_modal_while_open(self):
        src = (WEB / "App.tsx").read_text()
        call = _call(src, "navRef") or ""
        for want in ("open: drawerOpen", "modal: true", "initialFocus: closeRef",
                     "returnFocus: burgerRef"):
            self.assertIn(want, call)
        self.assertIn('<nav id="mainnav" ref={navRef}>', src)
        # the scrim stays clickable while the page behind is inert
        self.assertRegex(src, r'className="navscrim" aria-hidden="true" data-dialog-keep')

    def test_the_nav_drawer_closes_when_the_window_leaves_phone_width(self):
        # past 760px the drawer is the ordinary rail; left open, the page
        # would stay inert and scroll-locked with nothing visible to close
        src = (WEB / "App.tsx").read_text()
        css = (WEB / "index.css").read_text()
        self.assertIn("@media(max-width:760px)", css)
        m = re.search(r'matchMedia\("\(max-width:760px\)"\)(.*?)\}, \[drawerOpen\]\);',
                      src, re.S)
        self.assertIsNotNone(m, "no width watch on the open drawer")
        self.assertIn("setDrawerOpen(false)", m.group(1))
        self.assertIn('addEventListener("change"', m.group(1))

    def test_every_web_dialog_uses_the_primitive(self):
        bad = []
        for p in sources(WEB):
            src = p.read_text()
            if re.search(r'role="dialog"|role: "dialog"', src) and "useDialog(" not in src:
                bad.append(str(p.relative_to(WEB)))
        self.assertEqual(bad, [])

    def test_the_phone_sheet_reads_before_its_backdrop(self):
        src = (MOB / "components" / "sheet.tsx").read_text()
        body = src[src.index("<KeyboardAvoidingView"):src.index("</KeyboardAvoidingView>")]
        sheet = body.index("<View style={s.sheet}")
        backdrop = body.index("<Pressable style={s.backdrop}")
        self.assertLess(sheet, backdrop)
        tag = body[backdrop:body.index("/>", backdrop)]
        self.assertIn('accessibilityRole="button"', tag)
        self.assertIn('accessibilityLabel="Close"', tag)
        self.assertRegex(src, r"backdrop: \{ position: \"absolute\"")
        self.assertRegex(src, r'maxHeight: "88%", zIndex: 1')


if __name__ == "__main__":
    unittest.main()
