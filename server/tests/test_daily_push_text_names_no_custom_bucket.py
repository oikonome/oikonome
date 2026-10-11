"""The daily push's text never carries a custom bucket's name.

That one line leaves the instance for the publisher's push relay and the
phone platforms, and the household is told it holds only the verdict, the
top driver and the allowance — never a merchant or a name. A custom
bucket's name is typed by the household and is often exactly that (a
store, a person's daycare). If the push reused the SMS's per-tile
allowance line, every such name would go out every morning. Instead each
carve-out's allowance is folded into the Food or Everything-else figure it
was carved from, so the push still shows the whole allowance, under the
two fixed labels only. The SMS keeps the per-tile line."""
import datetime as dt
import unittest

from oikonome.notify import render
from oikonome.web import todayview


def _day():
    return {"verdict": "ON BUDGET", "variance": 0.0, "reasons": [],
            "today": dt.date(2026, 10, 10), "days_in_month": 31,
            "buckets": {
                "food": {"month_budget": 900.0, "actual": 300.0,
                         "remaining_budget": 650.0, "display_actual": 200.0,
                         "rows": [],
                         "children": [{"name": "Corner Bakery Fixture",
                                       "month_budget": 250.0, "actual": 100.0,
                                       "rows": []}]},
                "other": {"month_budget": 600.0, "actual": 160.0,
                          "remaining_budget": 400.0, "display_actual": 60.0,
                          "rows": [],
                          "children": [{"name": "Sam Daycare Fixture",
                                        "month_budget": 200.0,
                                        "actual": 100.0, "rows": []}]}}}


class DailyPushNamesNoCustomBucket(unittest.TestCase):

    def test_push_leaves_custom_bucket_names_out(self):
        p = render.push_daily(_day())
        self.assertNotIn("Corner Bakery", p)
        self.assertNotIn("Sam Daycare", p)
        self.assertIn("/day Food", p)
        self.assertIn("/day Everything else", p)

    def test_push_folds_each_carve_out_into_its_parent(self):
        d = _day()
        allow, kids, _tiles = todayview.allowances(d)
        food = allow["food"]["rate"] + kids[0]["rate"]
        other = allow["other"]["rate"] + kids[1]["rate"]
        p = render.push_daily(d)
        self.assertIn(f"${food:,.0f}/day Food", p)
        self.assertIn(f"${other:,.0f}/day Everything else", p)

    def test_an_overspent_carve_out_folds_as_the_today_tiles_add_up(self):
        """The folded figure is what the Today tiles it stands for add up
        to, each floored at zero: an overspent carve-out adds nothing and
        takes nothing from its parent's room, the rule the Today headline
        uses for every cross-tile total. The push must never quote a figure
        the page the notification opens does not."""
        d = _day()
        d["today"] = dt.date(2026, 10, 21)       # 11 days left
        food = d["buckets"]["food"]
        # Food $600 with a $50 carve-out: the parent keeps $550 and has
        # spent $330; the carve-out has spent $160 of its $50
        food.update(month_budget=600.0, actual=490.0,
                    remaining_budget=550.0, display_actual=330.0)
        food["children"][0].update(month_budget=50.0, actual=160.0)
        allow, kids, tiles = todayview.allowances(d)
        self.assertEqual(kids[0]["rate"], 0.0)
        self.assertAlmostEqual(allow["food"]["rate"], 20.0)
        food_tiles = [a for label, a in tiles
                      if label in ("Food", "Corner Bakery Fixture")]
        folded = sum(max(0.0, a["rate"]) for a in food_tiles)
        p = render.push_daily(d)
        self.assertIn(f"${folded:,.0f}/day Food", p)
        self.assertIn("$20/day Food", p)
        self.assertNotIn("$10/day Food", p)
        # and the per-tile SMS names the same two numbers it was folded from
        t = render.short_daily(d)
        self.assertIn("$20/day Food", t)
        self.assertIn("$0/day Corner Bakery Fixture", t)

    def test_sms_still_names_each_tile(self):
        t = render.short_daily(_day())
        self.assertIn("/day Corner Bakery Fixture", t)
        self.assertIn("/day Sam Daycare Fixture", t)


if __name__ == "__main__":
    unittest.main()
