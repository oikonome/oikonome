"""Every chart says its figures in words and opens them as a table.

The web's area charts and the Cash Flow pictures had no text a screen
reader could reach (the flow picture and the saved-per-month bars were an
image named "saved per month" that hid the figures they drew), and each
month's value lived only in a tooltip the mouse alone could move. On the
phone the flow picture's node figures, every saved-per-month bar, the
trend lines and a ring's centre total existed only as SVG. Now each chart
speaks one sentence — where it starts and ends, its highest and lowest
point — with a "Show numbers" table under it; the web tooltip walks by
arrow key. Every figure said is one the chart already prints: picks from
the points the server sent, or its `shown` figures, never a sum or an
average worked out here (specs/accessibility.md, requirement 9;
specs/shown-figures.md).
"""

import json
import re
import shutil
import subprocess
import unittest

from tests.a11y_source import APP, MOB, WEB, elements

CASES = APP / "mobile" / "scripts" / "chart-summary-cases.mjs"

RUNNER = """
import assert from "node:assert/strict";
import { chartSummaryCases } from %(cases)s;
const impl = await import(%(impl)s);
const failed = [];
for (const [name, fn] of Object.entries(chartSummaryCases(impl, assert))) {
  try { await fn(); } catch (e) { failed.push(name + ": " + e.message); }
}
if (failed.length) { console.error(failed.join("\\n")); process.exit(1); }
"""


def _read(p):
    return p.read_text()


