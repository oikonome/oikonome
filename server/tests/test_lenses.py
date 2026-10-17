"""Timeframe lenses (web/lenses.py + /api/lens/*).

The frozen contract: lenses are pure VIEWS over budget.month_status — the
week budget is the sum of per-day allowances (month_budget ÷ days-in-month
for each day's OWN month, exact across month straddles), past months are
FINAL report cards (engine verdict at month end, bills matched), and the
year grid composes 12 month cells with the current month projected.
"""

import datetime as dt
import unittest
import uuid

from oikonome.engine import budget
from oikonome.web import lenses

from .util import (TODAY, add_bill, add_ledger_history, add_txn, make_db,
                   seed_accounts, write_config)


def snapshot_months(conn, year: int, *months: int) -> None:
    """Freeze the CURRENT config as these months' budget snapshots — what
    the nightly job does for the current month as time passes them."""
    for m in months:
        budget.snapshot_month(conn, dt.date(year, m, 1))

# TODAY = Wed 2026-07-15 (util fixture). The straddle week under test:
# Mon 2026-06-29 → Sun 2026-07-05 (2 June days, 5 July days).


class LensBase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=3000, other_monthly=3100)

    def tearDown(self):
        self.conn.close()


class TestMonthFinality(LensBase):
    def test_past_month_is_final_full_month(self):
        """A past month's card judges the FULL month (frac=1): expected =
        the whole budget, verdict final, spend after month end excluded."""
        # an established household: the ledger predates June
        add_ledger_history(self.conn, dt.date(2026, 6, 1))
        add_txn(self.conn, dt.date(2026, 6, 10), 2000.0, "JUNE SPEND")
        add_txn(self.conn, dt.date(2026, 7, 2), 9000.0, "JULY SPEND")
        r = lenses.month_summary(self.conn, 2026, 6, today=TODAY)
        self.assertEqual(r["status"], "final")
        self.assertEqual(r["as_of"], "2026-06-30")
        self.assertIsNone(r["projection"])
        self.assertEqual(r["variable_actual"], 2000.0)
        self.assertEqual(r["variable_budget"], 6100.0)
        # margin against the FULL budget → deeply under
        self.assertAlmostEqual(r["variance"], 2000.0 - 6100.0, 2)
        self.assertEqual(r["verdict"], "UNDER BUDGET")

    def test_past_month_matches_bills(self):
        """The report card decomposes past months with the live schedule:
        a bill paid back then reads as FIXED, not variable overage."""
        add_bill(self.conn, "Rent", 2000.0, next_due=dt.date(2026, 7, 1),
                 last_seen=dt.date(2026, 6, 1))
        add_txn(self.conn, dt.date(2026, 6, 1), 2000.0, "RENT")
        r = lenses.month_summary(self.conn, 2026, 6, today=TODAY)
        self.assertEqual(r["variable_actual"], 0.0)
        self.assertEqual(r["bills"]["posted"], 2000.0)
        self.assertEqual(r["bills"]["paid"],
                         [{"date": "2026-06-01", "amount": 2000.0,
                           "payee": "RENT",
                           "category": "GENERAL MERCHANDISE",
                           "detailed": None}])

    def test_current_month_projects(self):
        add_txn(self.conn, TODAY.replace(day=5), 1500.0, "EARLY BURN")
        r = lenses.month_summary(self.conn, 2026, 7, today=TODAY)
        self.assertEqual(r["status"], "in_progress")
        p = r["projection"]
        # day 15, ledger from the 5th: ten days have finished since it
        # began (the days before it are no data; today is not over), and
        # the month runs 27 days from it — judged against the budget's
        # share of those 27 days
        spendable = 6100.0 * 27 / 31
        self.assertAlmostEqual(p["pace_total"], 1500.0 / 10 * 27, 2)
        self.assertAlmostEqual(p["variance"], p["pace_total"] - spendable, 2)
        self.assertEqual(p["verdict"], "UNDER BUDGET")
        self.assertAlmostEqual(p["tolerance"], spendable * 0.05, 2)

    def test_an_on_pace_morning_projects_on_budget_like_the_verdict(self):
        """Exactly fourteen days' worth of budget spent before day 15, and
        nothing yet today: the verdict says ON BUDGET, and the projection
        must agree — counting today as a finished $0 day read UNDER."""
        add_ledger_history(self.conn, TODAY)
        add_txn(self.conn, TODAY.replace(day=5), round(6100.0 * 14 / 31, 2),
                "PLAIN SPEND")
        r = lenses.month_summary(self.conn, 2026, 7, today=TODAY)
        p = r["projection"]
        self.assertAlmostEqual(p["pace_total"], 6100.0, 0)
        self.assertEqual(p["verdict"], "ON BUDGET")
        self.assertEqual(r["verdict"], "ON BUDGET")

    def test_an_on_allowance_household_projects_on_budget(self):
        """Spending exactly the daily allowance every day, today included:
        the pace runs over the days through yesterday, so today's spend
        stays out of the numerator too, the horizon the forecast uses.
        Counted in, day 2 projected double the budget and day 11 ten per
        cent over it."""
        allowance = round(6100.0 / 31, 2)
        for day in (2, 11):
            with self.subTest(day=day):
                self.conn.close()
                self.conn = make_db()
                write_config(self.conn, food_monthly=3000, other_monthly=3100)
                for d in range(1, day + 1):
                    add_txn(self.conn, dt.date(2026, 7, d), allowance,
                            f"PLAIN SPEND {d}")
                today = dt.date(2026, 7, day)
                r = lenses.month_summary(self.conn, 2026, 7, today=today)
                p = r["projection"]
                self.assertAlmostEqual(p["pace_total"], 6100.0, delta=1.0)
                self.assertEqual(p["verdict"], "ON BUDGET")
                y = lenses.year_summary(self.conn, 2026, today=today)
                self.assertEqual(y["cells"][6]["verdict"], "ON BUDGET")

    def test_day_one_projects_the_plan(self):
        """Nothing has elapsed on the 1st, so there is no pace to project."""
        add_txn(self.conn, dt.date(2026, 7, 1), 400.0, "EARLY BURN")
        r = lenses.month_summary(self.conn, 2026, 7,
                                 today=dt.date(2026, 7, 1))
        self.assertAlmostEqual(r["projection"]["pace_total"], 6100.0, 2)

    def test_future_month_is_plan_only(self):
        r = lenses.month_summary(self.conn, 2026, 9, today=TODAY)
        self.assertEqual(r["status"], "future")
        self.assertEqual(r["variable_actual"], 0.0)
        self.assertEqual(r["variable_budget"], 6100.0)

    def test_by_category_and_biggest(self):
        add_txn(self.conn, dt.date(2026, 6, 3), 300.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2026, 6, 9), 45.0, "AMZN A",
                override="Amazon - Household")
        add_txn(self.conn, dt.date(2026, 6, 11), 25.0, "AMZN B",
                override="Amazon - Food & Drink")
        r = lenses.month_summary(self.conn, 2026, 6, today=TODAY)
        cats = dict((c, a) for c, a, _k in r["by_category"])
        self.assertEqual(cats["Amazon"], 70.0)            # clustered
        self.assertEqual(cats["FOOD AND DRINK"], 300.0)
        self.assertEqual(r["amazon_total"], 70.0)
        self.assertEqual(r["biggest"][0]["payee"], "SAFEWAY")


