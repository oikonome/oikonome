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

from .util import TODAY, add_bill, add_txn, make_db, seed_accounts, write_config


def snapshot_months(conn, year: int, *months: int) -> None:
    """Freeze the CURRENT config as these months' budget snapshots — what
    the nightly job does for the current month as time passes them."""
    for m in months:
        budget.snapshot_month(conn, dt.date(year, m, 1))

# TODAY = Wed 2026-07-15 (util fixture). The straddle week under test:
# Mon 2026-06-29 → Sun 2026-07-05 (2 June days, 5 July days).
STRADDLE_MON = dt.date(2026, 6, 29)


class LensBase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=3000, other_monthly=3100)

    def tearDown(self):
        self.conn.close()


class TestWeekStraddle(LensBase):
    def test_snaps_to_monday(self):
        w = lenses.week_summary(self.conn, dt.date(2026, 7, 2), today=TODAY)
        self.assertEqual(w["start"], "2026-06-29")
        self.assertEqual(w["end"], "2026-07-05")

    def test_straddle_budget_sums_per_day_allowances(self):
        """A Mon–Sun week spanning two months = Σ each day's
        monthly ÷ days-in-month allowance."""
        w = lenses.week_summary(self.conn, STRADDLE_MON, today=TODAY)
        food = 2 * (3000 / 30) + 5 * (3000 / 31)     # June 30d, July 31d
        other = 2 * (3100 / 30) + 5 * (3100 / 31)
        self.assertAlmostEqual(w["buckets"]["food"]["week_budget"], food, 2)
        self.assertAlmostEqual(w["buckets"]["other"]["week_budget"], other, 2)
        self.assertAlmostEqual(w["week_budget"], food + other, 2)
        # completed week → expected == the full week budget
        self.assertEqual(w["status"], "final")
        self.assertAlmostEqual(w["expected"], w["week_budget"], 2)

    def test_straddle_actuals_only_count_week_rows(self):
        add_txn(self.conn, dt.date(2026, 6, 25), 500.0, "BEFORE WEEK")
        add_txn(self.conn, dt.date(2026, 6, 30), 40.0, "JUNE HALF")
        add_txn(self.conn, dt.date(2026, 7, 3), 60.0, "JULY HALF")
        add_txn(self.conn, dt.date(2026, 7, 6), 700.0, "AFTER WEEK")
        w = lenses.week_summary(self.conn, STRADDLE_MON, today=TODAY)
        self.assertEqual(w["actual"], 100.0)
        self.assertEqual([d["variable"] for d in w["days"]],
                         [0, 40.0, 0, 0, 60.0, 0, 0])
        self.assertEqual({t["payee"] for t in w["txns"]},
                         {"JUNE HALF", "JULY HALF"})

    def test_week_verdict_is_frozen_formula(self):
        """Verdict = variance vs max($50, 5% of expected) — the engine's
        rule at week scope."""
        w0 = lenses.week_summary(self.conn, STRADDLE_MON, today=TODAY)
        self.assertEqual(w0["verdict"], "UNDER BUDGET")   # $0 of ~$1.4k
        # spend exactly the week budget → ON BUDGET
        add_txn(self.conn, dt.date(2026, 7, 1), w0["week_budget"], "EXACT")
        w1 = lenses.week_summary(self.conn, STRADDLE_MON, today=TODAY)
        self.assertEqual(w1["verdict"], "ON BUDGET")
        self.assertAlmostEqual(
            w1["tolerance"], max(50.0, w1["expected"] * 0.05), 2)
        add_txn(self.conn, dt.date(2026, 7, 2),
                w1["tolerance"] + 1, "PUSH OVER")
        self.assertEqual(lenses.week_summary(
            self.conn, STRADDLE_MON, today=TODAY)["verdict"], "OVER BUDGET")

    def test_in_progress_week_prorates_expected(self):
        """TODAY is Wednesday of the 7/13 week → expected covers Mon–Wed
        only; the full budget still spans all 7 days."""
        w = lenses.week_summary(self.conn, dt.date(2026, 7, 13), today=TODAY)
        self.assertEqual(w["status"], "in_progress")
        day = (3000 + 3100) / 31
        self.assertAlmostEqual(w["expected"], 3 * day, 2)
        self.assertAlmostEqual(w["week_budget"], 7 * day, 2)

    def test_fixed_bills_stay_out_of_the_week_verdict(self):
        add_bill(self.conn, "Rent", 2000.0, next_due=dt.date(2026, 7, 1),
                 last_seen=dt.date(2026, 6, 1))
        add_txn(self.conn, dt.date(2026, 7, 1), 2000.0, "RENT")
        w = lenses.week_summary(self.conn, STRADDLE_MON, today=TODAY)
        self.assertEqual(w["actual"], 0.0)                # variable only
        self.assertEqual(w["buckets"]["fixed"]["actual"], 2000.0)
        rent = [t for t in w["txns"] if t["payee"] == "RENT"]
        self.assertTrue(rent and rent[0]["fixed"])
        self.assertEqual([d["fixed"] for d in w["days"]],
                         [0, 0, 2000.0, 0, 0, 0, 0])

    def test_envelope_overflow_is_counted_once_in_the_week(self):
        """A swipe over an envelope's cap is ONE purchase: the capped part
        is fixed, the rest variable, both on the day it happened. The week
        must sum to the purchase — not the full swipe as fixed plus the
        overflow again as variable."""
        add_bill(self.conn, "Corner Coffee", 100.0, bill_type="envelope",
                 frequency=None, interval=1)
        add_txn(self.conn, dt.date(2026, 7, 14), 150.0, "CORNER COFFEE",
                primary="FOOD_AND_DRINK")
        w = lenses.week_summary(self.conn, dt.date(2026, 7, 13), today=TODAY)
        self.assertEqual(w["buckets"]["fixed"]["actual"], 100.0)
        self.assertEqual(w["buckets"]["food"]["actual"], 50.0)
        self.assertEqual([d["fixed"] + d["variable"] for d in w["days"]],
                         [0, 150.0, 0, 0, 0, 0, 0])

    def test_custom_bucket_children_carry_week_scope(self):
        write_config(self.conn, food_monthly=3000, other_monthly=3100,
                     custom_buckets=[{"name": "Fun", "parent": "other",
                                      "monthly": 310, "categories":
                                      ["ENTERTAINMENT"], "merchants": []}])
        add_txn(self.conn, dt.date(2026, 7, 2), 80.0, "CINEMA",
                primary="ENTERTAINMENT")
        add_txn(self.conn, dt.date(2026, 7, 3), 50.0, "TARGET")
        w = lenses.week_summary(self.conn, STRADDLE_MON, today=TODAY)
        other = w["buckets"]["other"]
        self.assertEqual(other["actual"], 130.0)          # raw — frozen
        self.assertEqual(other["display_actual"], 50.0)   # after carve-out
        (fun,) = other["children"]
        self.assertEqual(fun["actual"], 80.0)
        self.assertAlmostEqual(fun["week_budget"],
                               2 * (310 / 30) + 5 * (310 / 31), 2)


