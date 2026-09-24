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
        self.assertEqual(debt.estimated_minimum("card", 5000, 20), 100.0)
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
                         ("loan", "none", "estimate"))
        self.assertEqual(auto["min_payment"], 150.0)     # 9000 / 60 at 0%
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
                         ("none", "estimate"))
        self.assertEqual(card["min_payment"], 25.0)

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
                         (1896.21, "issuer", 2496.0))
        self.assertEqual(mort["min_issuer"], 1896.21)
        r = debt.simulate([mort], extra=0, start=TODAY, rollover=False)
        self.assertEqual(r["months"], 360)
        self.assertGreater(r["total_interest"], 380_000)

    def test_the_remaining_term_from_origination_and_loan_term(self):
        orig = TODAY.replace(year=TODAY.year - 5)
        mort = self._mortgage(origination_date=orig.isoformat(),
                              loan_term="30 year")
        # 25 years left: the rate amortized over the 300 payments to go
        self.assertEqual(mort["min_payment"], 2025.63)     # rounded up
        self.assertEqual(mort["min_source"], "issuer")

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
        over the 120 payments left, not over a fresh 360 that would plan a
        payoff twenty years after the loan actually ends."""
        mort = self._raw_mortgage(150_000, {
            "next_monthly_payment": 2100,
            "maturity_date": TODAY.replace(year=TODAY.year + 10).isoformat()})
        self.assertEqual(mort["apr_source"], "none")
        self.assertEqual((mort["min_payment"], mort["min_source"]),
                         (1250.0, "issuer"))
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
