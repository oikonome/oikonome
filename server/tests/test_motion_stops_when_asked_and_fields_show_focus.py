"""Motion stops when the person asks for less, and a focused field shows it.

With the system's reduce-motion setting on, the receipt-parse bar, the
assistant's typing dots, Bills' scroll to a row, the setup wizard's
counting numbers and the restart screen's spinner stand still, and on the
phone the history import's sweep and the sheets do too. A focused field
keeps an outline of at least 3:1, so focus is never carried by a colour
change alone. The assistant's thread rows are buttons that answer Space
as well as Enter (specs/accessibility.md, "Reduced motion and field
focus").
"""

import re
import unittest

from tests.a11y_source import MOB, WEB

CSS = (WEB / "index.css").read_text()


def _reduce_blocks():
    out, i = [], 0
    while (i := CSS.find("@media (prefers-reduced-motion:reduce){", i)) >= 0:
        j, depth = CSS.index("{", i), 0
        while True:
            depth += {"{": 1, "}": -1}.get(CSS[j], 0)
            if depth == 0:
                break
            j += 1
        out.append(CSS[i:j + 1])
        i = j
    return "\n".join(out)


class MotionAndFocus(unittest.TestCase):
    def test_every_endless_css_animation_has_a_reduced_motion_stop(self):
        reduce = _reduce_blocks()
        for sel in (".rcpt-progress span", ".chat-typing i", ".syncdot.busy",
                    ".syncfill.indet", ".bf-fill.live::after", ".sk"):
            self.assertRegex(reduce, re.escape(sel) + r"\{[^}]*animation:none", sel)

    def test_script_motion_asks_first(self):
        motion = (WEB / "motion.ts").read_text()
        self.assertIn('"(prefers-reduced-motion: reduce)"', motion)
        self.assertIn('reducedMotion() ? "auto" : "smooth"', motion)
        bills = (WEB / "pages" / "Bills.tsx").read_text()
        self.assertNotIn('behavior: "smooth"', bills)
        self.assertEqual(bills.count("behavior: scrollBehavior()"), 2)
        for p in WEB.rglob("*.tsx"):
            self.assertNotIn('behavior: "smooth"', p.read_text(), p)
        self.assertIn("if (n <= from || reducedMotion())",
                      (WEB / "pages" / "Welcome.tsx").read_text())
        self.assertIn('reducedMotion() ? "none" : "spin', (WEB / "App.tsx").read_text())

    def test_a_focused_field_draws_an_outline_beside_its_edge(self):
        for sel in ("input:focus,select:focus", "textarea:focus",
                    ".chat-compose .box:focus-within"):
            m = re.search(re.escape(sel) + r"\{([^}]*)\}", CSS)
            self.assertIsNotNone(m, sel)
            self.assertIn("border-color:var(--blue)", m.group(1), sel)
            self.assertIn("outline:2px solid var(--blue)", m.group(1), sel)
            self.assertNotIn("outline:none", m.group(1), sel)

    def test_assistant_rows_answer_enter_and_space(self):
        src = (WEB / "pages" / "Assistant.tsx").read_text()
        row = src[src.index('role="button" tabIndex={0}'):]
        row = row[:row.index("</div>")]
        self.assertIn('e.key !== "Enter" && e.key !== " "', row)
        self.assertIn("e.preventDefault()", row)

    def test_phone_sheets_and_the_import_sweep_follow_reduce_motion(self):
        a11y = (MOB / "lib" / "a11y.ts").read_text()
        self.assertIn("AccessibilityInfo.isReduceMotionEnabled()", a11y)
        self.assertIn('"reduceMotionChanged"', a11y)
        n = 0
        for p in MOB.rglob("*.tsx"):
            src = p.read_text()
            self.assertNotIn('animationType="slide"', src, p)
            n += src.count("animationType={sheetAnim}")
        self.assertGreaterEqual(n, 11)
        welcome = (MOB / "app" / "welcome.tsx").read_text()
        bar = welcome[welcome.index("function BackfillBar("):]
        bar = bar[:bar.index("\n}\n")]
        self.assertIn("const still = useReduceMotion();", bar)
        self.assertIn("if (!live || still) return;", bar)
        self.assertIn("{live && !still && (", bar)


if __name__ == "__main__":
    unittest.main()
