"""A reduced daily answers "how long do I stop spending to get it back".

Every spend-at-most tile whose daily has been reduced carries the number
of no-spend days that bring it back to the plan, computed once in the
allowance entry and rendered verbatim on the page context, the email
HTML and the plain text — the same number on every surface.
"""

import datetime as dt
import html as _html
import unittest

from oikonome.web import report, todayview

from .util import (TODAY, add_bill, add_ledger_history, add_txn, make_db,
                   write_config)


class RecoverDaysArithmeticTests(unittest.TestCase):
    """budget $310 over 31 days = a $10 plan; 10 days left."""

    def test_exact_division_does_not_cost_an_extra_day(self):
        # $50 left over 10 days: five no-spend days leave 50/5 = $10 — the
        # plan exactly, and float noise must not round that up to six
        self.assertEqual(todayview.recover_days(310, 260, 10, 31), 5)

    def test_nothing_to_recover_is_zero(self):
        # $100 left over 10 days is the plan already
        self.assertEqual(todayview.recover_days(310, 210, 10, 31), 0)
        # and being ahead is still nothing to recover
        self.assertEqual(todayview.recover_days(310, 100, 10, 31), 0)

    def test_needing_every_remaining_day_is_unrecoverable(self):
        # $5 left: 10 − 0.5 rounds up to all 10 days — no day left to
        # spend the recovered daily on
        self.assertIsNone(todayview.recover_days(310, 305, 10, 31))

    def test_over_the_month_is_unrecoverable(self):
        self.assertIsNone(todayview.recover_days(310, 310, 10, 31))
        self.assertIsNone(todayview.recover_days(310, 400, 10, 31))

    def test_degenerate_inputs(self):
        self.assertIsNone(todayview.recover_days(0, 0, 10, 31))
        self.assertIsNone(todayview.recover_days(310, 100, 0, 31))