class TestWeekBills(LensBase):
    """The week map's Bills row: planned = occurrences due Mon–Sun
    (engine expansion + matching, no new cadence rules) + envelope share;
    awaiting = due in the week (≤ today) and unposted."""

    def test_paid_bill_counts_planned_and_posted(self):
        add_bill(self.conn, "Rent", 2000.0, next_due=dt.date(2026, 7, 1),
                 last_seen=dt.date(2026, 6, 1))
        add_txn(self.conn, dt.date(2026, 7, 1), 2000.0, "RENT")
        w = lenses.week_summary(self.conn, STRADDLE_MON, today=TODAY)
        fx = w["buckets"]["fixed"]
        self.assertEqual(fx["actual"], 2000.0)        # posted (green)
        self.assertEqual(fx["week_budget"], 2000.0)   # planned this week
        self.assertEqual(fx["awaiting"], 0.0)

    def test_unpaid_due_bill_is_awaiting(self):
        add_bill(self.conn, "Water", 100.0, next_due=dt.date(2026, 7, 2),
                 last_seen=dt.date(2026, 6, 2))
        w = lenses.week_summary(self.conn, STRADDLE_MON, today=TODAY)
        fx = w["buckets"]["fixed"]
        self.assertEqual(fx["actual"], 0.0)
        self.assertEqual(fx["week_budget"], 100.0)
        self.assertEqual(fx["awaiting"], 100.0)       # due 7/2 ≤ today, unposted

    def test_bill_due_outside_week_is_excluded(self):
        add_bill(self.conn, "Gym", 50.0, next_due=dt.date(2026, 7, 20),
                 last_seen=dt.date(2026, 6, 20))
        w = lenses.week_summary(self.conn, STRADDLE_MON, today=TODAY)
        self.assertEqual(w["buckets"]["fixed"]["week_budget"], 0.0)
        self.assertEqual(w["buckets"]["fixed"]["awaiting"], 0.0)

    def test_due_later_this_week_is_planned_not_awaiting(self):
        # TODAY is Wed 7/15 of the 7/13 week; a bill due Fri 7/17 is in
        # the plan (light segment) but its due date hasn't arrived
        add_bill(self.conn, "Insurance", 300.0,
                 next_due=dt.date(2026, 7, 17), last_seen=dt.date(2026, 6, 17))
        w = lenses.week_summary(self.conn, dt.date(2026, 7, 13), today=TODAY)
        fx = w["buckets"]["fixed"]
        self.assertEqual(fx["week_budget"], 300.0)
        self.assertEqual(fx["awaiting"], 0.0)

    def test_envelope_share_drips_whats_left_of_the_pool(self):
        # envelopes have no due dates — the week carries each day's share
        # of the pool, at the FORECAST's envelope-drip rate: the current
        # month drips what's left over the days left (untouched pool,
        # TODAY=7/15 → 310 over the 16 days after today), other months
        # drip the plan rate (their pool is untouched or final)
        add_bill(self.conn, "Household Env", 310.0, bill_type="envelope")
        w = lenses.week_summary(self.conn, dt.date(2026, 7, 13), today=TODAY)
        self.assertAlmostEqual(w["buckets"]["fixed"]["week_budget"],
                               7 * (310 / 16), 2)
        # and a straddle week mixes both months' rates
        ws = lenses.week_summary(self.conn, STRADDLE_MON, today=TODAY)
        self.assertAlmostEqual(ws["buckets"]["fixed"]["week_budget"],
                               2 * (310 / 30) + 5 * (310 / 16), 2)

    def test_untouched_pool_in_another_month_keeps_the_plan_rate(self):
        # a week in a future month: the pool there is untouched, so the
        # drip is the classic monthly ÷ days-in-month share, unchanged
        add_bill(self.conn, "Household Env", 310.0, bill_type="envelope")
        w = lenses.week_summary(self.conn, dt.date(2026, 8, 10), today=TODAY)
        self.assertAlmostEqual(w["buckets"]["fixed"]["week_budget"],
                               7 * (310 / 31), 2)

    def test_exhausted_pool_drips_nothing(self):
        """A flat monthly ÷ days-in-month share keeps planning envelope
        money the engine says is already spent."""
        add_bill(self.conn, "Household Env", 310.0, bill_type="envelope")
        add_txn(self.conn, dt.date(2026, 7, 6), 310.0, "HOUSEHOLD ENV")
        w = lenses.week_summary(self.conn, dt.date(2026, 7, 13), today=TODAY)
        self.assertEqual(w["buckets"]["fixed"]["week_budget"], 0.0)

    def test_exhausted_annual_pool_drips_nothing(self):
        # an annual pool spent for the year would otherwise still drip
        # its monthly share on every day of the week
        add_bill(self.conn, "Domain Registrar", 120.0, bill_type="envelope",
                 frequency=None, interval=12)
        add_txn(self.conn, dt.date(2026, 3, 10), 120.0, "DOMAIN REGISTRAR")
        w = lenses.week_summary(self.conn, dt.date(2026, 7, 13), today=TODAY)
        self.assertEqual(w["buckets"]["fixed"]["week_budget"], 0.0)

    def test_annual_pool_drips_its_year_remainder(self):
        # $120/yr untouched, TODAY=7/15 → 120 over the 169 days left in
        # the year, not the $10/mo plan share over the month
        add_bill(self.conn, "Domain Registrar", 120.0, bill_type="envelope",
                 frequency=None, interval=12)
        w = lenses.week_summary(self.conn, dt.date(2026, 7, 13), today=TODAY)
        days_left_year = (dt.date(2026, 12, 31) - TODAY).days
        self.assertAlmostEqual(w["buckets"]["fixed"]["week_budget"],
                               7 * (120 / days_left_year), 2)


