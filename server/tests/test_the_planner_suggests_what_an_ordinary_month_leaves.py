"""The planner's suggested monthly savings is the server's, measured on the
income an ordinary month brings (specs/honest-figures.md, "The planner's
suggested savings is an ordinary month's").

The plan's income for a household with tracked paychecks is this month's:
what landed plus what is still due, a one-off deposit included. The planner
filled "Save / month" with what that left, and saving the plan made one
month's windfall a standing monthly commitment.

The household here is paid $4,000 on the 1st, pays $1,200 rent and budgets
$600 for food and $800 for everything else: an ordinary month leaves
$1,400. This month a $1,700 refund landed too.
"""

import datetime as dt
import pathlib
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget, forecast
from oikonome.web import report
from oikonome.web.schemas.today import SuggestedBudgets, TodayFull

from .util import (_ensure_db, add_bill, add_txn, make_db, seed_accounts,
                   write_config)

TODAY = dt.date(2026, 7, 15)
_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _household(conn, **cfg) -> None:
    write_config(conn, food_monthly=600, other_monthly=800, **cfg)
    d = dt.date(2025, 5, 1)
    while d < TODAY:
        add_txn(conn, d, -4000.0, "LANTERN WORKS PAYROLL",
                primary="INCOME", account="chk")
        add_txn(conn, d.replace(day=3), 1200.0, "MAPLE COURT RENT",
                primary="RENT_AND_UTILITIES", account="chk")
        d = (d + dt.timedelta(days=32)).replace(day=1)
    add_bill(conn, "Lantern Works Payroll", 4000.0, income=True,
             next_due=dt.date(2026, 8, 1), last_seen=dt.date(2026, 7, 1))
    add_bill(conn, "Maple Court Rent", 1200.0,
             next_due=dt.date(2026, 8, 3), last_seen=dt.date(2026, 7, 3))


def _refund(conn) -> None:
    add_txn(conn, dt.date(2026, 7, 9), -1700.0, "STATE TAX REFUND",
            primary="INCOME", account="chk")


