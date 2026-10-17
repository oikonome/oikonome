"""Every surface that paces the month reads a short ledger the way the
verdict does — through one window, budget.pace_window
(specs/short-ledger-pacing.md).

The window runs from the later of the 1st and the ledger's first day,
counts FINISHED days (through yesterday; today is not over), and spreads
the budget's share from that start to month end. The seed and the
suggestion take their rate over the same finished days, the Today tiles
and the hero hand out the same share, the forecast and the month lens
project from the same pace, and a closed first month judges the same
share its last live day did. When any of them drifts, a household reads
OVER on exactly the spending its budget came from, or follows its tiles
into an OVER verdict.
"""

import datetime as dt
import re
import unittest
import uuid

from oikonome.engine import budget, forecast
from oikonome.notify import render
from oikonome.web import lenses, report, todayview

from .util import add_ledger_history, add_txn, make_db, write_config

TODAY = dt.date(2026, 7, 22)            # day 22 of a 31-day July
PER_DAY = 100.0                         # 40 food + 60 everything else
# a plan of exactly PER_DAY a day over a 31-day month
FOOD_PLAN, OTHER_PLAN = 1200, 1900


def _spend(conn, start: dt.date, end: dt.date, food=40.0, other=60.0):
    d = start
    while d <= end:
        add_txn(conn, d, food, "CORNER GROCER", primary="FOOD_AND_DRINK")
        add_txn(conn, d, other, "HARDWARE BARN")
        d += dt.timedelta(days=1)


def _ledger(days: int, today: dt.date = TODAY):
    conn = make_db()
    _spend(conn, today - dt.timedelta(days=days - 1), today)
    return conn


def _accept_suggestion(conn, today: dt.date) -> dict:
    write_config(conn)
    out = budget.suggest_budgets(conn, today)
    write_config(conn, food_monthly=out["suggestions"]["food_monthly"],
                 other_monthly=out["suggestions"]["other_monthly"])
    return out


def _follow_the_tiles(test, conn, first_day: dt.date, days: int):
    """Spend exactly each tile's allowance for `days` days from
    `first_day`, and assert the verdict never turns OVER — on the day
    itself, after the spending, or on the next morning."""
    day = first_day
    for _ in range(days):
        st = report.gather(conn, day)
        _a, _k, tiles = todayview.allowances(st)
        for label, a in tiles:
            add_txn(conn, day, a["today_allowance"],
                    "CORNER GROCER" if label == "Food" else "HARDWARE BARN",
                    primary=("FOOD_AND_DRINK" if label == "Food"
                             else "GENERAL_MERCHANDISE"))
        for when in (day, day + dt.timedelta(days=1)):
            st = budget.month_status(conn, when)
            test.assertNotEqual(
                st["verdict"], "OVER BUDGET",
                f"{when}: actual {st['variable_actual']:.2f} vs expected "
                f"{st['variable_expected']:.2f}")
        day += dt.timedelta(days=1)


