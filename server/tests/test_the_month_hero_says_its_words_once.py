"""The Month hero's words are the server's: both clients chose "over" /
"left of" from a sign, worked out where the pace tick stands, and picked
"over budget" / "within tolerance" from variance against tolerance —
three decisions made twice (specs/money-map.md, "The Month hero's
words")."""
import unittest

from oikonome.web import lenses


def _month(**kw):
    d = {"verdict": "ON BUDGET", "status": "in_progress", "remaining": 200.0,
         "variance": 0.0, "tolerance": 50.0, "variable_actual": 900.0,
         "projection": None}
    d.update(kw)
    return d


class HeroWordsTests(unittest.TestCase):
    def test_the_word_beside_the_figure(self):
        self.assertEqual(lenses.hero_words(_month())["remaining_word"],
                         "left of")
        self.assertEqual(lenses.hero_words(_month(remaining=-5))
                         ["remaining_word"], "over")
        self.assertEqual(lenses.hero_words(_month(status="final"))
                         ["remaining_word"], "left unspent of")

    def test_the_verdict_line_and_its_tone(self):
        # under way, the line is measured against the pace, not the
        # budget: "over budget" beside the hero's own "over" figure would
        # print two different over-budget amounts
        w = lenses.hero_words(_month(variance=320.4))["variance_text"]
        self.assertEqual(w, {"text": "$320 over pace so far", "tone": "neg"})
        w = lenses.hero_words(_month(variance=-90))["variance_text"]
        self.assertEqual(w, {"text": "$90 under pace so far", "tone": "pos"})
        w = lenses.hero_words(_month(variance=-80, status="final"))
        self.assertEqual(w["variance_text"],
                         {"text": "$80 under budget", "tone": "pos"})
        w = lenses.hero_words(_month(variance=10, status="future"))
        self.assertEqual(w["variance_text"],
                         {"text": "within tolerance ($50)", "tone": "warn"})

    def test_the_pace_tick_and_the_projection_tail(self):
        p = {"pace_total": 1400.0, "variance": 120.0, "tolerance": 60.0,
             "verdict": "OVER BUDGET"}
        d = _month(variance=-100.0, projection=p)
        w = lenses.hero_words(d)
        self.assertEqual(w["pace_expected"], 1000.0)
        self.assertEqual((p["tail"], p["tail_tone"]), ("$120 over", "neg"))

    def test_an_unjudged_month_has_no_words(self):
        w = lenses.hero_words(_month(verdict=None))
        self.assertEqual(w, {"remaining_word": None, "pace_expected": None,
                             "variance_text": None})


if __name__ == "__main__":
    unittest.main()