class ASavedPlan(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _suggested(self):
        st = budget.month_status(self.conn, TODAY)
        return st, forecast.plan_savings_suggestion(self.conn, st, TODAY)

    def test_a_one_off_deposit_does_not_move_the_suggestion(self):
        _household(self.conn)
        st, before = self._suggested()
        self.assertEqual(st["plan_surplus"], 1400.0)
        self.assertEqual(before, 1400)
        _refund(self.conn)
        st, after = self._suggested()
        # the month itself has the refund in its income and its excess
        self.assertEqual(st["income"], 5700.0)
        self.assertEqual(st["plan_surplus"], 3100.0)
        self.assertEqual(after, 1400)

    def test_the_plans_own_goal_is_put_back_and_another_goal_is_not(self):
        _household(self.conn, savings_goals=[
            {"name": "Savings", "target": 0, "monthly_plan": 500},
            {"name": "Trip", "target": 2500, "monthly_plan": 200}])
        _refund(self.conn)
        st, suggested = self._suggested()
        self.assertEqual(st["plan_surplus"], 2400.0)
        self.assertEqual(suggested, 1200)

    def test_a_plan_that_spends_it_all_suggests_zero_never_less(self):
        _household(self.conn)
        write_config(self.conn, food_monthly=1600, other_monthly=1900)
        self.assertEqual(self._suggested()[1], 0)

    def test_no_income_is_no_suggestion(self):
        write_config(self.conn, food_monthly=600, other_monthly=800)
        st, suggested = self._suggested()
        self.assertIsNone(st["plan_surplus"])
        self.assertIsNone(suggested)

    def test_a_typed_income_is_standing_as_it_is(self):
        write_config(self.conn, food_monthly=600, other_monthly=800,
                     budgeted_income_monthly=2600)
        st, suggested = self._suggested()
        self.assertEqual(st["income_source"], "typed")
        self.assertEqual(suggested, 1200)

    def test_today_sends_it_live_and_not_for_a_past_day(self):
        _household(self.conn)
        _refund(self.conn)
        live = report.gather(self.conn, TODAY)
        self.assertEqual(live["savings_suggested"], 1400)
        past = report.gather(self.conn, dt.date(2026, 7, 10), live=False)
        self.assertIsNone(past["savings_suggested"])
        self.assertIn("savings_suggested", TodayFull.model_fields)


class AFirstRun(unittest.TestCase):
    """Nothing saved: the planner is seeded from history, so the suggestion
    counts history's own budgets."""

    def setUp(self):
        self.conn = make_db()
        _household(self.conn)
        write_config(self.conn, food_monthly=0, other_monthly=0)
        d = dt.date(2025, 5, 1)
        while d < TODAY:
            add_txn(self.conn, d.replace(day=12), 500.0, "Corner Grocer",
                    primary="FOOD_AND_DRINK", account="chk")
            add_txn(self.conn, d.replace(day=18), 700.0, "Hardware Hub",
                    primary="GENERAL_MERCHANDISE", account="chk")
            d = (d + dt.timedelta(days=32)).replace(day=1)

    def tearDown(self):
        self.conn.close()

    def test_history_suggests_income_less_bills_and_its_own_budgets(self):
        out = budget.suggest_budgets(self.conn, TODAY)
        sug = out["suggestions"]
        self.assertEqual(
            (sug["income_monthly"], out["avg_bills_monthly"],
             sug["food_monthly"], sug["other_monthly"]),
            (4000.0, 1200.0, 500.0, 700.0))
        self.assertEqual(sug["savings_monthly"], 1600)
        _refund(self.conn)
        again = budget.suggest_budgets(self.conn, TODAY)["suggestions"]
        self.assertEqual(again["savings_monthly"], 1600)
        self.assertEqual(
            SuggestedBudgets.model_validate(again).savings_monthly, 1600)

    def test_history_with_no_income_suggests_none(self):
        self.conn.execute("DELETE FROM bills WHERE type = 'INCOME'")
        sug = budget.suggest_budgets(self.conn, TODAY)["suggestions"]
        self.assertNotIn("savings_monthly", sug)

    def test_with_no_income_history_the_typed_take_home_is_measured(self):
        """A household whose history names no income types its take-home
        on Budget: the suggestion is that figure less the bills, history's
        budgets and the other goals. The typed figure is not history's, so
        it is not sent back as the suggested income."""
        self.conn.execute("DELETE FROM bills WHERE type = 'INCOME'")
        sug = budget.suggest_budgets(self.conn, TODAY,
                                     typed_income=3000.0)["suggestions"]
        self.assertEqual(sug["savings_monthly"], 3000 - 1200 - 500 - 700)
        self.assertNotIn("income_monthly", sug)
        # a typed take-home that the plan spends whole suggests $0
        low = budget.suggest_budgets(self.conn, TODAY,
                                     typed_income=1000.0)["suggestions"]
        self.assertEqual(low["savings_monthly"], 0)

    def test_a_blank_or_unusable_take_home_is_no_suggestion(self):
        self.conn.execute("DELETE FROM bills WHERE type = 'INCOME'")
        for typed in (None, 0.0, -50.0, float("nan"), float("inf")):
            sug = budget.suggest_budgets(self.conn, TODAY,
                                         typed_income=typed)["suggestions"]
            self.assertNotIn("savings_monthly", sug, typed)

    def test_with_income_history_a_typed_figure_changes_nothing(self):
        sug = budget.suggest_budgets(self.conn, TODAY,
                                     typed_income=9000.0)["suggestions"]
        self.assertEqual((sug["income_monthly"], sug["savings_monthly"]),
                         (4000.0, 1600))


def _client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    return TestClient(appmod.app)


class TheSuggestionDoorTakesTheTypedTakeHome(unittest.TestCase):
    """GET /api/budgets/suggest?income=: the planner sends the box as
    typed; the server reads it by its own separator rules."""

    def setUp(self):
        self.client = _client()
        self.client.post("/api/signup", data={
            "email": f"typed-take-home-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = self.client.get("/api/me").json()["tenant_id"]
        self.conn = tenancy.tenant_connect(tid)
        self.addCleanup(self.conn.close)
        seed_accounts(self.conn)
        today = dt.date.today()
        d = (today.replace(day=1) - dt.timedelta(days=200)).replace(day=1)
        while d < today.replace(day=1):
            add_txn(self.conn, d.replace(day=5), 900.0, "OAKRIDGE LEASING",
                    primary="RENT_AND_UTILITIES", account="chk")
            add_txn(self.conn, d.replace(day=12), 400.0, "Corner Grocer",
                    primary="FOOD_AND_DRINK", account="chk")
            d = (d + dt.timedelta(days=32)).replace(day=1)
        nxt = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=5)
        add_bill(self.conn, "Oakridge Leasing", 900.0, next_due=nxt,
                 last_seen=nxt - dt.timedelta(days=31))
        self.today = today

    def _savings(self, **params):
        r = self.client.get("/api/budgets/suggest", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["suggestions"].get("savings_monthly")

    def test_the_typed_take_home_is_read_as_typed(self):
        want = budget.suggest_budgets(
            self.conn, self.today,
            typed_income=2750.0)["suggestions"]["savings_monthly"]
        self.assertIsNotNone(want)
        self.assertEqual(self._savings(income="2,750"), want)
        self.assertEqual(self._savings(income="2750.00"), want)

    def test_nothing_or_nonsense_typed_is_no_suggestion(self):
        self.assertIsNone(self._savings())
        self.assertIsNone(self._savings(income=""))
        self.assertIsNone(self._savings(income="lots"))
        self.assertIsNone(self._savings(income="1e400"))


class NeitherClientWorksItOut(unittest.TestCase):
    def test_the_box_shows_the_servers_figure(self):
        for rel in ("webapp/src/components/BudgetPlanner.tsx",
                    "mobile/src/app/budget.tsx"):
            src = (_ROOT / rel).read_text()
            self.assertIn("suggestions.savings_monthly", src, rel)
            self.assertIn("savings_suggested", src, rel)
            # the excess with the goal at $0 was the client's own figure
            self.assertNotIn("preview(0)", src, rel)
            self.assertNotIn("What's left, automatically", src, rel)


if __name__ == "__main__":
    unittest.main()
