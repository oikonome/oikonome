"""Budget-engine invariants: spend definition, envelope math, matching,
occurrence expansion, verdict semantics. These freeze shipped behaviour —
a failing test here means a real change to what the daily email and the
dashboard report. Do not weaken one to make a change pass: fix the change.
"""

import datetime as dt
import unittest

from oikonome.engine import budget

from .util import TODAY, add_bill, add_txn, make_db, write_config


class BudgetBase(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def status(self, **kw):
        return budget.month_status(self.conn, TODAY, **kw)


class TestSpendDefinition(BudgetBase):
    def test_override_transfer_excluded(self):
        """A manual TRANSFER_OUT override must
        exclude the row even when category_primary says otherwise."""
        add_txn(self.conn, TODAY, 500.0, "Outgoing Wire",
                primary="GENERAL_SERVICES", override="TRANSFER_OUT")
        st = self.status()
        self.assertEqual(st["total_actual"], 0.0)

    def test_primary_transfer_excluded(self):
        add_txn(self.conn, TODAY, 500.0, "BROKERAGE DEPOSIT", primary="TRANSFER_OUT")
        self.assertEqual(self.status()["total_actual"], 0.0)

    def test_cc_payment_excluded_but_loan_counts(self):
        add_txn(self.conn, TODAY, 400.0, "CARD PAYMENT", primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        add_txn(self.conn, TODAY, 300.0, "MORTGAGE", primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_MORTGAGE_PAYMENT")
        self.assertEqual(self.status()["total_actual"], 300.0)

    def test_amazon_override_not_transfer(self):
        add_txn(self.conn, TODAY, 25.0, "AMZN", override="Amazon - Food & Drink")
        st = self.status()
        self.assertEqual(st["buckets"]["food"]["actual"], 25.0)


class TestEnvelope(BudgetBase):
    def setUp(self):
        super().setUp()
        add_bill(self.conn, "Corner Coffee", 100.0, bill_type="envelope")

    def test_cap_only_under(self):
        add_txn(self.conn, TODAY, 40.0, "CORNER COFFEE", primary="FOOD_AND_DRINK")
        st = self.status()
        env = st["buckets"]["fixed"]["envelopes"][0]
        self.assertEqual(env["used"], 40.0)
        self.assertEqual(env["overflow"], 0.0)
        self.assertEqual(st["buckets"]["fixed"]["actual"], 40.0)
        self.assertEqual(st["buckets"]["food"]["actual"], 0.0)
        # cap-only: expected == used, never over-pace
        self.assertEqual(st["buckets"]["fixed"]["expected"], 40.0)

    def test_overflow_to_variable(self):
        for _ in range(3):
            add_txn(self.conn, TODAY, 50.0, "CORNER COFFEE", primary="FOOD_AND_DRINK")
        st = self.status()
        env = st["buckets"]["fixed"]["envelopes"][0]
        self.assertEqual(env["used"], 100.0)
        self.assertEqual(env["overflow"], 50.0)
        self.assertEqual(st["buckets"]["fixed"]["actual"], 100.0)
        self.assertEqual(st["buckets"]["food"]["actual"], 50.0)

    def test_conservation(self):
        """Every counted dollar lands in exactly one bucket."""
        for amt in (60.0, 60.0, 30.0):
            add_txn(self.conn, TODAY, amt, "CORNER COFFEE", primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 80.0, "SAFEWAY", primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY, 20.0, "TARGET")
        st = self.status()
        b = st["buckets"]
        total = b["fixed"]["actual"] + b["food"]["actual"] + b["other"]["actual"]
        self.assertAlmostEqual(total, 250.0, places=2)

    def test_plan_carries_full_monthly(self):
        st = self.status()
        self.assertEqual(st["buckets"]["fixed"]["month_budget"], 100.0)


class TestMatchWords(unittest.TestCase):
    """What a bill and a ledger row are matched on. Four letters and up,
    with a fallback to the short words only when a string has no long word
    at all — otherwise a payee spelled in initials can never find the rows
    that pay it, and its bill reads as unpaid forever."""

    def test_a_long_word_never_matches_on_a_short_one(self):
        self.assertEqual(budget._tokens("City of Pointe d'Amour"),
                         {"city", "pointe", "amour"})
        self.assertEqual(budget._key_token("City of Pointe d'Amour"), "city")
        # 'of' is not a match token just because it is there
        self.assertNotIn("of", budget._tokens("City of Pointe d'Amour"))

    def test_a_name_of_initials_still_gets_a_key(self):
        for payee, key in (("CVS", "cvs"), ("IRS", "irs"), ("DMV", "dmv")):
            self.assertEqual(budget._key_token(payee), key, payee)
            self.assertEqual(budget._tokens(payee), {key}, payee)

    def test_punctuation_shredded_initials_collapse_to_letters(self):
        """'AT&T' tokenizes to 'at' + 't' — both worthless — while the
        ledger spells the same payee 'ATT'."""
        self.assertEqual(budget._key_token("AT&T"), "att")
        self.assertEqual(budget._tokens("ATT BILL PAYMENT"), {"att"})

    def test_a_string_with_no_letters_at_all_yields_nothing(self):
        self.assertEqual(budget._tokens("4417 ***"), set())
        self.assertIsNone(budget._key_token("4417 ***"))


class TestMatching(BudgetBase):
    def test_key_token_required(self):
        add_bill(self.conn, "City of Pointe d'Amour", 86.0,
                 next_due=TODAY.replace(day=5), last_seen=TODAY.replace(day=5))
        add_txn(self.conn, TODAY.replace(day=5), 86.0, "BANGKOK RESTAURANT",
                primary="FOOD_AND_DRINK")
        st = self.status()
        self.assertEqual(st["buckets"]["fixed"]["actual"], 0.0)

    def _set_merchant(self, payee, merchant):
        self.conn.execute("UPDATE bills SET merchant=%s WHERE payee=%s",
                          (merchant, payee))

    def test_merchant_match_is_specific(self):
        """A bill's canonical merchant matches ONLY that merchant: 'corner
        coffee' won't swallow 'Corner Shop' — every token must be
        present."""
        add_bill(self.conn, "Corner Coffee", 10.00,
                 next_due=TODAY.replace(day=4), last_seen=TODAY.replace(day=4))
        self._set_merchant("Corner Coffee", "corner coffee")
        add_txn(self.conn, TODAY.replace(day=4), 10.00, "CORNER SHOP",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, TODAY.replace(day=6), 10.00, "CORNER COFFEE SHOP")
        st = self.status()
        rows = st["buckets"]["fixed"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertIn("COFFEE", rows[0]["name"])

    def test_merchant_bridges_nickname_mismatch(self):
        """A bill whose nickname shares no word with the descriptor still
        matches via its merchant — and near-misses stay out."""
        add_bill(self.conn, "Summit Rent", 500.0,
                 next_due=TODAY.replace(day=2), last_seen=TODAY.replace(day=2))
        self._set_merchant("Summit Rent", "property rental")
        add_txn(self.conn, TODAY.replace(day=2), 500.0, "Rental Property WEB PMTS",
                merchant="Rental Property")
        add_txn(self.conn, TODAY.replace(day=3), 500.0, "ALAMO CAR RENTAL",
                merchant="Alamo Car Rental")
        rows = self.status()["buckets"]["fixed"]["rows"]
        self.assertEqual(len(rows), 1)
        self.assertIn("Rental Property", rows[0]["name"])

    def test_merchant_absorbs_variant(self):
        """Merchant tokens are a subset test: bill merchant 'zenith fibre'
        matches the longer descriptor 'Zenith Fibre Telecom'."""
        add_bill(self.conn, "Fibre Internet", 70.0,
                 next_due=TODAY.replace(day=5), last_seen=TODAY.replace(day=5))
        self._set_merchant("Fibre Internet", "zenith fibre")
        add_txn(self.conn, TODAY.replace(day=5), 70.0, "ZENITH FIBRE TELECOM",
                merchant="Zenith Fibre Telecom")
        rows = self.status()["buckets"]["fixed"]["rows"]
        self.assertEqual(len(rows), 1)

    def test_one_txn_per_occurrence(self):
        add_bill(self.conn, "Netflix", 20.0, next_due=TODAY.replace(day=10),
                 last_seen=TODAY.replace(day=10))
        add_txn(self.conn, TODAY.replace(day=10), 20.0, "NETFLIX")
        add_txn(self.conn, TODAY.replace(day=11), 20.0, "NETFLIX")
        st = self.status()
        self.assertEqual(st["buckets"]["fixed"]["actual"], 20.0)
        self.assertEqual(st["buckets"]["other"]["actual"], 20.0)


class TestOccurrences(unittest.TestCase):
    def occ(self, rec, anchor, y=2026, m=7, floor=None):
        bill = {"recurrence": rec, "anchor": anchor, "floor": floor}
        return budget._occurrences_in_month(bill, y, m)

    def test_monthly_by_month_day(self):
        self.assertEqual(self.occ({"frequency": "MONTHLY", "byMonthDay": [16]},
                                  dt.date(2026, 8, 16)),
                         [dt.date(2026, 7, 16)])

    def test_negative_month_day(self):
        self.assertEqual(self.occ({"frequency": "MONTHLY", "byMonthDay": [-1]},
                                  dt.date(2026, 8, 31)),
                         [dt.date(2026, 7, 31)])

    def test_weekly_phase(self):
        out = self.occ({"frequency": "WEEKLY", "interval": 1},
                       dt.date(2026, 8, 3))       # a Monday
        self.assertEqual([d.weekday() for d in out], [0] * len(out))
        self.assertEqual(len(out), 4)             # Mondays in July 2026

    def test_nth_friday(self):
        out = self.occ({"frequency": "MONTHLY", "byDay": ["FR"],
                        "bySetPos": [1, 2, 3]}, dt.date(2026, 7, 3))
        self.assertEqual(out, [dt.date(2026, 7, 3), dt.date(2026, 7, 10),
                               dt.date(2026, 7, 17)])

    def test_one_time(self):
        self.assertEqual(self.occ({}, dt.date(2026, 7, 20)),
                         [dt.date(2026, 7, 20)])
        self.assertEqual(self.occ({}, dt.date(2026, 8, 20)), [])

    def test_negative_interval_is_clamped(self):
        """Restore can store interval=-1; it must clamp to 1, same as
        save_bill, or expansion walks backward until OverflowError."""
        out = self.occ({"frequency": "DAILY", "interval": -1},
                       dt.date(2026, 7, 15))
        self.assertEqual(out[0], dt.date(2026, 7, 1))
        self.assertEqual(out[-1], dt.date(2026, 7, 31))
        self.assertEqual(len(out), 31)


class TestVerdict(BudgetBase):
    def test_fixed_never_drives_verdict(self):
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1),
                 last_seen=TODAY.replace(day=1))
        add_txn(self.conn, TODAY.replace(day=1), 2000.0, "RENT",
                primary="RENT_AND_UTILITIES")
        st = self.status()
        self.assertEqual(st["buckets"]["fixed"]["actual"], 2000.0)
        self.assertEqual(st["variable_actual"], 0.0)
        self.assertNotEqual(st["verdict"], "OVER BUDGET")

    def test_historical_skips_bills(self):
        add_bill(self.conn, "Rent", 2000.0, next_due=TODAY.replace(day=1))
        st = self.status(historical=True)
        self.assertFalse(st["bills_tracked"])
        self.assertEqual(st["buckets"]["fixed"]["month_budget"], 0.0)

    def test_first_of_month_is_on_budget_not_under(self):
        """UNDER BUDGET off an empty ledger on the 1st would be a statement
        about the calendar, not about spending — the verdict is either on
        or over. Nothing spent, on day 1, is ON BUDGET."""
        st = budget.month_status(self.conn, TODAY.replace(day=1))
        self.assertEqual(st["variable_actual"], 0.0)
        self.assertEqual(st["verdict"], "ON BUDGET")

    def test_over_budget_still_fires_on_the_first(self):
        """Only the good news is suppressed early — you can absolutely blow
        the month's budget on day 1, and that is worth saying at once."""
        first = TODAY.replace(day=1)
        add_txn(self.conn, first, 4000.0, "SPLURGE",
                primary="GENERAL_MERCHANDISE")
        self.assertEqual(budget.month_status(self.conn, first)["verdict"],
                         "OVER BUDGET")

    def test_on_pace_spend_with_nothing_yet_today_is_on_budget(self):
        """Expected-to-date counts the days that have ELAPSED. Today has
        not: the today-ledger hands out today's allowance from midnight,
        so a household exactly on pace through yesterday, with nothing
        spent yet this morning, is ON BUDGET — not UNDER by one day's
        budget."""
        write_config(self.conn, food_monthly=1000, other_monthly=1000,
                     dynamic_variable_budget=False)
        # TODAY is the 15th of a 31-day month: 14 days elapsed
        pace = round(2000 * 14 / 31, 2)          # 903.23
        add_txn(self.conn, TODAY - dt.timedelta(days=1), pace, "TARGET")
        self.conn.commit()
        st = self.status()
        self.assertAlmostEqual(
            st["buckets"]["food"]["expected"] + st["buckets"]["other"]["expected"],
            pace, places=2)
        self.assertAlmostEqual(st["variance"], 0.0, places=2)
        self.assertEqual("ON BUDGET", st["verdict"])

    def test_under_budget_returns_once_the_month_is_underway(self):
        """Past the early window the verdict works as it always did."""
        st = budget.month_status(self.conn, TODAY.replace(day=20))
        self.assertEqual(st["variable_actual"], 0.0)
        self.assertEqual(st["verdict"], "UNDER BUDGET")


class TestWhyReasons(BudgetBase):
    """The Why box's merchant ranking is pure-variable; bill-related overage
    (envelope overflow, bill-shaped extra occurrences) still counts in the
    variance but is broken out as a labeled tail item."""

    def test_envelope_overflow_labeled_not_ranked(self):
        add_bill(self.conn, "Corner Coffee", 100.0, bill_type="envelope")
        for _ in range(6):
            add_txn(self.conn, TODAY, 300.0, "CORNER COFFEE",
                    primary="FOOD_AND_DRINK")
        st = self.status()
        self.assertEqual(st["verdict"], "OVER BUDGET")
        food = next(r for r in st["reasons"] if r["bucket"] == "food")
        self.assertEqual(food["detail"], ["Corner Coffee (over envelope) $1,700"])

    def test_extra_bill_charge_labeled(self):
        add_bill(self.conn, "YouTube Premium", 27.0,
                 next_due=TODAY.replace(day=12),
                 last_seen=dt.date(2026, 6, 12))
        add_txn(self.conn, TODAY.replace(day=12), 27.0, "YOUTUBE PREMIUM")
        add_txn(self.conn, TODAY.replace(day=13), 27.0, "YOUTUBE PREMIUM")
        add_txn(self.conn, TODAY, 1200.0, "TARGET")
        st = self.status()
        self.assertEqual(st["buckets"]["fixed"]["actual"], 27.0)
        other = next(r for r in st["reasons"] if r["bucket"] == "other")
        self.assertIn("YouTube Premium (beyond bill schedule) $27",
                      other["detail"])
        self.assertIn("TARGET $1,200", other["detail"])
        # the labeled item trails the ordinary merchant ranking
        self.assertTrue(other["detail"][-1].startswith("YouTube Premium ("))

    def test_token_coincidence_stays_ordinary(self):
        """A merchant sharing the bill's token but at an unrelated amount is
        ordinary variable spend, not 'beyond bill schedule'."""
        add_bill(self.conn, "Metro Express Car Wash", 21.0,
                 next_due=TODAY.replace(day=23),
                 last_seen=dt.date(2026, 6, 23))
        add_txn(self.conn, TODAY, 1100.0, "METRO DINER")
        st = self.status()
        other = next(r for r in st["reasons"] if r["bucket"] == "other")
        self.assertEqual(other["detail"], ["METRO DINER $1,100"])


class TestTenantIsolation(unittest.TestCase):
    """The fixture's tenant-per-test design makes this explicit — one
    tenant's data must be invisible to another's engine run."""

    def test_engine_scoped_to_tenant(self):
        c1, c2 = make_db(), make_db()
        write_config(c1)
        write_config(c2)
        add_txn(c1, TODAY, 999.0, "TENANT ONE SPEND")
        st2 = budget.month_status(c2, TODAY)
        self.assertEqual(st2["total_actual"], 0.0)
        st1 = budget.month_status(c1, TODAY)
        self.assertEqual(st1["total_actual"], 999.0)
        c1.close()
        c2.close()




class TestEarlyPaidBillAgreesAcrossSurfaces(BudgetBase):
    """month_status and upcoming_bill_occurrences must agree about a bill
    paid just before the month started.

    `upcoming_bill_occurrences` matches payments over a window reaching
    PREPAY_MATCH_DAYS BEFORE month start, precisely so a bill autopaid a few
    days early isn't reserved twice. Fetching rows from month start only
    makes the same payment invisible to `month_status`: the occurrence reads
    unpaid and becomes an expectation (and, once its due date passes,
    overdue).

    Same bill, same ledger, two answers — on the Today page, which is the
    surface the product is built around. The verdict would say money was
    still owed while headroom knew it had already gone out.
    """

    def _setup_early_paid(self):
        # bill due the 2nd of this month, autopaid 4 days early (last month)
        due = TODAY.replace(day=2)
        add_bill(self.conn, "Domain Registrar", 60.0, frequency="MONTHLY",
                 next_due=due)
        paid_on = due - dt.timedelta(days=4)     # previous month
        add_txn(self.conn, paid_on, 60.0, "DOMAIN REGISTRAR", account="chk")
        self.conn.commit()
        return due

    def test_month_status_sees_the_early_payment(self):
        self._setup_early_paid()
        st = self.status()
        due_unpaid = [o for o in st.get("overdue_unpaid", [])
                      if "domain registrar" in (o[0] or "").lower()]
        self.assertEqual(
            [], due_unpaid,
            "month_status re-dated an already-paid bill as overdue — the "
            "payment landed before month start, outside its match window")

    def test_both_surfaces_agree_which_occurrence_is_outstanding(self):
        """The precise form: the forecast settles July and reserves AUGUST.
        month_status must agree that July is not outstanding."""
        self._setup_early_paid()
        cfg = budget.load_config(self.conn)
        upcoming = budget.upcoming_bill_occurrences(
            self.conn, cfg, TODAY, TODAY + dt.timedelta(days=45))
        reserved_months = {str(o["due"])[:7] for o in upcoming
                           if "domain registrar" in (o["payee"] or "").lower()}
        overdue_months = {o[2][:7] for o in self.status().get("overdue_unpaid", [])
                          if "domain registrar" in (o[0] or "").lower()}
        self.assertNotIn(TODAY.strftime("%Y-%m"), reserved_months,
                         "forecast should have settled this month's occurrence")
        self.assertEqual(
            set(), overdue_months,
            f"verdict calls {overdue_months} overdue while the forecast has "
            f"moved on to {reserved_months}")

    def test_prepaid_bill_does_not_inflate_this_months_actuals(self):
        """The trap in the fix: matching over a wider window must not drag
        last month's cash into this month's fixed actuals."""
        self._setup_early_paid()
        st = self.status()
        # the payment belongs to LAST month and must not appear among this
        # month's spend rows, nor in the fixed bucket's actual
        self.assertEqual([], [r for r in st["rows"]
                              if "domain registrar" in (r["payee"] or "").lower()],
                         "last month's payment leaked into this month's rows")
        self.assertEqual(
            0.0, round(st["buckets"]["fixed"]["actual"], 2),
            "last month's payment was counted as this month's fixed spend")

    def test_next_months_bill_paid_this_month_is_fixed_spend_not_variable(self):
        """The mirror case: rent due the 1st, autopaid on the 28th of the
        month BEFORE. The forecast matches that payment to next month's
        occurrence; the verdict must too. Left unmatched it is a $2,000
        variable purchase — OVER BUDGET on the day the rent went out —
        while headroom, correctly, already counts the money as gone."""
        today = dt.date(2026, 7, 29)
        add_bill(self.conn, "Rent", 2000.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 8, 1), last_seen=dt.date(2026, 7, 1))
        add_txn(self.conn, dt.date(2026, 7, 1), 2000.0, "RENT", account="chk")
        add_txn(self.conn, dt.date(2026, 7, 28), 2000.0, "RENT", account="chk")
        self.conn.commit()
        st = budget.month_status(self.conn, today)
        b = st["buckets"]
        self.assertEqual(0.0, round(b["other"]["actual"], 2),
                         "next month's rent, paid early, was called variable spend")
        # cash basis: both payments left this month, both are this
        # month's fixed spend and this month's fixed plan
        self.assertEqual(4000.0, round(b["fixed"]["actual"], 2))
        self.assertEqual(4000.0, round(b["fixed"]["month_budget"], 2))
        self.assertNotEqual("OVER BUDGET", st["verdict"])
        self.assertEqual([], st["overdue_unpaid"])
        # and next month's own status does not count it again, nor plan
        # for an occurrence whose cash already left
        nxt = budget.month_status(self.conn, dt.date(2026, 8, 15))
        self.assertEqual(0.0, round(nxt["buckets"]["fixed"]["actual"], 2))
        self.assertEqual([], nxt["overdue_unpaid"])

    def test_unpaid_next_month_occurrence_is_not_this_months_plan(self):
        """Expanding next month's occurrences is for MATCHING only: an
        occurrence nobody has paid yet is neither this month's expectation
        nor its fixed plan, or every month would carry two rents."""
        add_bill(self.conn, "Rent", 2000.0, frequency="MONTHLY",
                 next_due=dt.date(2026, 8, 1), last_seen=dt.date(2026, 7, 1))
        add_txn(self.conn, dt.date(2026, 7, 1), 2000.0, "RENT", account="chk")
        self.conn.commit()
        st = self.status()
        self.assertEqual(2000.0, round(st["buckets"]["fixed"]["month_budget"], 2))
        self.assertEqual(2000.0, round(st["buckets"]["fixed"]["expected"], 2))
        self.assertEqual([], st["overdue_unpaid"])


class WhyHeadlineNeverContradictsTheVerdict(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_spread_overage_headline_says_over(self):
        """Both buckets just under their own 8% line, the sum over the
        verdict's 5% line: verdict OVER, no per-bucket 'over' entry —
        the headline must still say over, never 'On pace'."""
        write_config(self.conn, food_monthly=1300, other_monthly=1300,
                     dynamic_variable_budget=False)
        today = dt.date(2026, 8, 16)             # day 16 of 31: 15 elapsed
        frac = 15 / 31
        # target variance per bucket: just under 8% of expected, while
        # the combined variance clears max(50, 5% of combined expected)
        exp = 1300 * frac
        spend = exp * 1.07                       # +7% each — under 8%
        add_txn(self.conn, today, spend, "GROCER A",
                primary="FOOD_AND_DRINK")
        add_txn(self.conn, today, spend, "SHOP B",
                primary="GENERAL_MERCHANDISE")
        st = budget.month_status(self.conn, today)
        self.assertEqual(st["verdict"], "OVER BUDGET")
        self.assertFalse(
            [e for e in st["why"]["entries"] if e["tone"] == "over"],
            "fixture drift: a bucket crossed its own bar — retune spend")
        self.assertNotIn("On pace", st["why"]["headline"])
        self.assertIn("Over", st["why"]["headline"])


if __name__ == "__main__":
    unittest.main()