class PacersAgreeTests(unittest.TestCase):

    def test_a_mid_month_ledger_paces_at_its_own_daily_rate(self):
        for days in (budget.MIN_RATE_DAYS, 10, 15):
            with self.subTest(days=days):
                conn = _ledger(days)
                try:
                    _accept_suggestion(conn, TODAY)
                    st = budget.month_status(conn, TODAY)
                    self.assertEqual(st["verdict"], "ON BUDGET")
                    # spend through yesterday over the finished days since
                    # the ledger began — not over the 21 calendar days
                    self.assertEqual(st["pace_days"], days - 1)
                    self.assertEqual(st["ledger_days"], days - 1)
                    self.assertAlmostEqual(budget.paced_daily(st), PER_DAY)
                    paced_days = st["pace"]["paced_days"]
                    self.assertEqual(paced_days, days - 1 + 10)
                    proj = lenses._projection(st)
                    self.assertEqual(proj["verdict"], "ON BUDGET")
                    self.assertAlmostEqual(proj["pace_total"],
                                           PER_DAY * paced_days, delta=1)
                    fc = forecast.build(conn, today=TODAY, days=30)
                    self.assertAlmostEqual(fc["pace"]["rate"], PER_DAY,
                                           delta=0.01)
                finally:
                    conn.close()

    def test_a_days_old_ledger_is_read_at_its_rate_but_never_called_under(self):
        """Two finished days: the suggestion is their rate, not a week's
        worth of it; the window is not settled, so the projection is the
        plan and the forecast burns at the plan rate."""
        conn = _ledger(3)
        try:
            _accept_suggestion(conn, TODAY)
            st = budget.month_status(conn, TODAY)
            self.assertEqual(st["verdict"], "ON BUDGET")
            self.assertFalse(st["pace_settled"])
            self.assertEqual(st["pace_days"], 2)
            self.assertAlmostEqual(budget.paced_daily(st), PER_DAY)
            proj = lenses._projection(st)
            self.assertEqual(proj["verdict"], "ON BUDGET")
            self.assertAlmostEqual(proj["pace_total"],
                                   st["variable_budget_paced"], 1)
            budget_total = (st["buckets"]["food"]["month_budget"]
                            + st["buckets"]["other"]["month_budget"])
            fc = forecast.build(conn, today=TODAY, days=30)
            self.assertAlmostEqual(fc["pace"]["rate"], budget_total / 31,
                                   delta=0.01)
        finally:
            conn.close()

    def test_a_days_old_ledger_that_blows_the_month_projects_over(self):
        conn = make_db()
        try:
            add_txn(conn, TODAY - dt.timedelta(days=1), 2500.0, "HARDWARE BARN")
            write_config(conn, food_monthly=FOOD_PLAN, other_monthly=OTHER_PLAN)
            st = budget.month_status(conn, TODAY)
            self.assertEqual(st["verdict"], "OVER BUDGET")
            self.assertEqual(lenses._projection(st)["verdict"], "OVER BUDGET")
        finally:
            conn.close()

    def test_a_ledger_from_before_the_month_paces_on_the_calendar(self):
        conn = _ledger(60)
        try:
            write_config(conn, food_monthly=FOOD_PLAN, other_monthly=OTHER_PLAN)
            st = budget.month_status(conn, TODAY)
            self.assertEqual(st["pace_days"], TODAY.day - 1)
            self.assertEqual(st["month_share"], 1.0)
            self.assertTrue(st["pace_settled"])
            self.assertAlmostEqual(budget.paced_daily(st), PER_DAY)
            self.assertEqual(lenses._projection(st)["verdict"], "ON BUDGET")
        finally:
            conn.close()


class SuggestionFromFinishedDaysTests(unittest.TestCase):
    """The suggestion counts the days the verdict counts: a normal
    morning with nothing posted yet is not a day of no spending."""

    DAY = dt.date(2026, 10, 16)

    def test_an_accepted_suggestion_with_nothing_posted_today_is_on(self):
        conn = make_db()
        try:
            _spend(conn, dt.date(2026, 10, 10), dt.date(2026, 10, 15))
            out = _accept_suggestion(conn, self.DAY)
            self.assertEqual(out["history_days"], 6)
            self.assertEqual(out["suggestions"]["food_monthly"] +
                             out["suggestions"]["other_monthly"],
                             PER_DAY * 31)
            st = budget.month_status(conn, self.DAY)
            self.assertEqual(st["verdict"], "ON BUDGET")
            self.assertAlmostEqual(st["variance"], 0.0, delta=0.5)
            # and the tiles agree: on pace, no daily reduced
            _a, _k, tiles = todayview.allowances(report.gather(conn, self.DAY))
            self.assertTrue(all(a["daily_note"] is None for _l, a in tiles))
        finally:
            conn.close()

    def test_the_seed_reads_its_window_like_the_suggestion(self):
        conn = make_db()
        try:
            _spend(conn, dt.date(2026, 10, 10), dt.date(2026, 10, 15))
            seed = budget._seed_config(conn, today=self.DAY)
            self.assertEqual(seed["food_monthly"] + seed["other_monthly"],
                             PER_DAY * 31)
        finally:
            conn.close()