class TestMonthFinality(LensBase):
    def test_past_month_is_final_full_month(self):
        """A past month's card judges the FULL month (frac=1): expected =
        the whole budget, verdict final, spend after month end excluded."""
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
                           "payee": "RENT"}])

    def test_current_month_projects(self):
        add_txn(self.conn, TODAY.replace(day=5), 1500.0, "EARLY BURN")
        r = lenses.month_summary(self.conn, 2026, 7, today=TODAY)
        self.assertEqual(r["status"], "in_progress")
        p = r["projection"]
        # day 15: fourteen days have elapsed; today is not over
        self.assertAlmostEqual(p["pace_total"], 1500.0 / 14 * 31, 2)
        self.assertAlmostEqual(p["variance"], p["pace_total"] - 6100.0, 2)
        self.assertEqual(p["verdict"], "UNDER BUDGET")
        self.assertAlmostEqual(p["tolerance"], 6100.0 * 0.05, 2)

    def test_an_on_pace_morning_projects_on_budget_like_the_verdict(self):
        """Exactly fourteen days' worth of budget spent before day 15, and
        nothing yet today: the verdict says ON BUDGET, and the projection
        must agree — counting today as a finished $0 day read UNDER."""
        add_txn(self.conn, TODAY.replace(day=5), round(6100.0 * 14 / 31, 2),
                "PLAIN SPEND")
        r = lenses.month_summary(self.conn, 2026, 7, today=TODAY)
        p = r["projection"]
        self.assertAlmostEqual(p["pace_total"], 6100.0, 0)
        self.assertEqual(p["verdict"], "ON BUDGET")
        self.assertEqual(r["verdict"], "ON BUDGET")

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