class RecoverDaysOnTheTilesTests(unittest.TestCase):
    """TODAY is the 15th of a 31-day month: 17 days left, $1,000 food and
    other budgets, so the plan daily is $32."""

    def setUp(self):
        self.conn = make_db()
        # an established household: the tiles spread the whole month's
        # budget only for a ledger that predates the month
        add_ledger_history(self.conn, TODAY)
        write_config(self.conn, today_view="detail")
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_txn(self.conn, TODAY, 42.50, "SAFEWAY", primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 19.99, "TARGET")

    def tearDown(self):
        self.conn.close()

    def _tiles(self):
        d = report.gather(self.conn, TODAY)
        return d, dict(todayview.build_context(d)["day"]["allow_tiles"])

    def test_reduced_daily_says_how_many_days_bring_it_back(self):
        # food: $742.50 spent ($42.50 of it today, so today is used and
        # the rest spreads over the 16 days after it), $257.50 left →
        # daily $16; nine no-spend days leave 257.50/7 = $36.79 ≥ $32.26,
        # eight leave 257.50/8 = $32.19 — one short
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 700.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        d, tiles = self._tiles()
        food = tiles["Food"]
        self.assertEqual(food["daily_note"], "daily reduced from $32 to $16")
        self.assertEqual(food["recover_days"], 9)
        self.assertEqual(food["recover_note"],
                         "skip 9 days of spending and the daily is back to $32")
        # other is barely touched: nothing to recover, so no line at all
        self.assertIsNone(tiles["Everything else"]["recover_note"])
        self.assertIsNone(tiles["Everything else"]["recover_days"])
        # the same sentence on both email surfaces
        _, plain, html = report.build(d)
        for surface in (html, plain):
            self.assertIn("skip 9 days of spending and the daily is back "
                          "to $32", surface)

    def test_one_day_reads_as_one_no_spend_day(self):
        # $497.50 left over the 16 days after today → 15.42 plan-days:
        # one skipped day
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 460.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        d, tiles = self._tiles()
        self.assertEqual(tiles["Food"]["daily_note"],
                         "daily reduced from $32 to $31")
        self.assertEqual(tiles["Food"]["recover_days"], 1)
        self.assertEqual(tiles["Food"]["recover_note"],
                         "one no-spend day and the daily is back to $32")
        _, plain, html = report.build(d)
        for surface in (html, plain):
            self.assertIn("one no-spend day and the daily is back to $32",
                          surface)

    def test_over_for_the_month_says_so_instead_of_a_number(self):
        add_txn(self.conn, TODAY, 1180.0, "GADGET HUB",
                primary="GENERAL_MERCHANDISE")
        d, tiles = self._tiles()
        other = tiles["Everything else"]
        self.assertEqual(other["daily_note"], "daily reduced from $32 to $0")
        self.assertIsNone(other["recover_days"])
        self.assertEqual(other["recover_note"],
                         "can't get back to $32 this month")
        _, plain, html = report.build(d)
        # the HTML entity-escapes the apostrophe; the words are the same
        for surface in (_html.unescape(html), plain):
            self.assertIn("can't get back to $32 this month", surface)

    def test_a_raised_daily_carries_no_recovery_line(self):
        d, tiles = self._tiles()
        for label, a in tiles.items():
            self.assertIsNone(a["recover_note"], label)
        _, plain, html = report.build(d)
        for surface in (html, plain):
            self.assertNotIn("no-spend", surface)
            self.assertNotIn("skip ", surface)
            self.assertNotIn("get back to", surface)

    def test_carveout_child_recovers_on_its_own_numbers(self):
        from oikonome.engine import budget as _b
        cfg = _b.load_config(self.conn)
        cfg["custom_buckets"] = [{"name": "Coffee", "parent": "food",
                                  "monthly": 100,
                                  "merchants": ["STARBUCKS"]}]
        _b.save_config(self.conn, cfg)
        self.conn.commit()
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 80.0, "STARBUCKS",
                primary="FOOD_AND_DRINK")
        d, tiles = self._tiles()
        # child: $20 left over 17 days against a $3.23 plan → 6.2 plan-days
        # → 11 no-spend days (20/6 = $3.33 ≥ plan; 20/7 = $2.86 short)
        self.assertEqual(tiles["Coffee"]["recover_days"], 11)
        self.assertEqual(tiles["Coffee"]["recover_note"],
                         "skip 11 days of spending and the daily is back to $3")
        # the parent's carved remainder is ahead → nothing to recover
        self.assertEqual(tiles["Food"]["daily_note_tone"], "pos")
        self.assertIsNone(tiles["Food"]["recover_note"])
        _, plain, html = report.build(d)
        for surface in (html, plain):
            self.assertIn("skip 11 days of spending and the daily is back "
                          "to $3", surface)

    def test_api_payload_carries_the_recovery_fields(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=2), 700.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        d, _ = self._tiles()
        day = todayview.build_context(d)["day"]
        for k in ("food", "other"):
            self.assertIn("recover_days", day["allow"][k])
            self.assertIn("recover_note", day["allow"][k])


