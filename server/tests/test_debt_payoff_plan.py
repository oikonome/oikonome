"""The debt payoff planner: every card and loan on one schedule.

What breaks if these fail: the order the two methods promise (avalanche =
highest rate first, snowball = smallest balance first), the rolling
minimum that makes a plan accelerate, the debt-free date and the interest
the page quotes, the reading of the aggregator's rate / minimum fields,
the estimate for a debt the bank told us nothing about, and the plan
document's validation on the way in.
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget, debt
from oikonome.engine.compat import jsonb

from .util import TODAY, _ensure_db, make_db, seed_accounts, write_config


def _d(id, balance, apr, mn, **kw):
    return {"id": id, "name": id, "kind": kw.pop("kind", "card"),
            "balance": balance, "apr": apr, "min_payment": mn,
            "skip": kw.pop("skip", False), **kw}


class OrderAndRolloverTests(unittest.TestCase):
    def setUp(self):
        self.debts = [_d("big", 6000, 22.0, 120),
                      _d("small", 800, 9.0, 25),
                      _d("mid", 2500, 15.0, 60)]

    def test_avalanche_pays_the_highest_rate_first(self):
        r = debt.simulate(self.debts, method="avalanche", extra=300,
                          start=TODAY)
        order = [x["id"] for x in sorted(r["debts"], key=lambda x: x["order"])]
        self.assertEqual(order, ["big", "mid", "small"])
        # and the highest rate really clears before the lowest
        by = {x["id"]: x["paid_off"] for x in r["debts"]}
        self.assertLess(by["big"], by["small"])

    def test_snowball_pays_the_smallest_balance_first(self):
        r = debt.simulate(self.debts, method="snowball", extra=300,
                          start=TODAY)
        order = [x["id"] for x in sorted(r["debts"], key=lambda x: x["order"])]
        self.assertEqual(order, ["small", "mid", "big"])
        by = {x["id"]: x["paid_off"] for x in r["debts"]}
        self.assertLess(by["small"], by["big"])

    def test_avalanche_never_costs_more_interest_than_snowball(self):
        a = debt.simulate(self.debts, method="avalanche", extra=300,
                          start=TODAY)
        s = debt.simulate(self.debts, method="snowball", extra=300,
                          start=TODAY)
        self.assertLessEqual(a["total_interest"], s["total_interest"])
        # the same money in, so the two schedules end within a month
        self.assertLessEqual(abs((a["months"] or 0) - (s["months"] or 0)), 1)

    def test_a_retired_debts_minimum_rolls_to_the_next(self):
        rolled = debt.simulate(self.debts, method="snowball", extra=0,
                               start=TODAY)
        flat = debt.simulate(self.debts, method="snowball", extra=0,
                             start=TODAY, rollover=False)
        self.assertLess(rolled["months"], flat["months"])
        self.assertLess(rolled["total_interest"], flat["total_interest"])
        # the monthly outlay the page quotes: every minimum plus the extra
        self.assertEqual(rolled["monthly"], 205.0)
        self.assertEqual(flat["monthly"], 205.0)

    def test_extra_money_shortens_the_plan(self):
        none = debt.simulate(self.debts, method="avalanche", extra=0,
                             start=TODAY)
        some = debt.simulate(self.debts, method="avalanche", extra=500,
                             start=TODAY)
        self.assertLess(some["months"], none["months"])
        self.assertLess(some["total_interest"], none["total_interest"])
        self.assertEqual(some["monthly"], 705.0)

    def test_a_skipped_debt_is_left_out(self):
        debts = self.debts + [_d("skipme", 99_000, 30.0, 100, skip=True)]
        r = debt.simulate(debts, method="avalanche", extra=300, start=TODAY)
        self.assertEqual({x["id"] for x in r["debts"]},
                         {"big", "small", "mid"})
        self.assertEqual(r["starting"], 9300.0)


class ArithmeticTests(unittest.TestCase):
    def test_one_month_of_interest_then_the_payment(self):
        # $1,200 at 12%: one month accrues $12, and a minimum that covers
        # the whole thing clears it in that first month
        r = debt.simulate([_d("a", 1200, 12.0, 5000)], start=TODAY)
        self.assertEqual(r["months"], 1)
        self.assertEqual(r["total_interest"], 12.0)
        self.assertEqual(r["total_paid"], 1212.0)
        self.assertEqual(r["debt_free"], "2026-08-01")
        self.assertEqual(r["debts"][0]["paid_off"], "2026-08-01")

    def test_the_series_starts_today_and_walks_month_firsts(self):
        r = debt.simulate([_d("a", 1000, 0.0, 250)], start=TODAY)
        self.assertEqual(r["series"][0], (TODAY.isoformat(), 1000.0))
        self.assertEqual([p[0] for p in r["series"][1:]],
                         ["2026-08-01", "2026-09-01", "2026-10-01",
                          "2026-11-01"])
        self.assertEqual(r["series"][-1][1], 0.0)
        self.assertEqual(r["debt_free"], "2026-11-01")
        self.assertEqual(r["months"], 4)

    def test_a_minimum_below_the_interest_never_finishes(self):
        # 30% on $10,000 accrues $250/mo; paying $100 grows the balance
        r = debt.simulate([_d("a", 10_000, 30.0, 100)], start=TODAY)
        self.assertIsNone(r["debt_free"])
        self.assertIsNone(r["months"])
        self.assertTrue(r["growing"])
        self.assertEqual(len(r["series"]), debt.MAX_MONTHS + 1)
        self.assertGreater(r["series"][-1][1], 10_000)

    def test_a_run_that_never_finishes_says_so_rather_than_quoting_a_total(self):
        # the fifty-year compounded interest of a growing balance is not a
        # figure anyone will pay; the schedule says it does not finish
        r = debt.simulate([_d("a", 10_000, 30.0, 100)], start=TODAY)
        self.assertFalse(r["finishes"])
        done = debt.simulate([_d("a", 1_000, 12.0, 100)], start=TODAY)
        self.assertTrue(done["finishes"])
        self.assertTrue(debt.simulate([], start=TODAY)["finishes"])

    def test_the_estimated_card_minimum_always_finishes(self):
        # a high-rate card with no reported minimum: the estimate must cover
        # the month's interest at every rate the planner accepts
        for apr in (0.0, 18.0, 24.0, 27.0, 36.0, 99.0, debt.MAX_APR):
            for bal in (40.0, 800.0, 5_000.0, 23_456.0):
                mn = debt.estimated_minimum("card", bal, apr)
                r = debt.simulate([_d("a", bal, apr, mn)], start=TODAY)
                self.assertTrue(r["finishes"], (apr, bal, mn))
                self.assertFalse(r["growing"], (apr, bal, mn))

    def test_a_vanishingly_small_rate_amortizes_like_zero(self):
        # 1 + r rounds to 1 for a rate this small; the payment must not
        # divide by zero and fail the whole plan
        self.assertAlmostEqual(debt.amortized_payment(6000, 1e-300, 60), 100.0)
        self.assertAlmostEqual(debt.estimated_minimum("loan", 6000, 1e-15),
                               100.0)

    def test_nothing_owed_is_debt_free_today(self):
        r = debt.simulate([], start=TODAY)
        self.assertEqual(r["debt_free"], TODAY.isoformat())
        self.assertEqual(r["months"], 0)
        self.assertEqual(r["total_interest"], 0.0)

    def test_the_month_end_day_is_clamped(self):
        r = debt.simulate([_d("a", 100, 0.0, 100)],
                          start=dt.date(2026, 1, 31))
        self.assertEqual(r["debt_free"], "2026-02-01")

    def test_a_bad_method_is_refused(self):
        with self.assertRaises(ValueError):
            debt.simulate([], method="fastest", start=TODAY)


class PaymentDayConventionTests(unittest.TestCase):
    """The servicer's maturity or payoff date is the day of the LAST
    payment, and a month's payment is still due through its payment day.
    Read the other way, the plan drops a payment: on the payment day the
    loan already looks paid for the month, and on the maturity day itself
    it looks paid off with its final instalment still owed."""

    def test_the_maturity_day_itself_still_carries_the_last_payment(self):
        raw = {"maturity_date": "2031-03-15"}
        day = dt.date(2031, 3, 15)
        self.assertEqual(debt.remaining_term_months(raw, day), 1)
        self.assertTrue(debt.payment_still_due(raw, day))
        # the last instalment is the balance plus its month of interest,
        # paid that day: the loan ends on its maturity, not a month later
        pay = debt.mortgage_principal_and_interest(raw, 4000.0, 6.0, day)
        self.assertEqual(pay, 4020.0)
        r = debt.simulate([_d("m", 4000.0, 6.0, pay, kind="mortgage",
                              due_now=True)],
                          start=day, rollover=False)
        self.assertEqual(r["debt_free"], "2031-03-15")
        self.assertEqual(r["debts"][0]["paid_off"], "2031-03-15")
        self.assertEqual(r["months"], 0)
        after = dt.date(2031, 3, 16)
        self.assertIsNone(debt.remaining_term_months(raw, after))
        self.assertFalse(debt.payment_still_due(raw, after))

    def test_this_months_payment_is_due_through_its_payment_day(self):
        raw = {"maturity_date": "2030-05-20"}
        self.assertEqual(
            debt.remaining_term_months(raw, dt.date(2027, 5, 20)), 37)
        self.assertTrue(debt.payment_still_due(raw, dt.date(2027, 5, 20)))
        self.assertEqual(
            debt.remaining_term_months(raw, dt.date(2027, 5, 21)), 36)
        self.assertFalse(debt.payment_still_due(raw, dt.date(2027, 5, 21)))
        # a payment day past the month's end falls on its last day
        end = {"maturity_date": "2030-05-31"}
        self.assertTrue(debt.payment_still_due(end, dt.date(2027, 4, 30)))
        self.assertEqual(
            debt.remaining_term_months(end, dt.date(2027, 4, 30)), 38)

    def test_a_payoff_date_counts_the_payoff_month_itself(self):
        raw = {"expected_payoff_date": "2034-07-28"}
        # July 2026 through July 2034, both ends included
        self.assertEqual(
            debt.remaining_term_months(raw, dt.date(2026, 7, 15)), 97)
        self.assertEqual(
            debt.remaining_term_months(raw, dt.date(2026, 7, 29)), 96)
        # origination plus term: the last payment is the term's month on
        # the origination's day
        term = {"origination_date": "2024-07-10", "loan_term": "84 months"}
        self.assertEqual(
            debt.remaining_term_months(term, dt.date(2026, 7, 10)), 61)
        self.assertEqual(
            debt.remaining_term_months(term, dt.date(2026, 7, 11)), 60)

    def test_a_payment_still_due_today_is_walked_this_month(self):
        r = debt.simulate([_d("a", 1000, 0.0, 250, kind="loan",
                              due_now=True)], start=TODAY)
        self.assertEqual(r["series"][0], (TODAY.isoformat(), 1000.0))
        self.assertEqual(r["debt_free"], "2026-10-01")
        self.assertEqual(r["months"], 3)
        self.assertEqual(r["total_paid"], 1000.0)


class ServicerDatesDecideThisMonthTests(unittest.TestCase):
    """When the servicer says when the payment was made or is next due,
    that — not the maturity date's day of the month, which is often not
    the due day — decides whether this month's payment is still owed.
    Read from the maturity day alone, a payment already in the balance is
    paid a second time and the loan is planned paid off a month early."""

    TODAY = dt.date(2026, 11, 8)

    def test_a_payment_made_this_month_is_not_paid_again(self):
        raw = {"expected_payoff_date": "2031-06-30",
               "last_payment_date": "2026-11-05",
               "next_payment_due_date": "2026-12-05",
               "minimum_payment_amount": 250}
        # December 2026 through June 2031
        self.assertEqual(debt.remaining_term_months(raw, self.TODAY), 55)
        self.assertFalse(debt.payment_still_due(raw, self.TODAY))
        loan = _d("s", 12_000, 6.0, 250, kind="loan",
                  due_now=debt.payment_still_due(raw, self.TODAY))
        r = debt.simulate([loan], start=self.TODAY, rollover=False)
        # one payment by December's first: a month's interest, then $250
        self.assertEqual(r["series"][1], ("2026-12-01", 11810.0))
        # $250 leaves $6.10 after the payoff month's payment: the reported
        # minimum, not the term, sets the last cents (paid twice in the
        # first month, the walk would say June)
        self.assertEqual(r["series"][-2], ("2031-06-01", 6.1))
        self.assertEqual(r["debt_free"], "2031-07-01")

    def test_a_payment_due_later_this_month_is_still_owed(self):
        raw = {"expected_payoff_date": "2031-06-02",
               "next_payment_due_date": "2026-11-20"}
        # the maturity day (the 2nd) has passed, the due day has not
        self.assertTrue(debt.payment_still_due(raw, self.TODAY))
        self.assertEqual(debt.remaining_term_months(raw, self.TODAY), 56)
        today_due = {**raw, "next_payment_due_date": "2026-11-08"}
        self.assertTrue(debt.payment_still_due(today_due, self.TODAY))

    def test_a_due_date_still_ahead_this_month_is_owed_whatever_was_paid(self):
        """An extra principal payment, a late one for last month or half
        of a biweekly pair lands this month too, yet the servicer still
        lists this month's due date ahead: that payment is still owed."""
        raw = {"maturity_date": "2031-06-28",
               "last_payment_date": "2026-11-03",
               "next_payment_due_date": "2026-11-28"}
        self.assertTrue(debt.payment_still_due(raw, self.TODAY))
        self.assertEqual(debt.remaining_term_months(raw, self.TODAY), 56)

    def test_a_next_due_date_in_a_later_month_means_this_month_is_paid(self):
        raw = {"maturity_date": "2031-06-30",
               "next_payment_due_date": "2026-12-30"}
        self.assertFalse(debt.payment_still_due(raw, self.TODAY))
        self.assertEqual(debt.remaining_term_months(raw, self.TODAY), 55)

    def test_dates_that_do_not_read_as_dates_fall_back_to_the_maturity_day(self):
        raw = {"maturity_date": "2031-06-30",
               "last_payment_date": "not a date",
               "next_payment_due_date": 20261205}
        self.assertTrue(debt.payment_still_due(raw, self.TODAY))
        self.assertEqual(debt.remaining_term_months(raw, self.TODAY), 56)


