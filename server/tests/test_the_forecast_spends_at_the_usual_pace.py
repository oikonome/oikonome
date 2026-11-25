"""The 60-day forecast spends at the household's usual pace: its variable
spending over the 60 finished days before today, less that window's
one-offs, per finished day (forecast.forecast_pace,
specs/today-figures.md "The forecast's pace is the last 60 days"). The
month's own readers — the verdict, the tiles' "running $X/day",
budget.paced_daily — keep the month to date. Figures and names are
invented."""

import datetime as dt
import random
import unittest

from oikonome.engine import budget, forecast
from oikonome.web import todayview

from . import props
from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date


def _daily(conn, first: dt.date, last: dt.date, amount: float, name="CORNER CAFE"):
    d = first
    while d <= last:
        add_txn(conn, d, amount, name, primary="FOOD_AND_DRINK")
        d += dt.timedelta(days=1)


class UsualPace(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=1500, other_monthly=1500)

    def tearDown(self):
        self.conn.close()

    def test_steady_spending_is_the_rate_on_any_day_of_the_month(self):
        _daily(self.conn, D(2026, 4, 1), D(2026, 8, 3), 50.0)
        for today in (D(2026, 7, 15), D(2026, 7, 31), D(2026, 8, 1),
                      D(2026, 8, 3)):
            with self.subTest(today=today):
                p = forecast.forecast_pace(self.conn, today)
                self.assertEqual((p["basis"], p["days"]), ("trailing", 60))
                self.assertAlmostEqual(p["daily"], 50.0, places=6)

    def test_a_heavy_last_month_is_mixed_in_and_the_month_reads_its_own(self):
        _daily(self.conn, D(2026, 5, 1), D(2026, 6, 30), 200.0)
        _daily(self.conn, D(2026, 7, 1), D(2026, 7, 14), 40.0)
        st = budget.month_status(self.conn, TODAY)
        p = forecast.forecast_pace(self.conn, TODAY, st=st)
        self.assertAlmostEqual(p["daily"], (46 * 200.0 + 14 * 40.0) / 60,
                               places=6)
        # the month's pace is this month's alone
        self.assertAlmostEqual(budget.paced_daily(st), 40.0, places=6)
        fc = forecast.build(self.conn, today=TODAY)
        self.assertAlmostEqual(fc["pace_basis"]["daily"],
                               round(p["daily"], 2), places=2)
        self.assertEqual(
            todayview.pace_sentence(fc).split(" A heavier month")[0],
            "The forecast spends $163 a day on everyday purchases — your "
            "last 60 days.")

    def test_the_sentence_names_the_envelope_money_the_walk_spends(self):
        from .util import add_bill
        _daily(self.conn, D(2026, 5, 1), D(2026, 7, 14), 50.0)
        add_bill(self.conn, "Garden Center", 310.0, bill_type="envelope",
                 frequency=None, interval=1, merchant="GARDEN CENTER")
        fc = forecast.build(self.conn, today=TODAY)
        env = fc["envelope_daily"]
        self.assertGreater(env, 0.5)
        # the two together are the rate every walk spends
        self.assertAlmostEqual(fc["pace_basis"]["daily"] + env,
                               fc["pace_usual"]["rate"], places=2)
        money = todayview.charts._money
        self.assertEqual(
            todayview.pace_sentence(fc).split(" A heavier month")[0],
            f"The forecast spends {money(fc['pace_basis']['daily'])} a day "
            "on everyday purchases — your last 60 days — plus "
            f"{money(env)} a day of envelope money.")

    def test_a_ledger_begun_this_month_burns_the_verdicts_pace(self):
        _daily(self.conn, D(2026, 7, 1), D(2026, 7, 14), 35.0)
        st = budget.month_status(self.conn, TODAY)
        p = forecast.forecast_pace(self.conn, TODAY, st=st)
        self.assertEqual(p["days"], st["pace_days"])
        self.assertAlmostEqual(p["daily"], budget.paced_daily(st), places=6)

    def test_a_ledger_under_a_week_old_burns_the_plan(self):
        _daily(self.conn, D(2026, 7, 12), D(2026, 7, 14), 300.0)
        p = forecast.forecast_pace(self.conn, TODAY)
        self.assertEqual(p["basis"], "plan")
        self.assertAlmostEqual(p["daily"], 3000.0 / 31, places=6)
        fc = forecast.build(self.conn, today=TODAY)
        self.assertIn("plan's daily spending", todayview.pace_sentence(fc))

    def test_a_one_off_counts_inside_the_window_and_not_after_it(self):
        _daily(self.conn, D(2026, 3, 1), D(2026, 7, 14), 30.0)
        base = forecast.forecast_pace(self.conn, TODAY)["daily"]
        # 50 days ago: left out and named
        add_txn(self.conn, TODAY - dt.timedelta(days=50), 1500.0,
                "NORTHWIND GARAGE", txn_id="repair")
        p = forecast.forecast_pace(self.conn, TODAY)
        self.assertEqual([o["amount"] for o in p["one_offs"]], [1500.0])
        self.assertAlmostEqual(p["daily"], base, places=6)
        fc = forecast.build(self.conn, today=TODAY)
        self.assertIn("less a one-off charge: NORTHWIND GARAGE $1,500 "
                      "(05/26/26).", todayview.pace_sentence(fc))
        # 61 days ago: out of the window, not named, the rate unmoved
        add_txn(self.conn, TODAY - dt.timedelta(days=61), 1500.0,
                "NORTHWIND GARAGE", txn_id="repair")
        p = forecast.forecast_pace(self.conn, TODAY)
        self.assertEqual(p["one_offs"], [])
        self.assertAlmostEqual(p["daily"], base, places=6)

    def test_a_charge_the_live_schedule_calls_a_bill_is_never_pace(self):
        _daily(self.conn, D(2026, 4, 1), D(2026, 7, 14), 40.0)
        add_bill(self.conn, "RIVERTOWN WATER", 80.0,
                 next_due=D(2026, 7, 25), last_seen=D(2026, 6, 25))
        add_txn(self.conn, D(2026, 6, 25), 80.0, "RIVERTOWN WATER",
                txn_id="water-jun")
        rows, _since, _days = forecast.trailing_variable_rows(
            self.conn, TODAY)
        self.assertNotIn("water-jun", {r["txn_id"] for r in rows})
        self.assertAlmostEqual(
            forecast.forecast_pace(self.conn, TODAY)["daily"], 40.0, places=6)