class FollowingTheTilesTests(unittest.TestCase):
    """A household that accepts the suggestion and then spends exactly
    what its tiles hand out never reads OVER."""

    def test_a_five_day_ledger_followed_for_six_days(self):
        day = dt.date(2026, 10, 16)
        conn = _ledger(5, day)
        try:
            _accept_suggestion(conn, day)
            _follow_the_tiles(self, conn, day + dt.timedelta(days=1), 6)
        finally:
            conn.close()

    def test_a_ledger_across_the_month_boundary_followed_for_a_week(self):
        day = dt.date(2026, 10, 3)
        conn = make_db()
        try:
            _spend(conn, dt.date(2026, 9, 28), day)
            _accept_suggestion(conn, day)
            _follow_the_tiles(self, conn, day + dt.timedelta(days=1), 7)
        finally:
            conn.close()

    def test_steady_spending_across_the_boundary_stays_on_budget(self):
        """The suggestion is the ledger's daily rate as a month, so the
        same steady spending is on budget on every day after it."""
        day = dt.date(2026, 10, 3)
        conn = make_db()
        try:
            _spend(conn, dt.date(2026, 9, 28), day)
            _accept_suggestion(conn, day)
            for d in range(4, 20):
                when = dt.date(2026, 10, d)
                self.assertEqual(budget.month_status(conn, when)["verdict"],
                                 "ON BUDGET", when)
                _spend(conn, when, when)
        finally:
            conn.close()


class TodayTilesAgreeTests(unittest.TestCase):
    """The Today tiles, the email that mirrors them and the text's "$X/day"
    hand out the budget's share from the ledger's first day, not the whole
    month's budget over the days left."""

    def test_a_mid_month_ledgers_tiles_hand_out_its_own_daily_rate(self):
        conn = _ledger(10)
        try:
            _accept_suggestion(conn, TODAY)
            st = report.gather(conn, TODAY)
            self.assertEqual(st["pace_start"], TODAY - dt.timedelta(days=9))
            allow, _kids, tiles = todayview.allowances(st)
            day_allow = sum(a["today_allowance"] for _l, a in tiles)
            self.assertAlmostEqual(day_allow, PER_DAY, delta=1)
            # a household spending at its own rate is not told to cut back
            self.assertAlmostEqual(allow["food"]["plan"]
                                   + allow["other"]["plan"], PER_DAY,
                                   delta=1)
            # today's spend is exactly the day's share, so the rest of the
            # month keeps the same daily
            rates = sum(a["rate"] for _l, a in tiles)
            self.assertAlmostEqual(rates, PER_DAY, delta=1)
            sms = render.short_daily(st)
            self.assertIn(f"${round(allow['food']['rate'])}/day", sms)
            _s, plain, html = report.build(st)
            self.assertIn(f"of ${allow['food']['today_allowance']:,.0f}",
                          plain)
        finally:
            conn.close()

    def test_a_ledger_from_before_the_month_spreads_the_whole_budget(self):
        conn = _ledger(60)
        try:
            write_config(conn, food_monthly=FOOD_PLAN, other_monthly=OTHER_PLAN)
            st = report.gather(conn, TODAY)
            self.assertEqual(st["pace_start"], TODAY.replace(day=1))
            allow, _k, _t = todayview.allowances(st)
            days_left = 31 - TODAY.day + 1
            spent_before = 40.0 * (TODAY.day - 1)
            self.assertAlmostEqual(allow["food"]["today_allowance"],
                                   (FOOD_PLAN - spent_before) / days_left, 2)
        finally:
            conn.close()

    def test_todays_spend_is_credited_like_the_tile_credits_it(self):
        """A mid-month ledger that spends exactly today's tile allowance
        reads on budget to the cent: the verdict's credit for today is the
        tile's own (the paced share), not the whole month's."""
        conn = make_db()
        try:
            _spend(conn, TODAY - dt.timedelta(days=9),
                   TODAY - dt.timedelta(days=1), food=50.0, other=60.0)
            write_config(conn, food_monthly=FOOD_PLAN, other_monthly=OTHER_PLAN)
            _a, _k, tiles = todayview.allowances(report.gather(conn, TODAY))
            for label, a in tiles:
                add_txn(conn, TODAY, a["today_allowance"],
                        "CORNER GROCER" if label == "Food"
                        else "HARDWARE BARN",
                        primary=("FOOD_AND_DRINK" if label == "Food"
                                 else "GENERAL_MERCHANDISE"))
            st = budget.month_status(conn, TODAY)
            # nine days' spending ahead of a paced plan, and today's tile
            # spent: the variance is the nine days' overspend, no more
            spendable = (FOOD_PLAN + OTHER_PLAN) * st["month_share"]
            before = 9 * 110.0
            days_left = st["pace"]["days_left"]
            today_credit = (spendable - before) / days_left
            expected = ((FOOD_PLAN + OTHER_PLAN) * 9 / 31 + today_credit)
            self.assertAlmostEqual(st["variable_expected"], expected, 1)
        finally:
            conn.close()