class IssuerFieldTests(unittest.TestCase):
    def test_a_cards_purchase_apr_wins(self):
        raw = {"aprs": [
            {"apr_type": "cash_apr", "apr_percentage": 29.99,
             "balance_subject_to_apr": 0},
            {"apr_type": "purchase_apr", "apr_percentage": 21.24,
             "balance_subject_to_apr": 1500}]}
        self.assertEqual(debt.issuer_apr(raw, "credit card"), 21.24)

    def test_without_a_purchase_apr_the_rate_with_money_under_it(self):
        raw = {"aprs": [
            {"apr_type": "cash_apr", "apr_percentage": 29.99,
             "balance_subject_to_apr": 0},
            {"apr_type": "balance_transfer_apr", "apr_percentage": 0.0,
             "balance_subject_to_apr": 2000}]}
        self.assertEqual(debt.issuer_apr(raw, "credit card"), 0.0)
        # nothing under any of them → the highest
        raw = {"aprs": [{"apr_type": "cash_apr", "apr_percentage": 29.99},
                        {"apr_type": "other", "apr_percentage": 18.0}]}
        self.assertEqual(debt.issuer_apr(raw, "credit card"), 29.99)

    def test_a_mortgage_and_a_student_loan_carry_their_rate(self):
        self.assertEqual(
            debt.issuer_apr({"interest_rate": {"percentage": 4.125,
                                               "type": "fixed"}},
                            "mortgage"), 4.125)
        self.assertEqual(
            debt.issuer_apr({"interest_rate_percentage": 5.5}, "student"),
            5.5)

    def test_junk_reads_as_no_rate(self):
        self.assertIsNone(debt.issuer_apr(None, "credit card"))
        self.assertIsNone(debt.issuer_apr({}, "credit card"))
        self.assertIsNone(debt.issuer_apr(
            {"aprs": [{"apr_type": "purchase_apr",
                       "apr_percentage": "N/A"}]}, "credit card"))
        self.assertIsNone(debt.issuer_apr({"interest_rate_percentage": True},
                                          "student"))
        self.assertIsNone(debt.issuer_apr({"interest_rate_percentage": -3},
                                          "student"))
        self.assertIsNone(debt.issuer_apr({"interest_rate_percentage": 250},
                                          "student"))
        self.assertIsNone(debt.issuer_apr({"aprs": "purchase"}, "credit card"))

    def test_the_minimum_payment(self):
        self.assertEqual(debt.issuer_minimum({"minimum_payment_amount": 35}),
                         35.0)
        self.assertEqual(debt.issuer_minimum({"next_monthly_payment": "1450.5"}),
                         1450.5)
        self.assertIsNone(debt.issuer_minimum({"minimum_payment_amount": 0}))
        self.assertIsNone(debt.issuer_minimum({"minimum_payment_amount": "N/A"}))
        self.assertIsNone(debt.issuer_minimum(None))

    def test_the_estimate_for_a_debt_the_bank_said_nothing_about(self):
        # a card: the month's interest plus 1% of the balance, as issuers
        # compute it, at least $25 and never more than the balance
        self.assertAlmostEqual(debt.estimated_minimum("card", 5000, 20), 133.33,
                               places=2)
        self.assertAlmostEqual(debt.estimated_minimum("card", 5000, 27), 162.50,
                               places=2)
        # an unknown rate (0) still estimates at least 2%, near a typical card
        self.assertEqual(debt.estimated_minimum("card", 5000, 0), 100.0)
        self.assertEqual(debt.estimated_minimum("card", 600, 20), 25.0)
        self.assertEqual(debt.estimated_minimum("card", 10, 20), 10.0)
        # a five-year loan at 0% is the balance over 60 months
        self.assertAlmostEqual(debt.estimated_minimum("loan", 6000, 0), 100.0)
        # a 30-year mortgage at 6% on $300k is the textbook ~$1,799
        self.assertAlmostEqual(debt.estimated_minimum("mortgage", 300_000, 6.0),
                               1798.65, places=1)

    def test_the_kind(self):
        self.assertEqual(debt.debt_kind("credit", "credit card"), "card")
        self.assertEqual(debt.debt_kind("loan", "mortgage"), "mortgage")
        self.assertEqual(debt.debt_kind("loan", "home equity"), "mortgage")
        self.assertEqual(debt.debt_kind("loan", "auto"), "loan")
        self.assertEqual(debt.debt_kind("loan", None), "loan")