class LastDayOfTheMonthTests(unittest.TestCase):
    """10/31 is the month's last day: what is left of today is the whole
    rate, and with no tomorrow there is no reduced daily to recover — any
    spend on the 31st must not read "can't get back this month". Budgets
    are $310 food (a $10 plan) and $310 other."""

    LAST = dt.date(2026, 10, 31)

    def _day(self, today, food_before, food_today):
        rows = []
        if food_before:
            rows.append({"date": today - dt.timedelta(days=3),
                         "amount": food_before})
        if food_today:
            rows.append({"date": today, "amount": food_today})
        return {"verdict": "ON BUDGET", "variance": 0, "reasons": [],
                "today": today, "days_in_month": 31,
                "buckets": {
                    "food": {"month_budget": 310.0,
                             "actual": food_before + food_today,
                             "rows": rows},
                    "other": {"month_budget": 310.0, "actual": 300.0,
                              "rows": []}}}

    def _food(self, d):
        return dict(todayview.allowances(d)[2])["Food"]

    def test_spend_before_today_leaves_todays_remainder_without_a_note(self):
        food = self._food(self._day(self.LAST, 305.0, 0.0))
        self.assertAlmostEqual(food["rate"], 5.0, 2)
        self.assertEqual(food["rate_days"], 1)
        self.assertAlmostEqual(food["left_today"], 5.0, 2)
        self.assertIsNone(food["daily_note"])
        self.assertIsNone(food["recover_note"])
        self.assertIsNone(food["recover_days"])

    def test_spend_today_leaves_todays_remainder_without_a_note(self):
        food = self._food(self._day(self.LAST, 300.0, 3.0))
        # the rate is what is left of today: $10 allowance less $3 spent
        self.assertAlmostEqual(food["today_allowance"], 10.0, 2)
        self.assertAlmostEqual(food["rate"], food["left_today"], 2)
        self.assertAlmostEqual(food["rate"], 7.0, 2)
        self.assertIsNone(food["daily_note"])
        self.assertIsNone(food["daily_note_tone"])
        self.assertIsNone(food["recover_note"])

    def test_the_day_before_still_says_its_daily_was_reduced(self):
        # 10/30 with spend today: the rate covers 10/31 alone, and a $5
        # tomorrow against a $10 plan is a reduced daily that one day
        # cannot recover
        food = self._food(self._day(dt.date(2026, 10, 30), 300.0, 5.0))
        self.assertEqual(food["rate_days"], 1)
        self.assertEqual(food["daily_note"], "daily reduced from $10 to $5")
        self.assertEqual(food["recover_note"],
                         "can't get back to $10 this month")