class ChartsSayTheirFigures(unittest.TestCase):
    def _run(self, impl):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        self.assertTrue(impl.exists(), impl)
        r = subprocess.run(
            [node, "--experimental-strip-types", "--input-type=module", "-e",
             RUNNER % {"cases": json.dumps(CASES.as_uri()),
                       "impl": json.dumps(impl.as_uri())}],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0 and "bad option" in (r.stderr or ""):
            self.skipTest("node too old for --experimental-strip-types")
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_the_web_words_pass_the_shared_cases(self):
        self._run(WEB / "chartsummary.ts")

    def test_the_phone_words_pass_the_shared_cases(self):
        self._run(MOB / "lib" / "chart-summary.ts")

    def test_the_two_copies_are_one_text(self):
        def body(p):
            return _read(p).split("\n", 8)[8]     # past the header naming its twin
        self.assertEqual(body(WEB / "chartsummary.ts"), body(MOB / "lib" / "chart-summary.ts"))

    def test_the_words_do_no_arithmetic(self):
        """Picks and printing only: no sum, mean, difference or ratio of
        figures (the only `+`/`-` are on indexes and strings)."""
        src = re.sub(r"//.*|/\*.*?\*/", "", _read(WEB / "chartsummary.ts"), flags=re.S)
        for bad in ("reduce(", "Math.round", "Math.abs", "/ points", "toFixed",
                    "p.v +", "p.v -", "v / ", "v * "):
            self.assertNotIn(bad, src, bad)

    # ---- the web ------------------------------------------------------

    def test_web_area_charts_speak_and_walk_by_keyboard(self):
        src = _read(WEB / "components" / "AreaChart.tsx")
        svg = next(elements(WEB / "components" / "AreaChart.tsx", ("svg",), src))
        for want in ('role="img"', "tabIndex={0}", "aria-label={chartSentence(",
                     "onKeyDown={onKey}", "onBlur="):
            self.assertIn(want, svg.attrs, want)
        self.assertIn('aria-live="polite"', src)
        self.assertIn("<ShowNumbers table={() => chartTable(named,", src)
        keys = _read(WEB / "chartsummary.ts")
        for k in ("ArrowLeft", "ArrowRight", "Home", "End"):
            self.assertIn(f'"{k}"', keys)

    def test_every_web_area_chart_names_what_it_draws(self):
        n = 0
        for page in ("Today", "NetWorth", "CashFlow", "Retirement", "Spending", "Debt"):
            p = WEB / "pages" / f"{page}.tsx"
            for e in elements(p, ("AreaChart",)):
                n += 1
                self.assertIn("subject=", e.attrs, e.where())
        self.assertGreaterEqual(n, 7)

    def test_the_cash_flow_pictures_say_every_figure(self):
        p = WEB / "components" / "CashFlowCharts.tsx"
        src = _read(p)
        labels = [e.attr("aria-label") for e in elements(p, ("svg",), src)]
        self.assertEqual(labels, ["{partsSentence(sides)}",
                                  "{chartSentence(subject, named, (v) => money(v), ef)}"])
        self.assertEqual(src.count("<ShowNumbers"), 2)
        # the flow picture's words are its printed figures, the server's
        self.assertIn("label: n.label, figure: dollars(n), share: shownOf.get(n.key)?.share",
                      src)

    def test_the_web_rings_hide_the_drawing_behind_the_legend(self):
        for p in (WEB / "components" / "Donut.tsx", WEB / "components" / "AssistantBlocks.tsx"):
            src = _read(p)
            rings = [e for e in elements(p, ("svg",), src) if "size" in e.attrs]
            self.assertTrue(rings, p)
            for e in rings:
                self.assertIn('aria-hidden="true"', e.attrs, e.where())
            self.assertIn('<span className="sr-only">', src, p)

    def test_the_assistant_charts_speak_and_walk(self):
        src = _read(WEB / "components" / "AssistantBlocks.tsx")
        self.assertEqual(src.count("{...words.svg}"), 2)
        self.assertIn("chartSentence(", src)
        self.assertIn("aria-expanded={open}", src)
        self.assertIn('"Show numbers"', src)

    def test_the_unused_line_chart_is_gone(self):
        self.assertFalse((WEB / "components" / "LineChart.tsx").exists())

    # ---- the phone ----------------------------------------------------

    def test_phone_charts_speak_and_fold_out_their_numbers(self):
        for name, said in (("sparkline.tsx", "chartSentence(subject, named, fmt, dashFromIn)"),
                           ("net-bars.tsx", "chartSentence(subject, named,"),
                           ("flow-chart.tsx", "partsSentence(sides)")):
            p = MOB / "components" / name
            src = _read(p)
            views = [e for e in elements(p, ("View",), src)
                     if 'accessibilityRole="image"' in e.attrs]
            self.assertEqual(len(views), 1, name)
            self.assertIn("accessible", views[0].attrs)
            self.assertIn(said, views[0].attrs, name)
            self.assertIn("<ShowNumbers", src, name)
        flow = _read(MOB / "components" / "flow-chart.tsx")
        self.assertIn("const x = flowNodeFigure(flow, n);", flow)

    def test_every_phone_line_names_what_it_draws(self):
        n = 0
        for p in MOB.rglob("*.tsx"):
            for e in elements(p, ("Sparkline",)):
                n += 1
                self.assertIn("subject=", e.attrs, e.where())
        self.assertGreaterEqual(n, 7)

    def test_phone_answer_charts_and_ring(self):
        p = MOB / "components" / "answer-blocks.tsx"
        src = _read(p)
        self.assertEqual(src.count('accessibilityRole="image"'), 3)
        self.assertIn("accessibilityLabel={said}", src)
        self.assertIn("accessibilityLabel={shown ? `Total ${money(shown.total)}`", src)
        self.assertIn("<ShowNumbers table={() => chartRows(labels, series)} />", src)
        sn = _read(MOB / "components" / "show-numbers.tsx")
        self.assertIn('"Show numbers"', sn)
        self.assertIn("accessibilityState={{ expanded: open }}", sn)

    def test_the_phone_meter_is_a_progress_bar(self):
        src = _read(MOB / "components" / "ui.tsx")
        meter = src[src.index("export function Meter("):src.index("export function KV(")]
        self.assertIn('accessibilityRole="progressbar"', meter)
        self.assertIn("accessibilityValue={{ min: 0, max: 100,", meter)


if __name__ == "__main__":
    unittest.main()