class PlanDocumentTests(unittest.TestCase):
    def test_the_default_plan(self):
        self.assertEqual(debt.normalize_plan(None),
                         {"method": "avalanche", "extra_monthly": 0.0,
                          "debts": {}})

    def test_money_typed_the_way_people_type_it(self):
        p = debt.normalize_plan({"method": "snowball",
                                 "extra_monthly": "$1,250.50",
                                 "debts": {"c1": {"apr": "19.99%",
                                                  "min_payment": "$40",
                                                  "skip": 1}}})
        self.assertEqual(p, {"method": "snowball", "extra_monthly": 1250.5,
                             "debts": {"c1": {"apr": 19.99,
                                              "min_payment": 40.0,
                                              "skip": True}}})

    def test_a_blank_clears_an_override(self):
        p = debt.normalize_plan({"debts": {"c1": {"apr": "",
                                                  "min_payment": None}}})
        self.assertEqual(p["debts"], {"c1": {"apr": None,
                                             "min_payment": None}})
        # an override object with nothing in it is not stored
        self.assertEqual(debt.normalize_plan({"debts": {"c1": {}}})["debts"],
                         {})

    def test_what_is_refused(self):
        for bad in ({"method": "fastest"},
                    {"extra_monthly": -5},
                    {"extra_monthly": "lots"},
                    {"extra_monthly": float("nan")},
                    {"extra_monthly": 2_000_000},
                    {"debts": []},
                    {"debts": {"c1": {"apr": 101}}},
                    {"debts": {"c1": {"min_payment": -1}}},
                    {"debts": {"c1": "x"}},
                    {"debts": {"": {"apr": 1}}},
                    {"debts": {f"c{i}": {"apr": 1}
                               for i in range(debt.MAX_OVERRIDES + 1)}},
                    "a string"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    debt.normalize_plan(bad)

    def test_a_malformed_saved_plan_reads_as_the_default(self):
        self.assertEqual(debt.plan_settings({debt.PLAN_KEY: "junk"})["method"],
                         "avalanche")
        self.assertEqual(debt.plan_settings({debt.PLAN_KEY: {
            "method": "snowball", "extra_monthly": "-9"}})["method"],
            "avalanche")
        self.assertEqual(debt.plan_settings({})["extra_monthly"], 0.0)


class LoadDebtsTests(unittest.TestCase):
    """The debts as the page sees them, read from a tenant."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        c = self.conn
        c.execute("INSERT INTO accounts (id,item_id,name,type,subtype,mask,"
                  "balance_current) VALUES "
                  "('auto','it1','Car Loan','loan','auto','0001',9000),"
                  "('mort','it1','Home Loan','loan','mortgage','0002',240000),"
                  "('paid','it1','Old Card','credit','credit card',null,0),"
                  "('chk2','it1','Savings','depository','savings',null,800)")
        c.execute("INSERT INTO liabilities (account_id, raw) VALUES "
                  "('card', %s), ('mort', %s)",
                  (jsonb({"aprs": [{"apr_type": "purchase_apr",
                                    "apr_percentage": 24.99}],
                          "minimum_payment_amount": 35}),
                   jsonb({"interest_rate": {"percentage": 5.25},
                          "next_monthly_payment": 1500})))

    def tearDown(self):
        self.conn.close()

    def test_cards_and_loans_with_a_balance_and_where_the_inputs_came_from(self):
        cfg = budget.load_config(self.conn)
        rows = {d["id"]: d for d in debt.load_debts(self.conn, cfg)}
        self.assertEqual(set(rows), {"card", "auto", "mort"})
        card = rows["card"]
        self.assertEqual((card["kind"], card["apr"], card["apr_source"],
                          card["min_payment"], card["min_source"]),
                         ("card", 24.99, "issuer", 35.0, "issuer"))
        mort = rows["mort"]
        # the servicer's $1,500 may include escrow, but with no term and no
        # original principal there is no P&I to derive, so the reported
        # payment stands rather than a fresh 30-year guess on the balance
        self.assertEqual((mort["kind"], mort["apr"], mort["apr_source"],
                          mort["min_payment"], mort["min_source"],
                          mort["payment_reported"]),
                         ("mortgage", 5.25, "issuer", 1500.0, "issuer",
                          1500.0))
        auto = rows["auto"]
        self.assertEqual((auto["kind"], auto["apr_source"], auto["min_source"]),
                         ("loan", "typical", "estimate"))
        # no rate from the bank or the household: a car loan's typical one
        self.assertEqual(auto["apr"], debt.TYPICAL_APR["auto"])
        self.assertEqual(auto["min_payment"], 180.34)    # 9000 over 60 at it
        self.assertEqual(auto["mask"], "0001")
        self.assertEqual(auto["institution"], "Test Bank")

    def test_a_saved_override_beats_the_issuer(self):
        write_config(self.conn, debt_plan={
            "method": "snowball", "extra_monthly": 200,
            "debts": {"card": {"apr": 18.0},
                      "auto": {"apr": 6.5, "min_payment": 275, "skip": True}}})
        cfg = budget.load_config(self.conn)
        rows = {d["id"]: d for d in debt.load_debts(self.conn, cfg)}
        self.assertEqual((rows["card"]["apr"], rows["card"]["apr_source"],
                          rows["card"]["min_source"]), (18.0, "you", "issuer"))
        self.assertEqual((rows["auto"]["apr"], rows["auto"]["min_payment"],
                          rows["auto"]["skip"]), (6.5, 275.0, True))
        # the issuer's number is still carried, so the page can offer it back
        self.assertEqual(rows["card"]["apr_issuer"], 24.99)

    def test_build_returns_overrides_of_debts_it_does_not_list(self):
        # the save replaces the whole plan, so a client can only keep the
        # override of a debt off the list (excluded, or paid to zero this
        # cycle) if the payload hands it back
        write_config(self.conn, excluded_accounts=["mort"], debt_plan={
            "debts": {"mort": {"apr": 6.25}, "card": {"skip": True}}})
        r = debt.build(self.conn, TODAY)
        self.assertNotIn("mort", {d["id"] for d in r["debts"]})
        self.assertEqual(r["overrides"], {"mort": {"apr": 6.25},
                                          "card": {"skip": True}})

    def test_an_excluded_account_is_not_a_debt_here(self):
        write_config(self.conn, excluded_accounts=["mort"])
        cfg = budget.load_config(self.conn)
        self.assertEqual({d["id"] for d in debt.load_debts(self.conn, cfg)},
                         {"card", "auto"})

    def test_a_restored_blob_with_text_where_numbers_go(self):
        self.conn.execute(
            "UPDATE liabilities SET raw = %s WHERE account_id = 'card'",
            (jsonb({"aprs": [{"apr_type": "purchase_apr",
                              "apr_percentage": "N/A"}],
                    "minimum_payment_amount": "N/A"}),))
        cfg = budget.load_config(self.conn)
        card = {d["id"]: d for d in debt.load_debts(self.conn, cfg)}["card"]
        self.assertEqual((card["apr_source"], card["min_source"]),
                         ("typical", "estimate"))
        self.assertEqual(card["apr"], debt.TYPICAL_APR["card"])

    def _mortgage(self, **raw):
        self.conn.execute("UPDATE accounts SET balance_current = 300000 "
                          "WHERE id = 'mort'")
        self.conn.execute(
            "UPDATE liabilities SET raw = %s WHERE account_id = 'mort'",
            (jsonb({"interest_rate": {"percentage": 6.5},
                    "next_monthly_payment": 2496, "escrow_balance": 3100,
                    **raw}),))
        cfg = budget.load_config(self.conn)
        return {d["id"]: d for d in
                debt.load_debts(self.conn, cfg, today=TODAY)}["mort"]

    def test_a_mortgage_payment_with_escrow_is_not_all_principal(self):
        """A servicer's monthly payment carries the escrow (tax and
        insurance), which never touches the loan. Walking the whole
        payment against the balance retires a 30-year mortgage in about
        16 years and quotes half its interest; the principal-and-interest
        is the rate over the remaining term, and the plan walks that."""
        maturity = TODAY.replace(year=TODAY.year + 30)
        mort = self._mortgage(maturity_date=maturity.isoformat())
        self.assertEqual((mort["min_payment"], mort["min_source"],
                          mort["payment_reported"]),
                         (1894.51, "issuer", 2496.0))
        self.assertEqual(mort["min_issuer"], 1894.51)
        r = debt.simulate([mort], extra=0, start=TODAY, rollover=False)
        self.assertEqual(r["months"], 360)
        self.assertGreater(r["total_interest"], 380_000)

    def test_the_remaining_term_from_origination_and_loan_term(self):
        orig = TODAY.replace(year=TODAY.year - 5)
        mort = self._mortgage(origination_date=orig.isoformat(),
                              loan_term="30 year")
        # 25 years left, and this month's payment (today is the
        # origination's day) still to make: the rate amortized over the
        # 301 payments to go
        self.assertEqual(mort["min_payment"], 2022.94)     # rounded up
        self.assertEqual(mort["min_source"], "issuer")

    def test_an_origination_at_the_end_of_the_calendar_does_not_fail_the_plan(self):
        """An origination date near the calendar's last year plus a 30-year
        term lands past any date that can exist. That loan simply has no
        term to amortize over — the plan must fall back to the reported
        payment, not fail on every load until the row changes."""
        mort = self._mortgage(origination_date="9995-01-01",
                              loan_term="30 year")
        self.assertEqual((mort["min_payment"], mort["min_source"]),
                         (2496.0, "issuer"))
        self.assertIsNone(debt.remaining_term_months(
            {"origination_date": "9999-12-31", "loan_term": "480 months"},
            TODAY))

    def _raw_mortgage(self, balance, raw):
        self.conn.execute("UPDATE accounts SET balance_current = %s "
                          "WHERE id = 'mort'", (balance,))
        self.conn.execute(
            "UPDATE liabilities SET raw = %s WHERE account_id = 'mort'",
            (jsonb(raw),))
        cfg = budget.load_config(self.conn)
        return {d["id"]: d for d in
                debt.load_debts(self.conn, cfg, today=TODAY)}["mort"]

    def test_a_seasoned_mortgage_with_no_term_keeps_the_reported_payment(self):
        """Ten years into a 30-year $250k loan at 6% the balance is ~$209k
        and the real P&I is $1,498.88. Amortizing today's balance over a
        fresh 30 years gives ~$1,254, which stretches the plan ten years
        past the real payoff and adds ~$90k of interest. With no term and
        no original principal to go on, the reported payment is the honest
        figure: an upper bound, never a years-long understatement."""
        mort = self._raw_mortgage(209_214, {
            "interest_rate": {"percentage": 6.0},
            "next_monthly_payment": 2100})
        self.assertEqual((mort["min_payment"], mort["min_source"],
                          mort["payment_reported"]),
                         (2100.0, "issuer", 2100.0))

    def test_a_known_maturity_without_a_rate_still_ends_on_that_date(self):
        """No rate sent, but the maturity is ten years out: the balance
        over the 121 payments left (this month's is still due), not over a
        fresh 360 that would plan a payoff twenty years after the loan
        actually ends."""
        mort = self._raw_mortgage(150_000, {
            "next_monthly_payment": 2100,
            "maturity_date": TODAY.replace(year=TODAY.year + 10).isoformat()})
        self.assertEqual(mort["apr_source"], "typical")
        self.assertEqual(mort["min_source"], "issuer")
        self.assertLess(mort["min_payment"], 2100.0)
        r = debt.simulate([mort], extra=0, start=TODAY, rollover=False)
        self.assertEqual(r["months"], 120)

    def test_the_original_principal_gives_the_scheduled_payment(self):
        """No term, but the original principal and the rate: the payment on
        the original 30-year schedule, which is what a seasoned loan still
        pays — escrow stripped out of the servicer's figure."""
        mort = self._raw_mortgage(209_214, {
            "interest_rate": {"percentage": 6.0},
            "next_monthly_payment": 2100,
            "origination_principal_amount": 250_000})
        self.assertEqual((mort["min_payment"], mort["min_source"],
                          mort["payment_reported"]),
                         (1498.88, "issuer", 2100.0))

    def test_a_mortgage_with_no_reported_payment_still_ends_on_its_maturity(self):
        """The servicer sent the maturity and the rate but no monthly
        payment. The plan must amortize the balance over the payments left
        to that maturity, never over a fresh 30 years: on 241 payments left
        that would plan the payoff ten years late and quote tens of
        thousands of interest that will never be charged."""
        mort = self._raw_mortgage(200_000, {
            "interest_rate": {"percentage": 6.0},
            "maturity_date": TODAY.replace(year=TODAY.year + 20).isoformat()})
        self.assertEqual((mort["min_payment"], mort["min_source"],
                          mort["payment_reported"]),
                         (1429.79, "estimate", None))
        r = debt.simulate([mort], extra=0, start=TODAY, rollover=False)
        self.assertEqual(r["months"], 240)

    def _raw_loan(self, balance, raw, subtype="student"):
        self.conn.execute("UPDATE accounts SET balance_current = %s, "
                          "subtype = %s WHERE id = 'auto'", (balance, subtype))
        self.conn.execute("DELETE FROM liabilities WHERE account_id = 'auto'")
        self.conn.execute("INSERT INTO liabilities (account_id, raw) "
                          "VALUES ('auto', %s)", (jsonb(raw),))
        cfg = budget.load_config(self.conn)
        return {d["id"]: d for d in
                debt.load_debts(self.conn, cfg, today=TODAY)}["auto"]

    def test_a_loan_with_no_minimum_follows_the_servicers_payoff_date(self):
        """A student loan with no minimum but an expected payoff date eight
        years out is paid over those 96 months, not a flat five years that
        would double the payment the household is told to plan for."""
        loan = self._raw_loan(30_000, {
            "interest_rate_percentage": 5.0,
            "expected_payoff_date":
                TODAY.replace(year=TODAY.year + 8).isoformat()})
        self.assertEqual(loan["min_source"], "estimate")
        r = debt.simulate([loan], extra=0, start=TODAY, rollover=False)
        self.assertEqual(r["months"], 96)

    def test_a_loan_with_no_minimum_follows_its_maturity_or_its_term(self):
        loan = self._raw_loan(12_000, {
            "maturity_date": TODAY.replace(year=TODAY.year + 3).isoformat()},
            subtype="auto")
        self.assertEqual(loan["min_payment"], 364.28)
        orig = TODAY.replace(year=TODAY.year - 2)
        loan = self._raw_loan(12_000, {"origination_date": orig.isoformat(),
                                       "loan_term": "84 months"},
                              subtype="auto")
        r = debt.simulate([loan], extra=0, start=TODAY, rollover=False)
        self.assertEqual(r["months"], 60)

    def test_a_loan_months_from_payoff_with_no_payment_keeps_the_estimate(self):
        """A payoff date two months out with no reported payment would make
        the minimum half the balance: the estimate stands instead."""
        today = dt.date(2026, 11, 7)
        raw = {"expected_payoff_date": "2027-01-28"}
        self.assertIsNone(debt.term_payment(raw, 24000.0, 5.5, today))
        far = {"expected_payoff_date": "2034-11-28"}
        self.assertIsNotNone(debt.term_payment(far, 24000.0, 5.5, today))

    def test_a_loan_with_no_term_keeps_the_five_year_estimate(self):
        loan = self._raw_loan(6_000, {}, subtype="auto")
        self.assertEqual((loan["min_payment"], loan["min_source"]),
                         (120.23, "estimate"))

    def test_an_unknown_rate_never_outruns_a_known_payment(self):
        """A typical rate is a guess and the payment is a fact: a loan
        whose reported payment is below a month's interest at the typical
        rate is still walked to an end, at the rate that payment fits."""
        mort = self._raw_mortgage(300_000, {"next_monthly_payment": 1300})
        self.assertEqual(mort["apr_source"], "typical")
        self.assertLess(mort["apr"], debt.TYPICAL_APR["mortgage"])
        r = debt.simulate([mort], extra=0, start=TODAY, rollover=False)
        self.assertIsNotNone(r["debt_free"])
        self.assertGreater(r["total_interest"], 0)

    def test_a_long_loan_with_a_known_payment_keeps_its_typical_rate(self):
        """A car loan with more than five years left pays less a month
        than a five-year schedule would: its payment still covers the
        typical rate's interest, so the rate stands and interest accrues."""
        write_config(self.conn, debt_plan={
            "debts": {"auto": {"min_payment": 100}}})
        auto = {d["id"]: d for d in debt.load_debts(
            self.conn, budget.load_config(self.conn))}["auto"]
        self.assertEqual((auto["apr"], auto["apr_source"], auto["min_source"]),
                         (debt.TYPICAL_APR["auto"], "typical", "you"))
        r = debt.simulate([auto], extra=0, start=TODAY, rollover=False)
        self.assertGreater(r["total_interest"], 0)

    def test_a_card_with_a_small_known_minimum_still_accrues(self):
        """A statement minimum under 1% of today's balance leaves nothing
        for principal at any rate; the card is still not walked at 0%."""
        self.assertGreater(
            debt._rate_within_payment("card", 8000.0, 50.0, 22.0), 0.0)
        # a payment that covers the typical rate's interest keeps it
        self.assertEqual(
            debt._rate_within_payment("card", 1400.0, 35.0, 22.0), 22.0)
        # no jump at 1% of the balance: a cent either side is the same rate
        lo = debt._rate_within_payment("card", 8000.0, 80.0, 22.0)
        hi = debt._rate_within_payment("card", 8000.0, 80.01, 22.0)
        self.assertLess(abs(hi - lo), 0.05)
        # a payment that would not clear the debt in thirty years at 0% is
        # no evidence of a low rate: the typical one stands
        self.assertEqual(
            debt._rate_within_payment("card", 8000.0, 20.0, 22.0), 22.0)

    def test_an_unknown_rate_is_never_walked_at_zero(self):
        """A debt nobody has given a rate for still accrues interest in
        the plan, so the debt-free date and the interest total are not the
        good news a 0% walk would print."""
        loan = self._raw_loan(6_000, {}, subtype="student")
        self.assertEqual((loan["apr"], loan["apr_source"]),
                         (debt.TYPICAL_APR["student"], "typical"))
        r = debt.simulate([loan], extra=0, start=TODAY, rollover=False)
        self.assertGreater(r["total_interest"], 0)

    def test_a_typed_payment_still_wins_for_a_mortgage(self):
        write_config(self.conn, debt_plan={
            "debts": {"mort": {"min_payment": 2000}}})
        mort = self._mortgage()
        self.assertEqual((mort["min_payment"], mort["min_source"],
                          mort["payment_reported"]), (2000.0, "you", 2496.0))

    def test_a_card_minimum_is_still_the_issuers(self):
        # a card's minimum pays interest and fees that are ON the balance,
        # so all of it reduces what is owed — nothing to take out
        card = {d["id"]: d for d in debt.load_debts(
            self.conn, budget.load_config(self.conn))}["card"]
        self.assertEqual((card["min_payment"], card["min_source"],
                          card["payment_reported"]), (35.0, "issuer", None))

    def test_build_carries_both_methods_and_the_baseline(self):
        write_config(self.conn, debt_plan={"method": "snowball",
                                           "extra_monthly": 100})
        r = debt.build(self.conn, TODAY)
        self.assertEqual((r["method"], r["extra_monthly"]), ("snowball", 100.0))
        self.assertEqual(r["saved"], {"method": "snowball",
                                      "extra_monthly": 100.0})
        self.assertEqual(r["plan"]["method"], "snowball")
        self.assertEqual(r["alt"]["method"], "avalanche")
        self.assertEqual(r["minimums"]["extra"], 0.0)
        self.assertTrue(r["estimated"])      # the car loan's inputs
        self.assertEqual(r["today"], TODAY.isoformat())
        # a what-if overrides without touching what is saved
        w = debt.build(self.conn, TODAY, method="avalanche", extra=999)
        self.assertEqual((w["method"], w["extra_monthly"]), ("avalanche", 999.0))
        self.assertEqual(w["saved"]["method"], "snowball")
        self.assertEqual(budget.load_config(self.conn)["debt_plan"]["method"],
                         "snowball")


PW = "correct-horse-battery"


class DebtPlanApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app
        cls.owner = TestClient(app)
        cls.owner.post("/api/signup", data={
            "email": f"debt-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            conn.execute("UPDATE accounts SET balance_current = 3000 "
                         "WHERE id = 'card'")
        finally:
            conn.close()
        cls.viewer = cls._claim(cls, "viewer")

    def _claim(self, role):
        r = self.owner.post("/api/invites",
                            json={"label": role, "role": role, "password": PW})
        assert r.status_code == 200, r.text
        token = r.json()["url"].rsplit("token=", 1)[1]
        client = TestClient(self.app)
        got = client.post("/api/invite/claim", json={
            "token": token, "email": f"{role}-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        assert got.status_code == 200, got.text
        return client

    def test_the_plan_reads_and_saves(self):
        r = self.owner.get("/api/debt-plan")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual([x["id"] for x in d["debts"]], ["card"])
        self.assertEqual(d["saved"], {"method": "avalanche",
                                      "extra_monthly": 0.0})
        self.assertIsNotNone(d["plan"]["debt_free"])

        r = self.owner.post("/api/debt-plan", json={
            "method": "snowball", "extra_monthly": "$150",
            "debts": {"card": {"apr": 19.5, "min_payment": 60}}})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(d["saved"], {"method": "snowball",
                                      "extra_monthly": 150.0})
        card = d["debts"][0]
        self.assertEqual((card["apr"], card["apr_source"], card["min_payment"],
                          card["min_source"]), (19.5, "you", 60.0, "you"))
        self.assertEqual(d["plan"]["monthly"], 210.0)

        # a what-if rides the query and changes nothing saved
        r = self.owner.get("/api/debt-plan?method=avalanche&extra=1000")
        d = r.json()
        self.assertEqual((d["method"], d["extra_monthly"]),
                         ("avalanche", 1000.0))
        self.assertEqual(d["saved"]["method"], "snowball")
        # a blank what-if is "the saved plan", not a 422
        r = self.owner.get("/api/debt-plan?method=&extra=")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["method"], "snowball")

    def test_a_bad_plan_is_refused_with_a_sentence(self):
        r = self.owner.post("/api/debt-plan", json={"method": "fastest"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("avalanche", r.json()["detail"])
        r = self.owner.post("/api/debt-plan",
                            json={"debts": {"card": {"apr": 500}}})
        self.assertEqual(r.status_code, 400)

    def test_a_viewer_reads_but_cannot_save(self):
        self.assertEqual(self.viewer.get("/api/debt-plan").status_code, 200)
        r = self.viewer.post("/api/debt-plan", json={"method": "snowball"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
