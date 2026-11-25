"""The help guides say what the pages print, and the ? opens the right one.

The money map guide said a Today row reads "$X more than planned" while
Today prints "$X over pace"; the Today guide said every money figure is
positive beside a Biggest transactions list printed with minus signs; the
Alerts guide listed two anomaly signals when there are three and named no
merchant-pair alert; and the ? on the Month and Year views opened the
Today guide.
"""
import pathlib
import shutil
import subprocess
import unittest

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_GUIDES = _ROOT / "docs" / "guides"
_HELP = _ROOT / "webapp" / "src" / "helptopic.ts"


def _guide(name):
    return " ".join((_GUIDES / f"{name}.md").read_text().split())


class GuideWordsTests(unittest.TestCase):
    def test_the_money_map_guide_says_today_reads_against_its_pace(self):
        g = _guide("money-map")
        self.assertIn("On Today a spending row reads against today's pace: "
                      "`$X over pace`", g)

    def test_no_guide_says_every_figure_is_positive(self):
        for name in ("today", "lenses"):
            self.assertNotIn("Everything money-related shows spending as "
                             "positive", _guide(name))
            self.assertNotIn("As everywhere, spending shows as positive",
                             _guide(name))
            self.assertIn("minus sign", _guide(name))

    def test_the_alerts_guide_lists_every_anomaly_and_the_merchant_pairs(self):
        g = _guide("alerts")
        self.assertIn("Three signals", g)
        self.assertIn("Fee charged", g)
        self.assertIn("Interest charged", g)
        self.assertIn("new merchant pairs look like one merchant", g)
        self.assertIn("new merchant pairs look like one merchant",
                      _guide("merchants"))


class HelpTopicTests(unittest.TestCase):
    def _href(self, path, search=""):
        node = shutil.which("node")
        if not node or not _HELP.exists():
            self.skipTest("node or webapp/ not present")
        r = subprocess.run(
            [node, "--experimental-strip-types", "--input-type=module", "-e",
             f"const m = await import({_HELP.as_uri()!r});"
             f"console.log(m.helpHref({path!r}, {search!r}));"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip().splitlines()[-1]

    def test_the_month_and_year_views_open_the_lenses_guide(self):
        self.assertEqual(self._href("/", "?lens=month"), "/help?topic=lenses")
        self.assertEqual(self._href("/", "?lens=year&y=2026"),
                         "/help?topic=lenses")

    def test_today_still_opens_the_today_guide(self):
        self.assertEqual(self._href("/"), "/help?topic=today")
        self.assertEqual(self._href("/", "?lens=today"), "/help?topic=today")
        self.assertEqual(self._href("/bills"), "/help?topic=bills")


if __name__ == "__main__":
    unittest.main()