class TestPlanSurplus(LensBase):
    """Plan surplus at lens scope: every lens payload carries the STANDING
    monthly plan surplus (income − avg bills − budgets − savings plan, the
    same month_status figure Today shows); the SPA scales it to the
    timeframe. No income configured → None (the row stays hidden)."""

    def test_none_without_income(self):
        self.assertIsNone(lenses.week_summary(
            self.conn, STRADDLE_MON, today=TODAY)["plan_surplus"])
        self.assertIsNone(lenses.month_summary(
            self.conn, 2026, 7, today=TODAY)["plan_surplus"])
        self.assertIsNone(lenses.year_summary(
            self.conn, 2026, today=TODAY)["plan_surplus"])

    def test_monthly_figure_rides_all_three_lenses(self):
        write_config(self.conn, food_monthly=3000, other_monthly=3100,
                     budgeted_income_monthly=10000)
        add_bill(self.conn, "Rent", 2000.0, next_due=dt.date(2026, 8, 1),
                 last_seen=dt.date(2026, 7, 1))
        # income 10000 − avg bills 2000 − 3000 − 3100 − savings 0 = 1900
        self.assertEqual(lenses.week_summary(
            self.conn, STRADDLE_MON, today=TODAY)["plan_surplus"], 1900.0)
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
        Year only. week_summary stays an engine function because the
        weekly email digest still renders from it."""
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