class TestYearGrid(LensBase):
    def test_grid_final_projected_future(self):
        """TODAY = 2026-07-15: months 1–6 final (each has a budget
        snapshot), 7 projected, 8–12 blank."""
        add_ledger_history(self.conn, dt.date(2026, 1, 1))
        snapshot_months(self.conn, 2026, 1, 2, 3, 4, 5, 6)
        # tolerance at month end = max($50, 5% × $6,100) = $305 → clear it
        add_txn(self.conn, dt.date(2026, 3, 10), 6600.0, "MARCH BLOWOUT")
        y = lenses.year_summary(self.conn, 2026, today=TODAY)
        self.assertEqual(len(y["cells"]), 12)
        self.assertEqual([c["status"] for c in y["cells"]],
                         ["final"] * 6 + ["projected"] + ["future"] * 5)
        mar, jul, dec = y["cells"][2], y["cells"][6], y["cells"][11]
        self.assertEqual(mar["verdict"], "OVER BUDGET")
        self.assertAlmostEqual(mar["variance"], 6600.0 - 6100.0, 2)
        self.assertEqual(jul["verdict"], "UNDER BUDGET")  # $0 pace
        self.assertIsNone(dec["verdict"])

    def test_annual_totals_and_category_yoy(self):
        # income: money INTO checking (Plaid sign: negative = in)
        add_txn(self.conn, dt.date(2026, 2, 10), -5000.0, "PAYROLL",
                account="chk", primary="INCOME")
        add_txn(self.conn, dt.date(2025, 6, 10), -4000.0, "PAYROLL",
                account="chk", primary="INCOME")
        add_txn(self.conn, dt.date(2026, 2, 12), 900.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2025, 6, 12), 600.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        y = lenses.year_summary(self.conn, 2026, today=TODAY)
        self.assertEqual(y["totals"],
                         {"income": 5000.0, "spend": 900.0,
                          "saved": 4100.0, "rate": 82.0})
        self.assertEqual(y["prev_totals"]["spend"], 600.0)
        self.assertEqual(y["categories"],
                         [["FOOD AND DRINK", 900.0, 600.0, "FOOD_AND_DRINK"]])
        self.assertEqual(y["first_year"], 2025)

    def test_buckets_annual_covers_elapsed_months_only(self):
        # the year money map's rows — per-bucket sums over the elapsed
        # BUDGETED months (7 with TODAY = 2026-07-15 and months 1–6
        # snapshotted), PlanBucket-shaped
        snapshot_months(self.conn, 2026, 1, 2, 3, 4, 5, 6)
        add_txn(self.conn, dt.date(2026, 2, 12), 900.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        y = lenses.year_summary(self.conn, 2026, today=TODAY)
        ba = y["buckets_annual"]
        self.assertEqual(set(ba), {"food", "other", "fixed"})
        for k in ba:
            self.assertEqual(set(ba[k]),
                             {"actual", "expected", "month_budget"})
        # LensBase config: food 3000/mo, other 3100/mo × 7 elapsed months
        self.assertAlmostEqual(ba["food"]["month_budget"], 3000.0 * 7, 2)
        self.assertAlmostEqual(ba["other"]["month_budget"], 3100.0 * 7, 2)
        self.assertAlmostEqual(ba["food"]["actual"], 900.0, 2)
        self.assertAlmostEqual(ba["other"]["actual"], 0.0, 2)


class TestYearMoneyMapLabelAndItsListingAgree(LensBase):
    """The year money map shows a whole bucket (carve-outs included, since
    it draws no child rows), so the listing its label opens must count the
    same money — carve-out purchases listed, the same total."""

    def test_parent_bucket_listing_counts_what_the_year_label_shows(self):
        write_config(self.conn, food_monthly=3000, other_monthly=3100,
                     custom_buckets=[{"name": "Fun", "parent": "other",
                                      "monthly": 310, "categories":
                                      ["ENTERTAINMENT"], "merchants": []}])
        add_txn(self.conn, dt.date(2026, 7, 2), 80.0, "CINEMA",
                primary="ENTERTAINMENT", txn_id="fun1")
        add_txn(self.conn, dt.date(2026, 7, 3), 50.0, "TARGET",
                txn_id="oth1")
        y = lenses.year_summary(self.conn, 2026, today=TODAY)
        led = lenses.year_bucket_ledger(self.conn, "other", 2026, TODAY)
        self.assertEqual(led["amount"], y["buckets_annual"]["other"]["actual"])
        self.assertEqual(led["amount"], 130.0)
        self.assertEqual(set(led["txn_ids"]), {"fun1", "oth1"})


class TestBudgetSnapshots(LensBase):
    """Closed months are judged against the budget frozen FOR THEM — the
    nightly job upserts the current month's config into budget_snapshots,
    so the last write before the month turns is the month's own budget and
    later edits can't rewrite history. A closed month with no snapshot
    predates budgeting: the year grid shows its net cash flow (income −
    spending) instead of fabricating a verdict against today's numbers."""

    def test_snapshot_freezes_the_month_against_later_edits(self):
        snapshot_months(self.conn, 2026, 6)
        write_config(self.conn, food_monthly=100, other_monthly=100)
        r = lenses.month_summary(self.conn, 2026, 6, today=TODAY)
        self.assertEqual(r["variable_budget"], 6100.0)   # June's own budget
        self.assertEqual(r["budget_source"], "snapshot")
        cur = lenses.month_summary(self.conn, 2026, 7, today=TODAY)
        self.assertEqual(cur["variable_budget"], 200.0)  # live config
        self.assertEqual(cur["budget_source"], "live")

    def test_snapshot_rides_the_year_grid(self):
        snapshot_months(self.conn, 2026, 6)
        write_config(self.conn, food_monthly=100, other_monthly=100)
        y = lenses.year_summary(self.conn, 2026, today=TODAY)
        june = y["cells"][5]
        self.assertEqual((june["status"], june["variable_budget"]),
                         ("final", 6100.0))

    def test_upsert_covers_only_its_own_month(self):
        snapshot_months(self.conn, 2026, 6)
        write_config(self.conn, food_monthly=4000, other_monthly=100)
        snapshot_months(self.conn, 2026, 7)
        self.assertEqual(
            budget.snapshot_config(self.conn, 2026, 6)["food_monthly"], 3000)
        self.assertEqual(
            budget.snapshot_config(self.conn, 2026, 7)["food_monthly"], 4000)
        # re-capturing the same month replaces it (the nightly upsert)
        snapshot_months(self.conn, 2026, 7)
        self.assertEqual(budget.snapshot_months(self.conn, 2026), {6, 7})

    def test_unset_budget_never_snapshots(self):
        write_config(self.conn, food_monthly=0, other_monthly=0)
        self.assertFalse(
            budget.snapshot_month(self.conn, dt.date(2026, 6, 1)))
        self.assertIsNone(budget.snapshot_config(self.conn, 2026, 6))

    def test_no_snapshot_month_is_net_in_year_view(self):
        add_txn(self.conn, dt.date(2026, 6, 10), -5000.0, "PAYROLL",
                account="chk", primary="INCOME")
        add_txn(self.conn, dt.date(2026, 6, 12), 900.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        y = lenses.year_summary(self.conn, 2026, today=TODAY)
        june = y["cells"][5]
        self.assertEqual(june["status"], "net")
        self.assertIsNone(june["verdict"])
        self.assertEqual(june["income"], 5000.0)
        self.assertEqual(june["spend"], 900.0)
        self.assertEqual(june["net"], 4100.0)
        # no plan existed for net months — they add nothing to the map
        self.assertEqual(y["buckets_annual"]["food"]["month_budget"], 3000.0)

    def test_net_month_without_bank_coverage_has_unknown_income(self):
        # card spend only — no depository rows, so income is unknowable
        add_txn(self.conn, dt.date(2026, 6, 12), 900.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        june = lenses.year_summary(self.conn, 2026, today=TODAY)["cells"][5]
        self.assertEqual(june["status"], "net")
        self.assertIsNone(june["income"])
        self.assertIsNone(june["net"])
        self.assertEqual(june["spend"], 900.0)


class TestYearLensHonoursExcludedAccounts(LensBase):
    """An account the household excluded from the budget is out of every
    Year lens figure, not only the income: spend, saved and savings rate,
    the category year-over-year rows, a no-snapshot month's net cell and a
    category label's drill-down all drop its rows, so the year agrees with
    the Cash Flow report and with its own month cells."""

    def setUp(self):
        super().setUp()
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES "
            "('side','it1','Side Card','credit','credit card',0)")
        write_config(self.conn, food_monthly=3000, other_monthly=3100,
                     excluded_accounts=["side"])
        # checking feeds the ledger from January, so the whole year's
        # income is known and the totals are numbers
        add_txn(self.conn, dt.date(2026, 1, 5), 200.0, "TO SAVINGS",
                account="chk", primary="TRANSFER_OUT")
        add_txn(self.conn, dt.date(2026, 2, 10), -5000.0, "PAYROLL",
                account="chk", primary="INCOME")
        add_txn(self.conn, dt.date(2026, 2, 12), 900.0, "SAFEWAY",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, dt.date(2026, 2, 14), 800.0, "BOATYARD",
                account="side", primary="GENERAL_SERVICES")
        add_txn(self.conn, dt.date(2026, 2, 15), 70.0, "TEAROOM",
                account="side", primary="FOOD_AND_DRINK")

    def test_totals_and_categories_leave_the_excluded_account_out(self):
        y = lenses.year_summary(self.conn, 2026, today=TODAY)
        self.assertEqual(y["totals"],
                         {"income": 5000.0, "spend": 900.0,
                          "saved": 4100.0, "rate": 82.0})
        self.assertEqual(y["categories"],
                         [["FOOD AND DRINK", 900.0, 0.0, "FOOD_AND_DRINK"]])

    def test_net_cell_leaves_the_excluded_account_out(self):
        feb = lenses.year_summary(self.conn, 2026, today=TODAY)["cells"][1]
        self.assertEqual(feb["status"], "net")
        self.assertEqual((feb["income"], feb["spend"], feb["net"]),
                         (5000.0, 900.0, 4100.0))

    def test_category_drill_down_leaves_the_excluded_account_out(self):
        led = lenses.year_category_ledger(self.conn, "FOOD_AND_DRINK", 2026)
        self.assertEqual(led["amount"], 900.0)
        self.assertEqual(len(led["txn_ids"]), 1)


class TestPlanSurplus(LensBase):
    """Plan surplus at lens scope: every lens payload carries the STANDING
    monthly plan surplus (income − avg bills − budgets − savings plan, the
    same month_status figure Today shows); the SPA scales it to the
    timeframe. No income configured → None (the row stays hidden)."""

    def test_none_without_income(self):
        self.assertIsNone(lenses.month_summary(
            self.conn, 2026, 7, today=TODAY)["plan_surplus"])
        self.assertIsNone(lenses.year_summary(
            self.conn, 2026, today=TODAY)["plan_surplus"])

    def test_monthly_figure_rides_both_lenses(self):
        write_config(self.conn, food_monthly=3000, other_monthly=3100,
                     budgeted_income_monthly=10000)
        add_bill(self.conn, "Rent", 2000.0, next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        # income 10000 − avg bills 2000 − 3000 − 3100 − savings 0 = 1900
        self.assertEqual(lenses.month_summary(
            self.conn, 2026, 7, today=TODAY)["plan_surplus"], 1900.0)
        self.assertEqual(lenses.year_summary(
            self.conn, 2026, today=TODAY)["plan_surplus"], 1900.0)
        # a past year reports the figure from its budgeted (snapshotted)
        # months; with no snapshot at all the year has no plan to report
        self.assertIsNone(lenses.year_summary(
            self.conn, 2025, today=TODAY)["plan_surplus"])
        snapshot_months(self.conn, 2025, 12)
        self.assertEqual(lenses.year_summary(
            self.conn, 2025, today=TODAY)["plan_surplus"], 1900.0)

    def test_future_year_has_no_elapsed_months(self):
        write_config(self.conn, food_monthly=3000, other_monthly=3100,
                     budgeted_income_monthly=10000)
        self.assertIsNone(lenses.year_summary(
            self.conn, 2027, today=TODAY)["plan_surplus"])


class LensApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        from .util import _ensure_db
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from fastapi.testclient import TestClient

        from oikonome.db import tenancy
        from oikonome.web.app import app
        cls.client = TestClient(app)
        r = cls.client.post("/api/signup",
                            data={"email": f"lens-{uuid.uuid4().hex[:8]}@x.dev",
                                  "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=3000, other_monthly=3100)
            add_txn(conn, dt.date.today(), 42.5, "SAFEWAY",
                    primary="FOOD_AND_DRINK")
        finally:
            conn.close()

    def test_requires_auth(self):
        from fastapi.testclient import TestClient

        from oikonome.web.app import app
        anon = TestClient(app)
        for path in ("/api/lens/month", "/api/lens/year"):
            self.assertEqual(anon.get(path).status_code, 401, path)

    def test_the_week_lens_endpoint_is_gone(self):
        """There is no Week VIEW — Today | Month |
        Year only; the Week lens and the weekly email it fed were
        retired."""
        self.assertEqual(self.client.get("/api/lens/week").status_code, 404)

    def test_month_and_year_shapes(self):
        today = dt.date.today()
        r = self.client.get("/api/lens/month",
                            params={"y": today.year, "m": today.month}).json()
        self.assertEqual((r["y"], r["m"], r["status"]),
                         (today.year, today.month, "in_progress"))
        for key in ("verdict", "buckets", "bills", "by_category", "biggest",
                    "income", "projection", "spend_total"):
            self.assertIn(key, r)
        self.assertEqual(self.client.get(
            "/api/lens/month", params={"y": today.year, "m": 13}).status_code,
            400)
        ry = self.client.get("/api/lens/year").json()
        self.assertEqual(ry["y"], today.year)
        self.assertEqual(len(ry["cells"]), 12)
        self.assertIn("totals", ry)
        self.assertIn("categories", ry)
