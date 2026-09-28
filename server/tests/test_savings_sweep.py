"""Sweep-mode savings goals: contribute only what the month's own
surplus covers.

A 'monthly'-mode goal is a fixed commitment — the forecast walks the
plan out unconditionally. A 'sweep'-mode goal models "I only move money
to savings if the rest of the budget worked out": at month end it takes
min(plan, realized surplus), floored at zero. The invariants under test:

- a sweep goal never makes the forecast read worse than having no goal
  at all (the headline: trough and shortfall date are untouched);
- a month with zero or negative surplus contributes nothing;
- the posted transfer (the same matched-transfer evidence progress runs
  on) marks the month satisfied, so nothing is scheduled twice;
- the Today line is one server-composed sentence, identical in the SPA
  payload, the email HTML and the plain text;
- several sweep goals share ONE month-end pool, claimed in config order,
  and the pool never hands out more than the month's surplus;
- a plan-only goal (nothing to measure against) is still scheduled every
  month and never reads as satisfied;
- the mode round-trips through the settings API, defaulting unchanged.
"""

import datetime as dt
import os
import unittest
import uuid

from oikonome.engine import budget, forecast, savings
from oikonome.web import report

from .util import TODAY, add_bill, add_txn, make_db, write_config

PLAN = 500.0


def _goal(**over):
    g = {"name": "Rainy day", "target": 0, "monthly_plan": PLAN,
         "account_id": "sav", "tokens": [], "start_balance": 0,
         "mode": "sweep"}
    g.update(over)
    return g


def _add_savings_account(conn):
    conn.execute(
        "INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
        " VALUES ('sav','it1','Test Savings','depository','savings',0)"
        " ON CONFLICT (tenant_id, id) DO NOTHING")


class SweepModeConfigTests(unittest.TestCase):
    def test_mode_defaults_to_monthly_for_existing_goals(self):
        # every pre-existing goal (no mode key) must behave exactly as
        # before this mode existed
        self.assertEqual(savings.goal_mode({"name": "Trip"}), "monthly")
        self.assertEqual(savings.goal_mode({"name": "Trip", "mode": ""}),
                         "monthly")
        self.assertEqual(savings.goal_mode({"name": "T", "mode": "SWEEP"}),
                         "sweep")

    def test_plan_totals_split_by_mode(self):
        cfg = {"savings_goals": [
            {"name": "Fixed", "monthly_plan": 300},
            {"name": "Sweepy", "monthly_plan": 500, "mode": "sweep"}]}
        self.assertEqual(savings.monthly_plan_total(cfg), 800.0)
        self.assertEqual(savings.monthly_plan_total(cfg, mode="monthly"),
                         300.0)
        self.assertEqual(savings.monthly_plan_total(cfg, mode="sweep"),
                         500.0)


class SweepSurplusTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        _add_savings_account(self.conn)

    def tearDown(self):
        self.conn.close()

    def _cfg(self, **over):
        write_config(self.conn, budgeted_income_monthly=8000, **over)

    def test_sweep_plan_leaves_the_excess_line_whole(self):
        """A sweep plan draws FROM the surplus when a month provides it,
        so it must not pre-reduce the surplus that defines it — only
        fixed plans subtract from excess cash."""
        self._cfg(savings_goals=[])
        base = budget.month_status(self.conn, TODAY)
        self._cfg(savings_goals=[_goal()])
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["plan_surplus"], base["plan_surplus"])
        self.assertEqual(st["savings_plan"], 0.0)
        self.assertEqual(st["sweep_plan"], PLAN)
        self._cfg(savings_goals=[_goal(mode="monthly")])
        st2 = budget.month_status(self.conn, TODAY)
        self.assertEqual(round(base["plan_surplus"]
                               - st2["plan_surplus"], 2), PLAN)

    def test_availability_is_the_months_own_surplus_floored_at_zero(self):
        # income 8000, no bills, budgets 2000 → standing surplus 6000;
        # nothing spent, so month-to-date availability = accrued surplus
        # plus the under-pace variance — always ≥ 0, never above what the
        # month's numbers produce
        self._cfg(savings_goals=[_goal()])
        st = budget.month_status(self.conn, TODAY)
        frac = (TODAY.day - 1) / st["days_in_month"]      # elapsed days
        expect = round(max(0.0, st["plan_surplus"] * frac - st["variance"]),
                       2)
        self.assertEqual(st["sweep_available"], expect)
        self.assertGreater(st["sweep_available"], 0)
        # a month that did NOT work out: no income to budget against
        write_config(self.conn, savings_goals=[_goal()])
        st = budget.month_status(self.conn, TODAY)
        self.assertIsNone(st["plan_surplus"])
        self.assertIsNone(st["sweep_available"])


