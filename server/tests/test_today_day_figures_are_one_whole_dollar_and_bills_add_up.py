"""A day's allowance prints as one whole-dollar figure — floored once on
the server — in the tile, the day line, the daily note, the simple face and
the SMS, so "$41/day" is never "$42" a line away; and Today's Bills row
reads posted / what the month's bills come to — still due, a sum that
subtracts (specs/today-figures.md). Figures are invented."""

import datetime as dt
import unittest

from oikonome.engine import budget
from oikonome.notify import render
from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config


class OneRoundingTests(unittest.TestCase):
    def status(self):
        # $900 left over the 22 days left of October: $40.91 a day
        return {"verdict": "ON BUDGET", "variance": 0.0, "reasons": [],
                "today": dt.date(2026, 10, 10), "days_in_month": 31,
                "buckets": {
                    "food": {"month_budget": 1200.0, "actual": 300.0,
                             "rows": []},
                    "other": {"month_budget": 400.0, "actual": 100.0,
                              "rows": []}}}

    def test_the_tile_the_day_line_and_the_sms_say_the_same_dollars(self):
        d = self.status()
        allow, _k, _t = todayview.allowances(d)
        food = allow["food"]
        self.assertAlmostEqual(food["rate"], 900 / 22, places=4)
        self.assertEqual((food["rate_dollars"], food["left_dollars"],
                          food["today_allowance_dollars"]), (40, 40, 40))
        self.assertEqual(food["day_line"], "of $40 today · nothing spent yet")
        text = render.short_daily(d)
        self.assertIn("$40/day Food", text)
        self.assertNotIn("$41", text)

    def test_shown_daily_floors_and_never_goes_below_zero(self):
        self.assertEqual(budget.shown_daily(41.999), 41)
        self.assertEqual(budget.shown_daily(42.0), 42)
        self.assertEqual(budget.shown_daily(-3.5), 0)


class BillsRowTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=900)
        add_bill(self.conn, "EXAMPLE RENT CO", 700.0,
                 next_due=dt.date(2026, 7, 3), last_seen=dt.date(2026, 6, 3))
        # posted $6.50 under its plan: paid, nothing of it still due
        add_txn(self.conn, dt.date(2026, 7, 3), 693.5, "EXAMPLE RENT CO",
                primary="RENT_AND_UTILITIES", account="chk")
        add_bill(self.conn, "EXAMPLE POWER CO", 300.0,
                 next_due=dt.date(2026, 7, 25), last_seen=dt.date(2026, 6, 25))
        self.st = report.gather(self.conn, TODAY)

    def tearDown(self):
        self.conn.close()

    def test_posted_and_still_due_make_the_rows_total(self):
        rows, _t = todayview.today_map(self.st)
        row = next(r for r in rows if r["label"] == "Bills")
        due = self.st["bills_remaining_month"]
        self.assertAlmostEqual(row["budget"], row["actual"] + due, places=2)
        self.assertEqual(row["status"]["amount"], due)
        # the plan it differs from is named beside it
        self.assertIn("plan $1,000", row["caption"])

    def test_the_caption_names_posted_and_the_plan(self):
        # three bill figures on one card read as a contradiction unless
        # the words beside them add up: the caption names posted and the
        # plan, the words the parts between them
        rows, _t = todayview.today_map(self.st)
        row = next(r for r in rows if r["label"] == "Bills")
        from oikonome.web import charts
        self.assertTrue(row["caption"].startswith(
            f"posted {charts._money(row['actual'])} · plan $1,000"),
            row["caption"])

    def test_the_figures_beside_the_row_add_up_to_its_total(self):
        # 100.40 posted + 200.40 still due is $301 beside the row; said
        # as $100 + $200 the sum was a dollar short of it
        import copy
        import re
        from oikonome.web import bills_breakdown
        st = copy.deepcopy(self.st)
        st["bills_breakdown"] = bills_breakdown.from_parts(
            "running", plan=1000.0, posted=100.40, due=200.40)
        rows, _t = todayview.today_map(st)
        row = next(r for r in rows if r["label"] == "Bills")
        self.assertEqual(row["budget"], 301.0)
        due = int(re.match(r"\$([\d,]+) still due",
                           row["status"]["text"]).group(1))
        self.assertEqual(row["actual"] + due, row["budget"])


if __name__ == "__main__":
    unittest.main()