class HeroAgreesWithTheTilesTests(unittest.TestCase):
    """The hero's "$X of $Y left · N days" is the remainder the tiles hand
    out a day at a time: X = the tiles' daily rates times the days left,
    on the JSON the web and the app read and in both email renders."""

    def test_the_email_hero_is_the_tiles_times_the_days_left(self):
        conn = _ledger(10)
        try:
            write_config(conn, food_monthly=FOOD_PLAN, other_monthly=OTHER_PLAN)
            st = report.gather(conn, TODAY)
            _a, _k, tiles = todayview.allowances(st)
            days_left = st["pace"]["days_left"]
            # today has spend: each rate covers the days after today
            self.assertTrue(all(a["rate_days"] == days_left - 1
                                for _l, a in tiles))
            # what is left of today plus the rate over the days after it
            tiles_total = sum(max(0.0, a["left_today"])
                              + a["rate"] * a["rate_days"] for _l, a in tiles)
            self.assertAlmostEqual(st["month_remaining_paced"], tiles_total, 1)
            self.assertAlmostEqual(
                st["variable_budget_paced"],
                3100.0 * (31 - (TODAY.day - 9) + 1) / 31, 2)
            _s, plain, html = report.build(st)
            hero = (f"${st['month_remaining_paced']:,.0f} of "
                    f"${st['variable_budget_paced']:,.0f} left · "
                    f"{days_left} days")
            self.assertIn(hero, plain)
            flat = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
            self.assertIn(f"${st['month_remaining_paced']:,.0f} of "
                          f"${st['variable_budget_paced']:,.0f} left", flat)
        finally:
            conn.close()

    def test_the_json_hero_is_the_tiles_times_the_days_left(self):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        from fastapi.testclient import TestClient

        import oikonome.web.app as appmod
        from oikonome.db import tenancy
        from oikonome.web.app import app

        from .util import _ensure_db, seed_accounts
        _ensure_db()
        appmod.DEV_MODE = True
        client = TestClient(app)
        r = client.post("/api/signup",
                        data={"email": f"hero-{uuid.uuid4().hex[:8]}@x.dev",
                              "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        tid = client.get("/api/me").json()["tenant_id"]
        today = dt.date.today()         # the API reads the real day
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=FOOD_PLAN, other_monthly=OTHER_PLAN)
            _spend(conn, today - dt.timedelta(days=5), today,
                   food=10.0, other=15.0)
        finally:
            conn.close()
        d = client.get("/api/today/full").json()
        tiles = [d["allow"]["food"], d["allow"]["other"]] + d["allow_kids"]
        # a tile with spend today keeps what is left of today and spreads
        # the rest over the days after it
        tiles_total = sum(
            (max(0.0, a["left_today"]) + a["rate"]
             * max(1, d["days_left"] - 1)) if a["spent_today"] > 0
            else a["rate"] * d["days_left"]
            for a in tiles)
        self.assertAlmostEqual(d["month_remaining_paced"], tiles_total,
                               delta=0.01 * d["days_left"] * len(tiles))
        self.assertAlmostEqual(
            d["month_remaining_paced"],
            d["variable_budget_paced"] - d["variable_actual"], delta=0.02)
        self.assertAlmostEqual(d["variable_budget_paced"],
                               d["variable_budget"] * d["month_share"],
                               delta=0.02)


    def test_spending_exactly_the_allowance_keeps_the_daily(self):
        """Before and after today's spend posts the hero is the tiles'
        rates over the days they cover, and spending exactly the
        allowance leaves the daily where it was — never "daily reduced"
        and a lower /day in the text."""
        day = dt.date(2026, 10, 16)
        conn = _ledger(90, day - dt.timedelta(days=1))
        try:
            write_config(conn, food_monthly=1240, other_monthly=1860)
            for posted in (False, True):
                with self.subTest(today_posted=posted):
                    if posted:
                        _spend(conn, day, day)
                    st = report.gather(conn, day)
                    _a, _k, tiles = todayview.allowances(st)
                    days_left = st["pace"]["days_left"]
                    self.assertEqual(days_left, 16)
                    want_days = days_left - 1 if posted else days_left
                    self.assertTrue(all(a["rate_days"] == want_days
                                        for _l, a in tiles))
                    self.assertAlmostEqual(
                        st["month_remaining_paced"],
                        sum(a["rate"] * a["rate_days"] for _l, a in tiles), 2)
                    self.assertAlmostEqual(
                        sum(a["rate"] for _l, a in tiles), PER_DAY, 2)
                    self.assertTrue(all(a["daily_note"] is None
                                        for _l, a in tiles))
                    sms = render.short_daily(st)
                    self.assertIn("$40/day Food, $60/day Everything else",
                                  sms)
        finally:
            conn.close()


class ClosedFirstMonthTests(unittest.TestCase):
    """A closed month the ledger began inside of judges the budget's
    share from the ledger's first day — the share its last live day
    judged — so the verdict does not flip when the month closes."""

    def test_the_closed_first_month_judges_like_its_last_live_day(self):
        for per_day, want in ((100.0, "ON BUDGET"), (70.0, "UNDER BUDGET"),
                              (130.0, "OVER BUDGET")):
            with self.subTest(per_day=per_day):
                conn = make_db()
                try:
                    _spend(conn, dt.date(2026, 9, 10), dt.date(2026, 10, 16),
                           food=per_day * 0.4, other=per_day * 0.6)
                    write_config(conn, food_monthly=FOOD_PLAN,
                                 other_monthly=OTHER_PLAN)
                    live = budget.month_status(conn, dt.date(2026, 9, 30))
                    closed = budget.month_status(conn, dt.date(2026, 9, 30),
                                                 closed=True)
                    self.assertEqual(live["verdict"], want)
                    self.assertEqual(closed["verdict"], want)
                    self.assertAlmostEqual(closed["variable_expected"],
                                           3100.0 * 21 / 30, 2)
                    lens = lenses.month_summary(conn, 2026, 9,
                                                dt.date(2026, 10, 16))
                    self.assertEqual(lens["verdict"], want)
                    self.assertAlmostEqual(lens["variable_budget"],
                                           3100.0 * 21 / 30, 1)
                finally:
                    conn.close()


class LensesNeedASavedBudgetTests(unittest.TestCase):
    """The Month and Year lenses judge only a budget the household made:
    with none saved, the loader's first-month guess gets no verdict."""

    def test_no_saved_budget_shows_no_verdict(self):
        conn = make_db()
        try:
            today = dt.date(2026, 10, 16)
            _spend(conn, dt.date(2026, 10, 10), today, food=400.0,
                   other=600.0)
            self.assertFalse(budget.budgets_set(conn))
            m = lenses.month_summary(conn, 2026, 10, today)
            self.assertIsNone(m["verdict"])
            self.assertIsNone(m["projection"])
            self.assertFalse(m["budget_set"])
            # the loader's guess is no verdict, and the spendable an older
            # app subtracts from never puts the month over
            self.assertGreaterEqual(m["variable_budget"],
                                    m["variable_actual"])
            y = lenses.year_summary(conn, 2026, today)
            cell = y["cells"][9]
            self.assertEqual(cell["status"], "net")
            self.assertIsNone(cell["verdict"])
        finally:
            conn.close()

    def test_the_monthly_text_of_an_unbudgeted_month_says_no_budget_set(self):
        conn = make_db()
        try:
            _spend(conn, dt.date(2026, 9, 10), dt.date(2026, 10, 16))
            self.assertFalse(budget.budgets_set(conn))
            m = lenses.month_summary(conn, 2026, 9, dt.date(2026, 10, 16))
            text = render.short_monthly(m, "September 2026")
            self.assertEqual(
                text, "Oikonome, September 2026: no budget set · variable "
                      "$2,100 · out $2,100.")
        finally:
            conn.close()

    def test_a_saved_budget_is_judged(self):
        conn = make_db()
        try:
            today = dt.date(2026, 10, 16)
            add_ledger_history(conn, today)
            _spend(conn, dt.date(2026, 10, 10), today, food=400.0,
                   other=600.0)
            write_config(conn)
            m = lenses.month_summary(conn, 2026, 10, today)
            self.assertEqual(m["verdict"], "OVER BUDGET")
            self.assertTrue(m["budget_set"])
            y = lenses.year_summary(conn, 2026, today)
            self.assertEqual(y["cells"][9]["status"], "projected")
        finally:
            conn.close()


class ShortLedgerWindowTests(unittest.TestCase):

    def test_a_month_that_ended_before_the_ledger_began_is_not_judged(self):
        for closed, today in ((True, dt.date(2026, 9, 15)),
                              (False, dt.date(2026, 9, 30))):
            with self.subTest(closed=closed):
                w = budget.pace_window_for(dt.date(2026, 10, 5), today,
                                           closed=closed)
                self.assertEqual(w["share"], 0)
                self.assertEqual(w["paced_days"], 0)
                self.assertEqual(w["elapsed_days"], 0)
        # the month the ledger begins in still paces from its first day
        w = budget.pace_window_for(dt.date(2026, 9, 30), dt.date(2026, 9, 15),
                                   closed=True)
        self.assertEqual(w["paced_days"], 1)

    def test_a_ledger_from_last_months_end_paces_from_the_first(self):
        conn = make_db()
        try:
            _spend(conn, dt.date(2026, 6, 30), dt.date(2026, 7, 3))
            write_config(conn, food_monthly=FOOD_PLAN, other_monthly=3100)
            st = budget.month_status(conn, dt.date(2026, 7, 3))
            self.assertEqual(st["pace_days"], 2)
            self.assertEqual(st["pace_start"], dt.date(2026, 7, 1))
            # three finished days of ledger: not yet a settled rate
            self.assertEqual(st["ledger_days"], 3)
            self.assertFalse(st["pace_settled"])
        finally:
            conn.close()

    def test_a_ledger_that_began_this_month_paces_from_its_first_day(self):
        conn = make_db()
        try:
            add_txn(conn, dt.date(2026, 7, 2), 100.0, "HARDWARE BARN")
            write_config(conn, food_monthly=FOOD_PLAN, other_monthly=3100)
            st = budget.month_status(conn, dt.date(2026, 7, 3))
            self.assertEqual(st["pace_days"], 1)
            self.assertAlmostEqual(st["month_share"], 30 / 31)
            self.assertFalse(st["pace_settled"])
        finally:
            conn.close()


class SuggestionHistoryTests(unittest.TestCase):

    def test_a_ledger_that_began_today_gets_no_suggestion(self):
        conn = make_db()
        try:
            _spend(conn, TODAY, TODAY)
            write_config(conn)
            out = budget.suggest_budgets(conn, TODAY)
            self.assertEqual(out["history_days"], 0)
            self.assertTrue(out["not_enough_history"])
            self.assertNotIn("food_monthly", out["suggestions"])
            self.assertNotIn("other_monthly", out["suggestions"])
            self.assertEqual(out["suggestions"]["buckets"], {})
            self.assertEqual(out["bucket_candidates"], [])
        finally:
            conn.close()
        conn = _ledger(2)
        try:
            write_config(conn)
            out = budget.suggest_budgets(conn, TODAY)
            self.assertFalse(out["not_enough_history"])
            self.assertGreater(out["suggestions"]["food_monthly"], 0)
        finally:
            conn.close()

    def test_the_suggestion_says_how_many_finished_days_stand_behind_it(self):
        for days, scaled in ((3, True), (12, True), (TODAY.day, True),
                             (80, False)):
            with self.subTest(days=days):
                conn = _ledger(days)
                try:
                    write_config(conn)
                    out = budget.suggest_budgets(conn, TODAY)
                    # today is not a finished day
                    self.assertEqual(out["history_days"], days - 1)
                    self.assertEqual(out["history_scaled"], scaled)
                    self.assertEqual(out["min_rate_days"],
                                     budget.MIN_RATE_DAYS)
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
