"""The signed-in header fits 320 CSS px without scrolling sideways.

At 400 % zoom of a 1280 px window every signed-in page scrolled sideways
by about 100 px: the sync status, Feedback, Alerts, Help and the user menu
sat in one row that could neither wrap nor fold. The round icons were also
a fixed 28 px box with a 28 px line height, which clips its glyph under
wider text spacing. Now, at phone width, Feedback and Help fold into the
user menu, the sync status may break over two lines, and the icons size by
min-width and min-height (specs/accessibility.md, requirement 10; measured by
scripts/a11y-audit.py's reflow check).
"""

import re
import unittest

from tests.a11y_source import WEB, elements

CSS = (WEB / "index.css").read_text()
APP_SRC = (WEB / "App.tsx").read_text()


def _phone_block():
    i = CSS.index("@media (max-width:480px){\n  /* seven characters")
    depth, j = 0, CSS.index("{", i)
    while True:
        if CSS[j] == "{":
            depth += 1
        elif CSS[j] == "}":
            depth -= 1
            if depth == 0:
                return CSS[i:j]
        j += 1


class TheHeaderFitsANarrowWindow(unittest.TestCase):
    def test_round_icons_size_by_min_height_not_a_line_height(self):
        icons = [e for e in elements(WEB / "App.tsx", ("NavLink",), APP_SRC)
                 if "hdr-ic" in e.attrs]
        self.assertEqual(len(icons), 3)            # feedback, help, alerts
        for e in icons:
            self.assertIn("hdr-round", e.attrs, e.where())
            for bad in ("height: 28", "lineHeight", "width: 28"):
                self.assertNotIn(bad, e.attrs, e.where())
        rule = re.search(r"\.hdr-round\{([^}]*)\}", CSS).group(1)
        for want in ("min-width:28px", "min-height:28px", "display:inline-flex"):
            self.assertIn(want, rule)
        self.assertNotIn("line-height", rule)
        summary = _phone_block()
        self.assertIn("min-height:28px", summary)
        self.assertNotIn("line-height:28px", summary)

    def test_feedback_and_help_fold_into_the_user_menu(self):
        folded = [e for e in elements(WEB / "App.tsx", ("NavLink",), APP_SRC)
                  if "hdr-fold" in e.attrs]
        self.assertEqual(len(folded), 2)
        menu = APP_SRC[APP_SRC.index('<div className="usermenu-panel">'):]
        menu = menu[:menu.index("</div>")]
        self.assertIn('<NavLink to="/feedback" className="um-fold"', menu)
        self.assertIn('<NavLink to={helpHref(loc.pathname, loc.search)} className="um-fold"', menu)
        self.assertIn(".usermenu-panel .um-fold{display:none}", CSS)
        block = _phone_block()
        for want in (".hdr-fold{display:none}", ".usermenu-panel .um-fold{display:block}",
                     ".topline .rt{flex:0 1 auto}", "white-space:normal"):
            self.assertIn(want, block)


if __name__ == "__main__":
    unittest.main()
