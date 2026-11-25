"""The savings pace window is called what it is: the last 3 months.

A goal's pace window opens on the same day three calendar months back
(savings.pace_since), so it holds 89 to 92 days. Calling it "the last 90
days" was wrong on most days of the year. Every surface that names the
window (Today on the web and the phone, the daily email, the report's
plain text, the lens Savings rows) says "the last 3 months", and none
still says the day count.
"""

import datetime as dt
import pathlib
import unittest

import oikonome
from oikonome.engine import savings
from oikonome.web import todayview

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent

SURFACES = ("server/oikonome/web/todayview.py",
            "server/oikonome/web/lenses.py",
            "server/oikonome/web/report.py",
            "server/oikonome/web/templates/today_body.html",
            "webapp/src/pages/Today.tsx",
            "mobile/src/app/(tabs)/index.tsx")
OLD = ("last 90 days", "in 90 days")


class ThePaceWindowIsThreeMonths(unittest.TestCase):
    def test_the_window_spans_three_calendar_months_not_ninety_days(self):
        spans = {(d - savings.pace_since(d)).days + 1
                 for d in (dt.date(2026, 3, 1), dt.date(2026, 5, 31),
                           dt.date(2026, 8, 15), dt.date(2027, 1, 31))}
        self.assertNotEqual(spans, {90})

    def test_no_surface_names_the_window_by_a_day_count(self):
        for rel in SURFACES:
            src = (_APP / rel).read_text()
            for old in OLD:
                self.assertFalse(old in src, f"{rel} says {old!r}")

    def test_the_today_savings_row_says_three_months_on_its_average(self):
        st = {"savings_goals": [{"name": "Rainy day", "monthly_plan": 150.0,
                                 "rate_90d": 120.0, "saved_this_month": None}],
              "plan_goals": [{"name": "Rainy day"}], "savings_plan": 150.0}
        row = todayview._savings_row(st)[0]
        self.assertEqual(row["caption"], "avg / mo, last 3 months")


if __name__ == "__main__":
    unittest.main()