class SweepForecastTests(unittest.TestCase):
    """The headline: forecast(sweep) never reads below forecast(no goal),
    and a fixed plan is the strictly-worse-or-equal comparison."""

    def setUp(self):
        self.conn = make_db()
        _add_savings_account(self.conn)
        # a paycheck keeps the walk rising so month ends have real cash;
        # a bill keeps the fixture honest about mid-month dips
        add_bill(self.conn, "Rent", 1500.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_bill(self.conn, "Paycheck Co", 4000.0, frequency="WEEKLY",
                 interval=2, next_due=TODAY + dt.timedelta(days=2),
                 income=True, last_seen=TODAY - dt.timedelta(days=12))

    def tearDown(self):
        self.conn.close()

    def _build(self, goals):
        write_config(self.conn, budgeted_income_monthly=8000,
                     food_monthly=1000, other_monthly=1000,
                     savings_goals=goals)
        return forecast.build(self.conn, today=TODAY, days=60)

    def test_sweep_never_degrades_the_forecast_below_the_no_goal_baseline(self):
        base = self._build([])
        swp = self._build([_goal()])
        for k in ("plan", "pace", "pace_now", "pace_nocards", "pace_stmt"):
            # the trough is untouched: sweep only ever leaves with money
            # the walk can spare above its own no-goal low point
            self.assertEqual(swp[k]["min"], base[k]["min"], k)
            self.assertEqual(swp[k]["min_date"], base[k]["min_date"], k)
            self.assertEqual(swp[k]["negative_date"],
                             base[k]["negative_date"], k)
            # end-of-horizon cash reduces by at most plan per month end
            self.assertLessEqual(base[k]["end"] - swp[k]["end"],
                                 2 * PLAN + 0.01, k)
            self.assertGreaterEqual(swp[k]["end"], base[k]["min"], k)
        # and the sweep actually happens when surplus exists — this is
        # not vacuous equality
        self.assertTrue(swp["pace_stmt"]["sweep_events"])
        for _, amt, label in swp["pace_stmt"]["sweep_events"]:
            self.assertLessEqual(-amt, PLAN + 0.01)
            self.assertIn("(sweep savings)", label)

    def test_sweep_is_never_more_pessimistic_than_the_fixed_plan(self):
        fixed = self._build([_goal(mode="monthly")])
        swp = self._build([_goal()])
        for k in ("plan", "pace", "pace_stmt"):
            self.assertGreaterEqual(swp[k]["min"], fixed[k]["min"], k)
            self.assertGreaterEqual(swp[k]["end"], fixed[k]["end"], k)

    def test_zero_surplus_month_contributes_nothing(self):
        # income exactly covers the budgets (no bills in this fixture)
        # → standing surplus is 0 — the month never produces anything
        # to sweep, so the goal must leave the forecast untouched
        write_config(self.conn, budgeted_income_monthly=2000,
                     food_monthly=1000, other_monthly=1000,
                     savings_goals=[_goal()])
        swp = forecast.build(self.conn, today=TODAY, days=60)
        write_config(self.conn, budgeted_income_monthly=2000,
                     food_monthly=1000, other_monthly=1000,
                     savings_goals=[])
        base = forecast.build(self.conn, today=TODAY, days=60)
        for k in ("plan", "pace", "pace_stmt"):
            self.assertEqual(swp[k]["series"], base[k]["series"], k)
            self.assertEqual(swp[k]["sweep_events"], [], k)

    def test_posted_transfer_marks_the_month_satisfied(self):
        # the real transfer posts mid-month (destination side, Plaid
        # signs: negative = money in) — the current month owes nothing
        # more, later months still project a sweep
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -PLAN,
                "TRANSFER TO SAVINGS", primary="TRANSFER_IN", account="sav")
        swp = self._build([_goal()])
        month_end = TODAY.replace(day=31).isoformat()
        cur = [e for e in swp["pace_stmt"]["sweep_events"]
               if e[0] <= month_end]
        nxt = [e for e in swp["pace_stmt"]["sweep_events"]
               if e[0] > month_end]
        self.assertEqual(cur, [], "a satisfied month must not be walked out")
        self.assertTrue(nxt, "future months still owe their sweep")
        # progress reads the same evidence and reports the month done
        cfg = budget.load_config(self.conn)
        p = savings.progress(self.conn, cfg, TODAY)[0]
        self.assertEqual(p["mode"], "sweep")
        self.assertEqual(p["swept_this_month"], PLAN)
        self.assertTrue(p["sweep_satisfied"])