class SpendingWithinTodaysAllowanceTests(unittest.TestCase):
    """Once today has spend, today is charged at the larger of its
    allowance and what was spent: a small purchase never "raises" the
    daily, and what is left of today plus rate x rate_days is exactly what
    is left of the month — today's unspent allowance is never handed out
    twice. September 2026 has 30 days; Food $900 is a $30/day plan."""

    def _day(self, today, food_before, food_today, budget_=900.0):
        rows = []
        if food_before:
            rows.append({"date": today.replace(day=1),
                         "amount": food_before})
        if food_today:
            rows.append({"date": today, "amount": food_today})
        return {"verdict": "ON BUDGET", "variance": 0, "reasons": [],
                "today": today, "days_in_month": 30,
                "buckets": {
                    "food": {"month_budget": budget_,
                             "actual": food_before + food_today,
                             "rows": rows},
                    "other": {"month_budget": 900.0, "actual": 0.0,
                              "rows": []}}}

    def _food(self, d):
        return dict(todayview.allowances(d)[2])["Food"]

    def test_a_one_dollar_purchase_never_raises_the_daily(self):
        # day 28, on pace: $810 through yesterday, $90 over 3 days
        food = self._food(self._day(dt.date(2026, 9, 28), 810.0, 1.0))
        self.assertAlmostEqual(food["left_today"], 29.0, 2)
        self.assertAlmostEqual(food["rate"], 30.0, 2)
        self.assertIsNone(food["daily_note"])
        # early in the month the same: $3,000 budget, day 2, on pace
        food = self._food(self._day(dt.date(2026, 9, 2), 100.0, 1.0, 3000.0))
        self.assertIsNone(food["daily_note"])
        self.assertAlmostEqual(food["rate"], 100.0, 2)

    def test_spending_past_the_allowance_reduces_the_daily(self):
        food = self._food(self._day(dt.date(2026, 9, 28), 810.0, 40.0))
        # $90 at midnight, $40 spent today: $50 over the 2 days after
        self.assertAlmostEqual(food["rate"], 25.0, 2)
        self.assertEqual(food["daily_note"], "daily reduced from $30 to $25")

    def test_left_today_plus_the_spread_rate_is_what_is_left(self):
        for day in (2, 10, 20, 28, 29):
            for before in (0.0, 300.0, 810.0, 899.0, 950.0):
                for spent in (0.0, 1.0, 29.0, 30.0, 45.0, 120.0):
                    d = dt.date(2026, 9, day)
                    food = self._food(self._day(d, before, spent))
                    remainder = 900.0 - before - spent
                    # with nothing spent yet the rate covers today too
                    total = ((max(0.0, food["left_today"]) if spent else 0.0)
                             + food["rate"] * food["rate_days"])
                    self.assertAlmostEqual(
                        total, max(0.0, remainder), 1,
                        f"day {day} before {before} today {spent}")

    def test_a_refund_today_is_left_today_once_not_twice(self):
        # day 21: $600 through yesterday leaves $300 over 10 days, $30 each
        # at midnight; a $50 return posts today. Left today is the $30 plus
        # the $50 back, and spending it all leaves $270 over the 9 days
        # after today — the daily stays at plan.
        food = self._food(self._day(dt.date(2026, 9, 21), 600.0, -50.0))
        self.assertAlmostEqual(food["left_today"], 80.0, 2)
        # a $30 buy and a $50 return net to a $20 refund the same way
        d = self._day(dt.date(2026, 9, 21), 600.0, 0.0)
        d["buckets"]["food"]["rows"] += [
            {"date": dt.date(2026, 9, 21), "amount": 30.0},
            {"date": dt.date(2026, 9, 21), "amount": -50.0}]
        d["buckets"]["food"]["actual"] = 580.0
        self.assertAlmostEqual(self._food(d)["left_today"], 50.0, 2)

    def test_the_text_never_promises_a_raised_rate_after_a_small_purchase(self):
        from oikonome.notify import render
        t = render.short_daily(self._day(dt.date(2026, 9, 28), 810.0, 1.0))
        self.assertIn("$30/day Food", t)
        self.assertIn("— 2 days left.", t)


class TextDaysLeftIsTheRatesSpanTests(unittest.TestCase):
    """The text's "Spend at most $X/day — N days left" names the days the
    rates are spread over: once today has spend that is the days after
    today, never the calendar count that would promise a day's room the
    rate does not hold."""

    def _day(self, today, food_today, other_today):
        def rows(amount):
            return ([{"date": today, "amount": amount}] if amount else [])
        return {"verdict": "ON BUDGET", "variance": 0, "reasons": [],
                "today": today, "days_in_month": 31,
                "buckets": {
                    "food": {"month_budget": 310.0, "actual": food_today,
                             "rows": rows(food_today)},
                    "other": {"month_budget": 310.0, "actual": other_today,
                              "rows": rows(other_today)}}}

    def test_spend_today_counts_the_days_after_today(self):
        from oikonome.notify import render
        t = render.short_daily(self._day(dt.date(2026, 10, 21), 5.0, 5.0))
        # 31 - 21 + 1 = 11 calendar days; today is used, so 10
        self.assertIn("— 10 days left.", t)

    def test_no_spend_today_counts_today(self):
        from oikonome.notify import render
        t = render.short_daily(self._day(dt.date(2026, 10, 21), 0.0, 0.0))
        self.assertIn("— 11 days left.", t)

    def test_tiles_that_differ_name_the_shorter_span(self):
        from oikonome.notify import render
        t = render.short_daily(self._day(dt.date(2026, 10, 21), 5.0, 0.0))
        self.assertIn("— 10 days left.", t)

    def test_a_single_day_reads_singular(self):
        from oikonome.notify import render
        t = render.short_daily(self._day(dt.date(2026, 10, 31), 5.0, 0.0))
        self.assertIn("— 1 day left.", t)


if __name__ == "__main__":
    unittest.main()
