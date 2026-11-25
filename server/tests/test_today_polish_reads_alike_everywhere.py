"""Small things Today said differently, or cut short, on one surface:

- a non-monthly bill already paid is listed at what it cost, "paid", not
  at its plan as if still owed;
- the line under each day's figure ("of $40 today · nothing spent yet")
  is composed once and shown at every width: the web hid it on a phone,
  where the app showed only the line after it;
- the phone's alert line keeps its amount (two lines, not one cut short);
- the cash chart's captions do not print over each other on a phone.

Figures are invented."""

import datetime as dt
import pathlib
import unittest

from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config

ROOT = pathlib.Path(__file__).resolve().parents[2]


class PaidIrregularBillTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        add_bill(self.conn, "NORTHWIND INSURANCE", 240.0, frequency="YEARLY",
                 next_due=dt.date(2026, 7, 6),
                 last_seen=dt.date(2025, 7, 6))
        add_txn(self.conn, dt.date(2026, 7, 6), 230.0, "NORTHWIND INSURANCE",
                account="chk")
        add_bill(self.conn, "HILL WATER DISTRICT", 90.0, frequency="MONTHLY",
                 interval=3, next_due=dt.date(2026, 7, 27),
                 last_seen=dt.date(2026, 4, 27))
        self.st = report.gather(self.conn, TODAY)

    def tearDown(self):
        self.conn.close()

    def test_a_paid_one_shows_what_it_cost(self):
        got = {i["payee"]: (i["amount"], i["label"], i["paid"])
               for i in self.st["irregular_bills"]}
        self.assertEqual(got["NORTHWIND INSURANCE"], (230.0, "annual, paid", True))
        self.assertEqual(got["HILL WATER DISTRICT"],
                         (90.0, "every 3 months", False))
        _s, plain, html = report.build(self.st)
        self.assertIn("NORTHWIND INSURANCE $230 (annual, paid)", plain)
        self.assertIn("(annual, paid)", html)


class DayLineTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=620, other_monthly=930,
                     today_view="detail")

    def tearDown(self):
        self.conn.close()

    def lines(self):
        st = report.gather(self.conn, TODAY)
        allow, _kids, _tiles = todayview.allowances(st)
        return st, allow

    def test_one_line_under_each_figure(self):
        add_txn(self.conn, TODAY, 12.0, "CORNER CAFE",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 400.0, "BIRCHWOOD FURNITURE")
        st, allow = self.lines()
        food, other = allow["food"], allow["other"]
        m = todayview.charts._money
        self.assertEqual(food["day_line"],
                         f"of {m(food['today_allowance'])} today · "
                         f"spent {m(12.0)}")
        self.assertTrue(other["day_line"].startswith("over today by "),
                        other["day_line"])
        _s, _plain, html = report.build(st)
        for a in (food, other):
            self.assertIn(a["day_line"], html)

    def test_a_day_with_no_daily_left_does_not_say_of_zero(self):
        """Once the month's budget is spent there is no daily: "spent $50
        of $0" read as a figure out of nothing."""
        add_txn(self.conn, TODAY - dt.timedelta(days=3), 1000.0,
                "BIRCHWOOD FURNITURE")
        add_txn(self.conn, TODAY, 50.0, "BIRCHWOOD FURNITURE")
        _st, allow = self.lines()
        other = allow["other"]
        self.assertEqual(other["today_allowance_dollars"], 0)
        self.assertNotIn("of $0", other["day_line"])
        self.assertEqual(other["day_line"],
                         "over today by $50 · spent $50, no daily left")

    def test_nothing_spent_yet(self):
        _st, allow = self.lines()
        self.assertTrue(allow["food"]["day_line"].endswith(
            " today · nothing spent yet"))


class ClientsShowTheSameLinesTests(unittest.TestCase):
    def read(self, rel):
        return (ROOT / rel).read_text()

    def test_the_web_shows_the_line_at_phone_width(self):
        css = self.read("webapp/src/index.css")
        self.assertNotIn(".drow>.drow-s{display:none}", css)
        today = self.read("webapp/src/pages/Today.tsx")
        self.assertIn("a!.day_line", today)
        self.assertNotIn("nothing\n                          spent yet", today)

    def test_an_over_row_prints_one_figure_at_every_width(self):
        """The web printed "$0" for an over-budget day at desktop width and
        "over by $N" at phone width, as the app does; both now say over."""
        today = self.read("webapp/src/pages/Today.tsx")
        row = today[today.index("htile-big drow-n"):][:400]
        self.assertNotIn("hide-m", row)
        self.assertIn("over by {money(-a!.left_today)}", row)

    def test_the_app_shows_the_line_and_the_whole_alert(self):
        app = self.read("mobile/src/app/(tabs)/index.tsx")
        self.assertIn("a.day_line", app)
        alert = app[app.index("things need you"):][:600]
        self.assertIn("numberOfLines={2}", alert)
        self.assertIn("-webkit-line-clamp:2", self.read("webapp/src/index.css"))

    def test_chart_captions_make_room(self):
        chart = self.read("webapp/src/components/AreaChart.tsx")
        self.assertIn("labelledEvents.has(k)", chart)
        self.assertIn("legendRows * 14", chart)


if __name__ == "__main__":
    unittest.main()