class MultipleSweepGoalTests(unittest.TestCase):
    """Several sweep goals draw from ONE month-end pool.

    The invariant: a month can never sweep out more than that month's own
    surplus, however many goals want it, and the pool is claimed in config
    order — a later goal gets what is left, never at the expense of an
    earlier one. A regression here is silent: the forecast simply walks
    out money the month never produced.
    """

    def setUp(self):
        self.conn = make_db()
        _add_savings_account(self.conn)
        add_bill(self.conn, "Rent", 1500.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_bill(self.conn, "Paycheck Co", 4000.0, frequency="WEEKLY",
                 interval=2, next_due=TODAY + dt.timedelta(days=2),
                 income=True, last_seen=TODAY - dt.timedelta(days=12))

    def tearDown(self):
        self.conn.close()

    def _cfg(self, goals):
        write_config(self.conn, budgeted_income_monthly=8000,
                     food_monthly=1000, other_monthly=1000,
                     savings_goals=goals)

    def test_two_goals_split_one_months_surplus_in_config_order(self):
        # each plan alone fits the standing surplus; together they cannot
        big = 3000.0
        first = _goal(name="First", monthly_plan=big, tokens=["first"])
        second = _goal(name="Second", monthly_plan=big, tokens=["second"])
        self._cfg([])
        base = forecast.build(self.conn, today=TODAY, days=90)
        st = budget.month_status(self.conn, TODAY)
        # the month-end pool: the standing surplus for a future month, and
        # for the current month the surplus PROJECTED to month end (the
        # standing figure corrected by today's pace variance)
        std_room = max(0.0, st["plan_surplus"])
        cur_room = max(0.0, st["plan_surplus"] - (st.get("variance") or 0))
        self.assertLess(std_room, 2 * big)    # the pool really is short
        self._cfg([first, second])
        swp = forecast.build(self.conn, today=TODAY, days=90)

        for k in ("plan", "pace", "pace_now", "pace_nocards", "pace_stmt"):
            # the trough dollar and the shortfall date are the invariant.
            # min_DATE is deliberately not asserted here: each goal's take
            # is rounded to the cent, so several sequential takes can pull
            # a later day onto the same trough value and move the argmin
            # without the trough itself reading a cent worse.
            self.assertEqual(swp[k]["min"], base[k]["min"], k)
            self.assertEqual(swp[k]["negative_date"],
                             base[k]["negative_date"], k)
        events = swp["pace_stmt"]["sweep_events"]
        self.assertTrue(events, "a surplus month must sweep something")
        by_date: dict[str, dict[str, float]] = {}
        for when, amt, label in events:
            name = label.split(" (sweep savings)")[0]
            by_date.setdefault(when, {})[name] = -amt
        this_month = f"{TODAY.year:04d}-{TODAY.month:02d}"
        for when, takes in by_date.items():
            room = cur_room if when.startswith(this_month) else std_room
            self.assertLessEqual(sum(takes.values()), room + 0.01, when)
            for name, take in takes.items():
                self.assertLessEqual(take, big + 0.01, name)
            # config order: the later goal only ever gets the remainder,
            # so it takes nothing until the earlier one has its full plan
            if takes.get("Second", 0) > 0.005:
                self.assertAlmostEqual(takes.get("First", 0), big, 2)
        self.assertTrue(any("Second" in t for t in by_date.values()),
                        "the leftover pool must still reach the second goal")


class PlanOnlySweepGoalTests(unittest.TestCase):
    """A sweep goal with no destination account and no tokens.

    Nothing in the ledger can be attributed to it (matching unscoped would
    sum every transfer on the tenant), so it can never be marked
    satisfied and no transfer may suppress it: the forecast must keep
    scheduling the full plan every month, and the posted total the Today
    line reads must stay at zero rather than borrow a tracked sibling's
    evidence.
    """

    def setUp(self):
        self.conn = make_db()
        _add_savings_account(self.conn)
        # income events, so the walk rises and a month end has cash to
        # spare — a purely declining walk sweeps nothing by design
        add_bill(self.conn, "Paycheck Co", 4000.0, frequency="WEEKLY",
                 interval=2, next_due=TODAY + dt.timedelta(days=2),
                 income=True, last_seen=TODAY - dt.timedelta(days=12))
        write_config(self.conn, budgeted_income_monthly=8000,
                     food_monthly=1000, other_monthly=1000,
                     savings_goals=[_goal(account_id=None, tokens=[])])

    def tearDown(self):
        self.conn.close()

    def test_progress_reports_sweep_mode_with_nothing_measured(self):
        cfg = budget.load_config(self.conn)
        p = savings.progress(self.conn, cfg, TODAY)[0]
        self.assertTrue(p["plan_only"])
        self.assertEqual(p["mode"], "sweep")
        self.assertIsNone(p["swept_this_month"])
        self.assertIsNone(p["sweep_satisfied"])

    def test_a_transfer_it_cannot_own_never_marks_the_month_swept(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=3), -PLAN,
                "TRANSFER TO SAVINGS", primary="TRANSFER_IN", account="sav")
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["sweep_plan"], PLAN)
        self.assertEqual(st["sweep_posted"], 0.0)
        swp = forecast.build(self.conn, today=TODAY, days=60)
        month_end = TODAY.replace(day=31).isoformat()
        cur = [e for e in swp["pace_stmt"]["sweep_events"]
               if e[0] <= month_end]
        self.assertTrue(cur, "an unmeasurable goal is still owed its plan")
        self.assertAlmostEqual(-cur[0][1], PLAN, 2)