class UsualPaceHoldsForGeneratedLedgers(unittest.TestCase):
    """For generated ledgers: the rate times the finished days is the
    window's rows less its one-offs, to the cent, above the floor; a
    charge inside the window that is no one-off never lowers it; spending
    before the window never moves it; and the month's own pace and
    verdict do not read the window at all."""

    def test_the_rate_is_its_window(self):
        for seed in props.seeds(4):
            with self.subTest(seed=seed):
                rng = random.Random(seed)
                conn = make_db()
                try:
                    write_config(conn, food_monthly=300, other_monthly=300)
                    d = TODAY - dt.timedelta(days=rng.randint(70, 150))
                    while d < TODAY:
                        if rng.random() < 0.7:
                            add_txn(conn, d, float(rng.randint(5, 90)),
                                    f"SHOP {rng.randint(1, 6)}")
                        d += dt.timedelta(days=1)
                    if rng.random() < 0.5:
                        add_txn(conn, TODAY - dt.timedelta(
                            days=rng.randint(1, 59)), 2400.0,
                            "HARBOR APPLIANCES")
                    st = budget.month_status(conn, TODAY)
                    paced, verdict = budget.paced_daily(st), st["verdict"]
                    p = forecast.forecast_pace(conn, TODAY, st=st)
                    rows, since, days = forecast.trailing_variable_rows(
                        conn, TODAY, st=st)
                    want = (sum(r["amount"] for r in rows)
                            - sum(o["amount"] for o in p["one_offs"]))
                    if p["daily"] > p["plan_daily"] * 0.25:
                        self.assertEqual(round(p["daily"] * days * 100),
                                         round(want * 100))
                    # spending before the window: the rate does not move,
                    # nor does the month's own pace or verdict
                    add_txn(conn, since - dt.timedelta(days=3), 777.0,
                            "SHOP 1")
                    st2 = budget.month_status(conn, TODAY)
                    p2 = forecast.forecast_pace(conn, TODAY, st=st2)
                    self.assertAlmostEqual(p2["daily"], p["daily"], places=6)
                    self.assertEqual((budget.paced_daily(st2),
                                      st2["verdict"]), (paced, verdict))
                    # an ordinary charge inside the window never lowers it
                    add_txn(conn, TODAY - dt.timedelta(days=2), 15.0,
                            "SHOP 2")
                    p3 = forecast.forecast_pace(conn, TODAY)
                    self.assertGreaterEqual(p3["daily"] + 1e-9, p2["daily"])
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
