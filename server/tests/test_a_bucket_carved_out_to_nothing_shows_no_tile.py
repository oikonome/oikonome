"""A parent bucket whose whole budget went to its carve-outs shows no tile.

"Everything else" is what the carve-outs leave. When they leave nothing
and nothing has been spent in it, its tile reads "$0 of $0" beside the
tiles that actually hold the money. The tile is left off the page and
the email; the totals do not move, because an empty tile carries no
allowance and no spend. The moment a charge lands in the bucket the tile
is back, reading over."""
import datetime as dt
import unittest

from oikonome.web import todayview


def _day(other_left=0.0, other_spent=0.0):
    return {"today": dt.date(2026, 10, 10), "days_in_month": 31,
            "buckets": {
                "food": {"month_budget": 900.0, "actual": 300.0,
                         "remaining_budget": 900.0, "display_actual": 300.0,
                         "rows": [], "children": []},
                "other": {"month_budget": 600.0, "actual": 100.0 + other_spent,
                          "remaining_budget": other_left,
                          "display_actual": other_spent, "rows": [],
                          "children": [{"name": "Household Fixture",
                                        "month_budget": 600.0 - other_left,
                                        "actual": 100.0, "rows": []}]}}}


class ABucketCarvedOutToNothing(unittest.TestCase):

    def test_the_empty_parent_is_left_off_and_the_totals_hold(self):
        allow, _kids, tiles = todayview.allowances(_day())
        self.assertTrue(allow["other"]["empty"])
        shown = todayview.shown_tiles(tiles)
        self.assertEqual([label for label, _ in shown],
                         ["Food", "Household Fixture"])
        self.assertEqual(todayview.simple_day_meter(shown),
                         todayview.simple_day_meter(tiles))

    def test_the_short_text_names_no_rate_for_the_empty_parent(self):
        from oikonome.notify import render
        text = render.short_daily(_day())
        self.assertIn("/day Household Fixture", text)
        self.assertNotIn("Everything else", text)
        # with room left the parent has a tile, and a rate to name
        self.assertIn("/day Everything else",
                      render.short_daily(_day(other_left=150.0)))

    def test_a_parent_with_room_left_keeps_its_tile(self):
        allow, _kids, tiles = todayview.allowances(_day(other_left=150.0))
        self.assertFalse(allow["other"]["empty"])
        self.assertEqual(len(todayview.shown_tiles(tiles)), 3)

    def test_spending_in_an_emptied_parent_brings_the_tile_back(self):
        allow, _kids, tiles = todayview.allowances(_day(other_spent=40.0))
        self.assertFalse(allow["other"]["empty"])
        self.assertIn("Everything else",
                      [label for label, _ in todayview.shown_tiles(tiles)])

    def test_a_refund_in_an_emptied_parent_keeps_the_tile_and_its_room(self):
        """A return landing outside the carve-outs gives the bucket room
        again. Hiding its tile would drop that room from the headline."""
        allow, _kids, tiles = todayview.allowances(_day(other_spent=-60.0))
        self.assertFalse(allow["other"]["empty"])
        self.assertGreater(allow["other"]["today_allowance"], 0)
        shown = todayview.shown_tiles(tiles)
        self.assertEqual(todayview.simple_day_meter(shown),
                         todayview.simple_day_meter(tiles))

    def test_a_bucket_with_no_carve_outs_is_never_hidden(self):
        d = _day()
        d["buckets"]["other"].update(children=[], month_budget=0.0, actual=0.0)
        allow, _kids, tiles = todayview.allowances(d)
        self.assertFalse(allow["other"].get("empty"))
        self.assertEqual(len(todayview.shown_tiles(tiles)), 2)


if __name__ == "__main__":
    unittest.main()
