"""Phone text grows without cutting money short, and small controls reach 44pt.

At a large system text size the Bills calendar cut "+$1,250" to "+$1,2…"
in its day cells and the answer tables cut their figures the same way.
The small segmented control, the date picker's month arrows and day cells,
the alerts bell, the sheets' footer buttons and the calendar cells were
33–42pt targets, under the 44pt platform guideline, and the bell's badge
was a fixed height. Now the cells and tables wrap, and above a text-size
threshold the calendar lists its days in words ("Tue 12: +$1,250 in, $86
out") from the in and out the server sends for each day
(specs/accessibility.md, requirement 12; the server half is in
test_calendar_uses_the_household_day.py).
"""

import re
import unittest

from tests.a11y_source import MOB, elements

CAL = MOB / "components" / "calendar-section.tsx"


class PhoneTextAndTargets(unittest.TestCase):
    def test_the_calendar_lists_its_days_at_very_large_text(self):
        src = CAL.read_text()
        self.assertIn("const asList = fontScale >= CALENDAR_LIST_SCALE;", src)
        self.assertIn("calendarDayLine(d, (v) => money(v, false))", src)
        dates = (MOB / "lib" / "dates.ts").read_text()
        fn = dates[dates.index("export function calendarDayLine("):]
        # it prints the server's figures; the only sum it never does
        self.assertIn("day.came_in", fn)
        self.assertIn("day.went_out", fn)
        self.assertNotRegex(fn, r"events|reduce\(|\+ day\.|- day\.")
        api = (MOB / "lib" / "api.ts").read_text()
        self.assertIn("came_in?: number;", api)

    def test_amounts_wrap_instead_of_being_cut(self):
        src = CAL.read_text()
        cell = src[src.index("{has && !past && c.d && calendarCellFigures("):]
        cell = cell[:cell.index("</Text>")]
        self.assertNotIn("numberOfLines", cell)
        table = (MOB / "components" / "show-numbers.tsx").read_text()
        self.assertIn("numberOfLines={ci > 0 ? undefined : 2}", table)

    def test_small_targets_reach_44pt(self):
        seg = (MOB / "components" / "segmented-control.tsx").read_text()
        self.assertIn("hitSlop={{ top: sm ? 9 : 7, bottom: sm ? 9 : 7 }}", seg)
        dp = (MOB / "components" / "date-picker.tsx").read_text()
        self.assertEqual(dp.count("hitSlop={{ top: 6, bottom: 6 }}"), 2)
        for p in (CAL, MOB / "components" / "date-picker.tsx"):
            self.assertRegex(p.read_text(), r"(gridCell|  cell): \{ flex: 1, minHeight: 44,")
        for p in MOB.rglob("*.tsx"):
            for e in elements(p, ("Pressable",)):
                if re.search(r"style=\{\[?b\.btn\b", e.attrs):
                    self.assertIn("hitSlop={FOOTER_SLOP}", e.attrs, e.where())
        lay = (MOB / "app" / "(tabs)" / "_layout.tsx").read_text()
        bell = lay[lay.index("function AlertsBell("):]
        bell = bell[:bell.index("\n}\n")]
        self.assertIn("hitSlop={10}", bell)
        self.assertIn("minHeight: 16", bell)
        self.assertNotIn("height: 16,", bell.replace("minHeight: 16,", ""))

    def test_font_scaling_is_capped_only_on_the_bell_badge(self):
        found = []
        for p in MOB.rglob("*.tsx"):
            src = p.read_text()
            self.assertNotIn("allowFontScaling={false}", src, p)
            if "maxFontSizeMultiplier" in src:
                found.append(p.name)
        self.assertEqual(found, ["_layout.tsx"])


if __name__ == "__main__":
    unittest.main()
