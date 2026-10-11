"""The live verdict, the forecast pace and the budget seed all measure
spending against the stretch of time it actually covers.

The verdict's expected-to-date counts the elapsed days plus the part of
today's allowance already used; its actual counts every row through today.
A household on pace through yesterday that spends exactly the allowance the
today-ledger hands out is ON BUDGET — the same day its tile reads $0 left.

The cash forecast's pace is spend through yesterday over the days through
yesterday, so on-plan spending (today included) walks at the plan rate.

The first-run seed and the history suggestion divide by the months the
ledger covers, so a month of history is not read as a third of a quarter.
"""

import datetime as dt
import unittest

from oikonome import localtime
from oikonome.engine import budget, forecast
from oikonome.web import todayview

from .util import TODAY, add_txn, make_db, write_config

# util config: food 1000 + other 1000, dynamic scaling off. TODAY is the
# 15th of a 31-day month.
DAYS = 31


class VerdictCountsTodaysAllowanceTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _on_pace_through_yesterday(self, today):
        per_day = 1000 / DAYS
        for d in range(1, today.day):
            day = today.replace(day=d)
            add_txn(self.conn, day, round(per_day, 2), "CORNER GROCER",
                    primary="FOOD_AND_DRINK")
            add_txn(self.conn, day, round(per_day, 2), "HARDWARE BARN")

    def _spend_todays_allowance(self, today):
        st = budget.month_status(self.conn, today)
        st["today"] = today
        allow, _, _ = todayview.allowances(st)
        add_txn(self.conn, today, allow["food"]["today_allowance"],
                "CORNER GROCER", primary="FOOD_AND_DRINK")
        add_txn(self.conn, today, allow["other"]["today_allowance"],
                "HARDWARE BARN")
        return allow

    def test_spending_exactly_todays_allowance_is_on_budget(self):
        self._on_pace_through_yesterday(TODAY)
        allow = self._spend_todays_allowance(TODAY)
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["verdict"], "ON BUDGET")
        self.assertAlmostEqual(st["variance"], 0.0, delta=0.1)
        self.assertIsNone(st["why"]["recovery"])
        # the tile on the same page agrees: nothing left, nothing over
        st["today"] = TODAY
        after, _, _ = todayview.allowances(st)
        self.assertAlmostEqual(after["food"]["left_today"], 0.0, delta=0.01)
        self.assertGreater(allow["food"]["today_allowance"], 25.0)

    def test_first_of_the_month_allowance_is_on_budget(self):
        first = TODAY.replace(day=1)
        self._spend_todays_allowance(first)
        st = budget.month_status(self.conn, first)
        self.assertEqual(st["verdict"], "ON BUDGET")

    def test_spending_past_todays_allowance_is_still_over(self):
        first = TODAY.replace(day=1)
        add_txn(self.conn, first, 400.0, "HARDWARE BARN")
        st = budget.month_status(self.conn, first)
        self.assertEqual(st["verdict"], "OVER BUDGET")
        self.assertAlmostEqual(st["variance"], 400 - 1000 / DAYS, delta=0.1)

    def test_a_morning_with_nothing_bought_sits_where_yesterday_ended(self):
        self._on_pace_through_yesterday(TODAY)
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["verdict"], "ON BUDGET")
        self.assertAlmostEqual(st["variance"], 0.0, delta=0.1)

    def test_a_closed_month_is_judged_against_the_whole_budget(self):
        end = TODAY.replace(day=DAYS)
        add_txn(self.conn, end, 60.0, "HARDWARE BARN")
        st = budget.month_status(self.conn, end, closed=True)
        self.assertAlmostEqual(st["variable_expected"], 2000.0, 2)


class ForecastPaceHorizonTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_on_plan_spending_including_today_walks_at_the_plan_rate(self):
        per_day = 2000 / DAYS
        for d in range(1, TODAY.day + 1):
            add_txn(self.conn, TODAY.replace(day=d), round(per_day, 4),
                    "HARDWARE BARN")
        fc = forecast.build(self.conn, today=TODAY, days=30)
        self.assertAlmostEqual(fc["pace"]["rate"], fc["plan"]["rate"],
                               delta=0.05)


class SeedDividesByCoveredMonthsTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_one_month_of_history_seeds_a_full_month(self):
        # ledger starts on the 1st of last month: one complete month
        last = (TODAY.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
        add_txn(self.conn, last, 900.0, "HARDWARE BARN")
        add_txn(self.conn, last + dt.timedelta(days=10), 600.0,
                "CORNER GROCER", primary="FOOD_AND_DRINK")
        seed = budget._seed_config(self.conn, today=TODAY)
        self.assertEqual(seed["other_monthly"], 900.0)
        self.assertEqual(seed["food_monthly"], 600.0)
        self.assertFalse(seed["_provisional"])

    def test_a_partial_month_is_never_scaled_up_and_is_not_saved(self):
        # ten days of history in the last full month — dated from the real
        # household day, because load_config seeds from that day
        today = localtime.now_local(None).date()
        last_end = today.replace(day=1) - dt.timedelta(days=1)
        add_txn(self.conn, last_end - dt.timedelta(days=9), 300.0,
                "HARDWARE BARN")
        seed = budget._seed_config(self.conn, today=today)
        self.assertEqual(seed["other_monthly"], 300.0)
        self.assertTrue(seed["_provisional"])
        # load_config hands the provisional seed out but does not keep it
        cfg = budget.load_config(self.conn)
        self.assertNotIn("_provisional", cfg)
        row = self.conn.execute(
            "SELECT config FROM tenant_settings").fetchone()
        self.assertNotIn("other_monthly", dict(row["config"]) if row else {})

    def test_under_a_closed_month_the_month_in_progress_joins_the_seed(self):
        """History from the 15th of last month: half a closed month plus
        the month so far is just over one month of spending, and the seed
        is that spending over that span, not last month's half alone."""
        last = (TODAY.replace(day=1) - dt.timedelta(days=1)).replace(day=15)
        add_txn(self.conn, last, 1600.0, "HARDWARE BARN")
        add_txn(self.conn, TODAY.replace(day=10), 1500.0, "HARDWARE BARN")
        seed = budget._seed_config(self.conn, today=TODAY)
        covered = (budget.month_coverage(last, last.year, last.month, TODAY)
                   + budget.month_coverage(last, TODAY.year, TODAY.month,
                                           TODAY))
        self.assertGreater(covered, 1.0)
        self.assertEqual(seed["other_monthly"], round(3100.0 / covered, -1))
        self.assertTrue(seed["_provisional"])

    def _partial_history(self, amount=300.0, back=9):
        today = localtime.now_local(None).date()
        last_end = today.replace(day=1) - dt.timedelta(days=1)
        add_txn(self.conn, last_end - dt.timedelta(days=back), amount,
                "HARDWARE BARN")

    def _stored(self):
        row = self.conn.execute(
            "SELECT config FROM tenant_settings").fetchone()
        return dict(row["config"]) if row else {}

    def test_an_unrelated_settings_write_does_not_keep_the_seed(self):
        self._partial_history()
        with budget.config_txn(self.conn) as cfg:
            self.assertIn("other_monthly", cfg)       # the seed is read
            cfg["timezone"] = "America/Denver"
        stored = self._stored()
        self.assertEqual(stored.get("timezone"), "America/Denver")
        self.assertNotIn("other_monthly", stored)
        self.assertNotIn("food_monthly", stored)
        # so the seed still tracks the backfill as it arrives
        before = budget.load_config(self.conn)["other_monthly"]
        self._partial_history(amount=500.0, back=20)
        self.assertGreater(budget.load_config(self.conn)["other_monthly"],
                           before)

    def test_a_budget_the_block_sets_is_kept(self):
        self._partial_history()
        with budget.config_txn(self.conn) as cfg:
            cfg["other_monthly"] = 1234.0
        self.assertEqual(self._stored().get("other_monthly"), 1234.0)


class SuggestionIgnoresThePartialMonthTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_full_month_plus_a_few_days_suggests_the_full_month(self):
        last = (TODAY.replace(day=1) - dt.timedelta(days=1)).replace(day=1)
        add_txn(self.conn, last, 1400.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY.replace(day=2), 100.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
        out = budget.suggest_budgets(self.conn, TODAY)
        self.assertEqual(out["suggestions"]["food_monthly"], 1400.0)

    def test_only_the_month_in_progress_is_not_scaled_up(self):
        add_txn(self.conn, TODAY.replace(day=2), 100.0, "CORNER GROCER",
                primary="FOOD_AND_DRINK")
        out = budget.suggest_budgets(self.conn, TODAY)
        self.assertEqual(out["suggestions"]["food_monthly"], 100.0)


if __name__ == "__main__":
    unittest.main()