class SweepSurfaceParityTests(unittest.TestCase):
    """One composed sentence, three renders — the SPA payload, the email
    HTML and the plain text must carry the identical sweep line."""

    def setUp(self):
        self.conn = make_db()
        _add_savings_account(self.conn)
        write_config(self.conn, budgeted_income_monthly=8000,
                     savings_goals=[_goal()])

    def tearDown(self):
        self.conn.close()

    def test_email_and_plain_mirror_the_sweep_line(self):
        d = report.gather(self.conn, TODAY)
        from oikonome.web.todayview import build_context
        line = build_context(d)["day"]["sweep_line"]
        self.assertIn("sweep up to $500", line)
        self.assertIn("available so far this month", line)
        _, plain, html = report.build(d)
        self.assertIn(line, plain)
        # the HTML render escapes nothing in this sentence (digits,
        # dashes, spaces), so a substring check is a true parity check
        self.assertIn(line, html)

    def test_satisfied_month_says_swept(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=2), -PLAN,
                "TRANSFER TO SAVINGS", primary="TRANSFER_IN", account="sav")
        d = report.gather(self.conn, TODAY)
        from oikonome.web.todayview import build_context
        line = build_context(d)["day"]["sweep_line"]
        self.assertIn("swept $500 to savings this month", line)
        _, plain, html = report.build(d)
        self.assertIn(line, plain)
        self.assertIn(line, html)

    def test_no_sweep_goal_no_line(self):
        write_config(self.conn, budgeted_income_monthly=8000,
                     savings_goals=[_goal(mode="monthly")])
        d = report.gather(self.conn, TODAY)
        from oikonome.web.todayview import build_context
        self.assertIsNone(build_context(d)["day"]["sweep_line"])


class SweepModeApiTests(unittest.TestCase):
    """the mode round-trips through /api/settings; the default is
    unchanged for clients that never send it."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from .util import _ensure_db
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"sweep-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def _save(self, *goals):
        return self.client.post("/api/settings",
                                json={"savings_goals": list(goals)})

    def _goals(self):
        return self.client.get("/api/settings").json()["savings_goals"]

    def test_sweep_mode_round_trips(self):
        r = self._save({"name": "Rainy", "target": 0, "monthly_plan": 500,
                        "tokens": [], "start_balance": 0, "mode": "sweep"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self._goals()[0]["mode"], "sweep")

    def test_monthly_and_absent_normalize_to_no_mode_key(self):
        # old configs carry no key; a client sending 'monthly' must not
        # start writing one — byte-identical round trips either way
        for g in ({"name": "A", "target": 0, "monthly_plan": 100,
                   "tokens": [], "start_balance": 0, "mode": "monthly"},
                  {"name": "A", "target": 0, "monthly_plan": 100,
                   "tokens": [], "start_balance": 0}):
            self.assertEqual(self._save(g).status_code, 200)
            self.assertNotIn("mode", self._goals()[0])

    def test_unknown_mode_rejected(self):
        r = self._save({"name": "A", "target": 0, "monthly_plan": 100,
                        "tokens": [], "start_balance": 0,
                        "mode": "whenever"})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
