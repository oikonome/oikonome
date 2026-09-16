"""The simple face's day bar tells the same story as the headline number
and the chips beside it: one bucket's overspend is never absorbed by a
neighbour's unused allowance (bar remaining == the headline's floored
sum), and the bar's over state trips only when the day as a whole is
genuinely over — every allowance exhausted with real overshoot."""

import unittest

from oikonome.web.todayview import simple_day_meter


def _tile(allow, spent):
    return {"today_allowance": round(allow, 2),
            "spent_today": round(spent, 2),
            "left_today": round(allow - spent, 2)}


class SimpleDayMeterTests(unittest.TestCase):
    def test_overspend_not_absorbed_by_neighbour_slack(self):
        # Food $19.50 over its 50-cent allowance; everything-else untouched.
        # The headline floors Food at $0 and reads $30. Raw sums drew a
        # two-thirds-full bar (20 of 30.5) implying ~$10.50 left on the same
        # screen — the bar's implied remainder must equal the headline.
        tiles = [("Food", _tile(0.50, 20.00)),
                 ("Everything else", _tile(30.00, 0.00))]
        spent, allow = simple_day_meter(tiles)
        self.assertAlmostEqual(allow, 30.50, places=2)
        headline = sum(max(0.0, a["left_today"]) for _, a in tiles)
        self.assertAlmostEqual(allow - spent, headline, places=2)
        # money remains, so the bar must not read over (renderers test
        # spent > allow + 0.5) — the red story belongs to Food's chip
        self.assertLessEqual(spent, allow + 0.5)

    def test_over_state_trips_when_every_bucket_is_exhausted(self):
        tiles = [("Food", _tile(10.00, 25.00)),
                 ("Everything else", _tile(20.00, 20.00))]
        spent, allow = simple_day_meter(tiles)
        self.assertAlmostEqual(allow, 30.00, places=2)
        # nothing left anywhere: the true overshoot shows and the bar
        # goes over, agreeing with the chips
        self.assertAlmostEqual(spent, 45.00, places=2)
        self.assertGreater(spent, allow + 0.5)

    def test_exactly_spent_day_is_full_but_not_over(self):
        tiles = [("Food", _tile(10.00, 10.00)),
                 ("Everything else", _tile(20.00, 20.00))]
        spent, allow = simple_day_meter(tiles)
        self.assertAlmostEqual(spent, allow, places=2)
        self.assertLessEqual(spent, allow + 0.5)

    def test_untouched_day_reads_empty(self):
        tiles = [("Food", _tile(12.00, 0.00)),
                 ("Everything else", _tile(30.00, 0.00))]
        spent, allow = simple_day_meter(tiles)
        self.assertAlmostEqual(spent, 0.0, places=2)
        self.assertAlmostEqual(allow, 42.00, places=2)


if __name__ == "__main__":
    unittest.main()
