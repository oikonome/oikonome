"""Money-engine invariants when the data is partial: every dollar reaches a
figure exactly once, and no arithmetic corner takes a page down.

* The dynamic variable scale engages only when there IS a variable budget
   to compress. With food+other at $0 and fixed bills over income,
   `available < static_total` is `-1000 < 0` → True, and
   `available / static_total` is a ZeroDivisionError that takes Today, the
   forecast, the lenses and the email gather with it — for any tenant with
   $0 variable budgets, an income scenario and a heavy fixed month, which
   is a common mid-wizard state.

* reporting._cash_graph's current-month CASH term must recognize a posted
   card settlement by more than
   category_detailed = LOAN_PAYMENTS_CREDIT_CARD_PAYMENT: a checking-side
   autopay a sparse source files as TRANSFER_OUT (e.g. "CHASE AUTOPAY")
   otherwise goes missing and the month's cash-leave under-counts. A
   TRANSFER_OUT outflow counts as a settlement when a credit-account
   inflow pairs with it (same amount, a few days apart) — the
   each-card-dollar-exactly-once invariant, extended to the
   transfer-shaped payment leg.

* The same term subtracts posted savings transfers, because the forecast
   walk schedules the savings plan NET of them. A transfer posted mid-month
   has already left live checking (the walk's start), so counted in neither
   place it lands ZERO times; subtracting the same per-goal posted amount
   the walk nets out makes the month's point max(plan, posted).

* The Year lens payload carries `saved_ytd` — the ledger's net into the
   standing "Savings" goal over the lens year's elapsed span — so the SPA
   does not extrapolate the money map's Savings row as
   rate_90d × elapsed months.

* retirement.current_buckets nets card debt through the sim's whole draw
   order (cash → taxable → tax-deferred → Roth) and carries whatever still
   remains as `debt_residual`. Flooring at taxable makes debt beyond
   cash+taxable vanish and every projection start from an optimistic
   portfolio.

* A manually-tracked "card payment" recurring bill must not double-reserve
   against the live card machinery: headroom's required is
   card_debt + due_total, and the forecast walks both the bill occurrence
   and the per-card payoff event. (Such a bill can never occurrence-match
   its payment either — _spend_rows excludes card payments — so it also
   re-dates "overdue" forever.) upcoming_bill_occurrences skips bills whose
   payee marks them as card payments while live card debt exists for the
   scenarios to pay; with no live card debt the bill is the only signal and
   stays.
"""

import datetime as dt
import unittest

from oikonome.engine import budget, forecast, reporting, retirement

from .util import TODAY, add_bill, add_txn, make_db, write_config


class ZeroBudgetDynamicScaleTests(unittest.TestCase):
    """$0 food+other budgets + income scenario + fixed bills > income:
    month_status must return a clean status, not ZeroDivisionError."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0,
                     dynamic_variable_budget=True,
                     budgeted_income_monthly=3000.0)
        # a fixed month heavier than income → available is negative
        add_bill(self.conn, "Rent", 4000.0,
                 next_due=dt.date(2026, 7, 20), last_seen=dt.date(2026, 6, 20))

    def tearDown(self):
        self.conn.close()

    def test_month_status_survives_zero_variable_budgets(self):
        """The crash this guards against takes Today, the forecast and the
        email down with it."""
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["buckets"]["food"]["month_budget"], 0.0)
        self.assertEqual(st["buckets"]["other"]["month_budget"], 0.0)
        # nothing was compressed — no scale note for the SPA to explain
        self.assertIsNone(st["variable_scale"])
        self.assertEqual(st["verdict"], "ON BUDGET")

    def test_downstream_forecast_survives_too(self):
        fc = forecast.build(self.conn, today=TODAY, days=30)
        self.assertIn("pace_stmt", fc)

    def test_real_budgets_still_compress_to_zero_with_a_note(self):
        """Companion regression: with budgets to compress, the same
        underwater month scales them to $0 AND says so."""
        write_config(self.conn, food_monthly=500, other_monthly=500,
                     dynamic_variable_budget=True,
                     budgeted_income_monthly=3000.0)
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual(st["variable_scale"]["factor"], 0.0)
        self.assertEqual(st["variable_scale"]["available"], 0.0)
        self.assertEqual(st["buckets"]["food"]["month_budget"], 0.0)


class _CashGraphHarness(unittest.TestCase):
    """The cash-graph test pattern: _cash_graph uses the real dt.date.today(), so
    these do too; assertions are algebra over the same forecast series the
    graph folds in, which keeps them month-boundary safe."""

    INCOME = 4000.0

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.today = dt.date.today()
        self.ms = self.today.replace(day=1)
        add_txn(self.conn, self.ms, -self.INCOME, "EMPLOYER PAYROLL",
                account="chk", primary="INCOME")

    def tearDown(self):
        self.conn.close()

    def _fwd_and_forecast(self):
        g = reporting.compute_cashflow(self.conn)["cash_graph"]
        fc = forecast.build(self.conn, days=forecast.HORIZON_DAYS)
        fwd = sum(v for _, v in g["points"][g["forecast_from"]:])
        return fwd, fc

    def _expect(self, fc, mtd_cash):
        # sum of every forecast-month net == (final walk balance − checking
        # now) + the current month's actual CASH flow so far
        return fc["pace_stmt"]["series"][-1][1] - fc["checking"] + mtd_cash


class TransferShapedCardPaymentTests(_CashGraphHarness):
    """A checking-side card payment a sparse source filed TRANSFER_OUT is
    still a card settlement — the cash left checking when it posted, and
    the walk won't re-schedule it (the card balance already dropped).
    Recognition: a TRANSFER_OUT outflow paired 1:1 with a credit-account
    inflow of the same amount within a few days."""

    def test_transfer_out_autopay_counts_as_the_card_settlement(self):
        """Charge + autopay both posted this month, the checking leg
        categorized TRANSFER_OUT ("CHASE AUTOPAY"). Unrecognized, the MTD term
        carries income only and the month's cash-leave under-counts by the
        whole payment."""
        self.conn.execute(
            "UPDATE accounts SET balance_current=0 WHERE id='card'")
        add_txn(self.conn, self.ms, 300.0, "STEREO SHOP", account="card")
        add_txn(self.conn, self.ms, 300.0, "CHASE AUTOPAY", account="chk",
                primary="TRANSFER_OUT")
        add_txn(self.conn, self.ms, -300.0, "PAYMENT THANK YOU",
                account="card", primary="TRANSFER_IN")
        fwd, fc = self._fwd_and_forecast()
        self.assertEqual(fc["card_events_stmt"], [])   # nothing left to pay
        self.assertAlmostEqual(fwd, self._expect(fc, self.INCOME - 300.0),
                               places=1)

    def test_unpaired_transfer_out_is_not_a_card_payment(self):
        """A transfer with no credit-side counterpart (moving money to an
        untracked account) stays out of the card term — the pairing is the
        evidence, not the TRANSFER_OUT label."""
        self.conn.execute(
            "UPDATE accounts SET balance_current=0 WHERE id='card'")
        add_txn(self.conn, self.ms, 200.0, "TRANSFER TO BROKERAGE",
                account="chk", primary="TRANSFER_OUT")
        fwd, fc = self._fwd_and_forecast()
        self.assertAlmostEqual(fwd, self._expect(fc, self.INCOME), places=1)

    def test_categorized_payment_with_its_credit_leg_counts_once(self):
        """The invariant under the expansion: a properly-categorized payment
        whose credit-side leg is also visible must not count twice — the
        credit inflow is claimed by the categorized leg, not re-paired."""
        self.conn.execute(
            "UPDATE accounts SET balance_current=0 WHERE id='card'")
        add_txn(self.conn, self.ms, 300.0, "STEREO SHOP", account="card")
        add_txn(self.conn, self.ms, 300.0, "CARD AUTOPAY", account="chk",
                primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        add_txn(self.conn, self.ms, -300.0, "PAYMENT THANK YOU",
                account="card", primary="TRANSFER_IN")
        # a same-amount transfer nearby must not steal the settlement's
        # credit leg and get itself counted as a second payment
        add_txn(self.conn, self.ms, 300.0, "TRANSFER TO BROKERAGE",
                account="chk", primary="TRANSFER_OUT")
        fwd, fc = self._fwd_and_forecast()
        self.assertAlmostEqual(fwd, self._expect(fc, self.INCOME - 300.0),
                               places=1)


class PostedSavingsTransferTests(_CashGraphHarness):
    """The savings doctrine, worked: the forecast walk schedules each
    goal's plan NET of what already posted this month, and the posted money
    is already out of live checking (the walk's start) — so the MTD cash
    term must subtract the SAME posted amount, or the month's point counts
    it zero times. Month total = posted (MTD) + max(0, plan − posted)
    (walk) = max(plan, posted): once, never zero, never twice."""

    GOAL = {"name": "Savings", "target": 10_000.0, "monthly_plan": 250.0,
            "account_id": "sav", "tokens": [], "start_balance": 0.0}

    def setUp(self):
        super().setUp()
        write_config(self.conn, savings_goals=[dict(self.GOAL)])
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('sav','it1','Test Savings',"
            "'depository','savings',0) ON CONFLICT (tenant_id, id) DO NOTHING")

    def _post_transfer(self, amount):
        # both legs, as an aggregator reports them: destination-side inflow
        # (what savings._matched_net measures) + checking-side outflow
        # (already excluded from spend; 'sav' is depository, so the
        # credit pairing can't claim it either)
        add_txn(self.conn, self.ms, -amount, "TRANSFER TO SAVINGS",
                account="sav", primary="TRANSFER_IN")
        add_txn(self.conn, self.ms, amount, "TRANSFER TO SAVINGS",
                account="chk", primary="TRANSFER_OUT")

    def _plan_amounts(self, fc):
        return [a for _, a, p in fc["events"] if "(savings plan)" in p]

    def test_posted_transfer_counts_once_not_zero(self):
        """Plan fully posted mid-month. The walk schedules nothing more
        this month, so the MTD term is the only place the posted $250 can
        land — miss it there and it lands nowhere."""
        self._post_transfer(250.0)
        fwd, fc = self._fwd_and_forecast()
        # no remainder event — only whole future-month plans in the walk
        self.assertTrue(all(a == -250.0 for a in self._plan_amounts(fc)))
        self.assertAlmostEqual(fwd, self._expect(fc, self.INCOME - 250.0),
                               places=1)

    def test_partial_posting_splits_between_mtd_and_walk(self):
        """$100 posted of a $250 plan: MTD carries the posted −100, the
        walk schedules the −150 remainder — once each, $250 total."""
        self._post_transfer(100.0)
        fwd, fc = self._fwd_and_forecast()
        self.assertIn(-150.0, self._plan_amounts(fc))
        self.assertAlmostEqual(fwd, self._expect(fc, self.INCOME - 100.0),
                               places=1)

    def test_plan_only_goal_leaves_the_mtd_term_alone(self):
        """A plan-only goal has no ledger to observe — the walk
        schedules the full plan and the MTD term must not invent a posted
        amount (that would count the plan twice)."""
        write_config(self.conn, savings_goals=[
            {"name": "Savings", "monthly_plan": 250.0}])
        fwd, fc = self._fwd_and_forecast()
        self.assertTrue(all(a == -250.0 for a in self._plan_amounts(fc)))
        self.assertAlmostEqual(fwd, self._expect(fc, self.INCOME), places=1)


class YearLensSavedYtdTests(unittest.TestCase):
    """The Year lens payload carries `saved_ytd` — the ledger's net into
    the standing "Savings" goal (the money-map row's key) over the lens
    year's elapsed span — so the SPA can stop extrapolating the row's
    actual as rate_90d × elapsed months. None when there is no measurable
    goal (absent or plan-only); the SPA then falls back."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, savings_goals=[
            {"name": "Savings", "target": 10_000.0, "monthly_plan": 250.0,
             "account_id": "sav", "tokens": [], "start_balance": 1000.0}])
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('sav','it1','Test Savings',"
            "'depository','savings',0) ON CONFLICT (tenant_id, id) DO NOTHING")
        # this year: +500 (Feb), +300 (May), −100 back out (Jun)
        add_txn(self.conn, dt.date(2026, 2, 10), -500.0, "TO SAVINGS",
                account="sav", primary="TRANSFER_IN")
        add_txn(self.conn, dt.date(2026, 5, 3), -300.0, "TO SAVINGS",
                account="sav", primary="TRANSFER_IN")
        add_txn(self.conn, dt.date(2026, 6, 20), 100.0, "FROM SAVINGS",
                account="sav", primary="TRANSFER_OUT")
        # last year — a different lens year's figure
        add_txn(self.conn, dt.date(2025, 12, 5), -999.0, "TO SAVINGS",
                account="sav", primary="TRANSFER_IN")

    def tearDown(self):
        self.conn.close()

    def _year(self, y):
        from oikonome.web import lenses
        return lenses.year_summary(self.conn, y, today=TODAY)

    def test_saved_ytd_is_the_ledger_net_not_an_extrapolation(self):
        """500 + 300 − 100 = 700 net this year — start_balance and other
        years stay out (contributions, not the goal's running balance)."""
        self.assertEqual(self._year(2026)["saved_ytd"], 700.0)

    def test_prior_year_is_scoped_to_that_year(self):
        self.assertEqual(self._year(2025)["saved_ytd"], 999.0)

    def test_plan_only_goal_reports_none(self):
        """Plan-only: no ledger to measure — None, and the SPA keeps
        its extrapolation fallback."""
        write_config(self.conn, savings_goals=[
            {"name": "Savings", "monthly_plan": 250.0}])
        self.assertIsNone(self._year(2026)["saved_ytd"])

    def test_no_standing_savings_goal_reports_none(self):
        write_config(self.conn, savings_goals=[])
        self.assertIsNone(self._year(2026)["saved_ytd"])

    def test_saved_ytd_counts_checking_leg_when_dest_is_silent(self):
        """Dest account exists but has no TRANSFER rows — the month
        lens already counts checking-leg posted_for_plan; year must
        sum those same windows, not paint $0 next to $500 months."""
        write_config(self.conn, savings_goals=[
            {"name": "Savings", "target": 10_000.0, "monthly_plan": 500.0,
             "account_id": "sav", "tokens": [], "start_balance": 0}])
        # wipe dest-side transfers from setUp so dest is silent
        self.conn.execute(
            "DELETE FROM transactions WHERE account_id='sav'")
        add_txn(self.conn, dt.date(2026, 1, 15), 500.0, "TO SAVINGS",
                account="chk", primary="TRANSFER_OUT")
        add_txn(self.conn, dt.date(2026, 2, 15), 500.0, "TO SAVINGS",
                account="chk", primary="TRANSFER_OUT")
        self.assertEqual(self._year(2026)["saved_ytd"], 1000.0)


class RetirementDebtResidualTests(unittest.TestCase):
    """current_buckets nets card debt in the sim's draw order — cash →
    taxable → tax-deferred → Roth — and whatever remains is carried as
    `debt_residual`, subtracted from `total`. Netting that stops (floors)
    at taxable makes debt beyond cash+taxable silently vanish, and every
    projection then starts from an optimistic portfolio."""

    def setUp(self):
        self.conn = make_db()          # fixture: chk $5,000 / card $250
        write_config(self.conn)
        for aid, item, sub, bal in (("k401", "it-401k", "401k", 10_000.0),
                                    ("roth", "it-roth", "roth", 5_000.0)):
            self.conn.execute(
                "INSERT INTO items (id, aggregator, institution_name) "
                "VALUES (%s,'test',%s) ON CONFLICT (tenant_id, id) DO NOTHING",
                (item, item))
            self.conn.execute(
                """INSERT INTO accounts (id, item_id, name, type, subtype,
                                         balance_current)
                   VALUES (%s,%s,%s,'investment',%s,%s)
                   ON CONFLICT (tenant_id, id) DO NOTHING""",
                (aid, item, aid, sub, bal))

    def tearDown(self):
        self.conn.close()

    def _debt(self, amt):
        self.conn.execute(
            "UPDATE accounts SET balance_current=%s WHERE id='card'", (amt,))
        return retirement.current_buckets(self.conn)

    def test_debt_beyond_cash_and_taxable_spills_into_td_then_roth(self):
        """$20k debt vs $5k cash, $0 taxable — the $15k excess must come
        out of the portfolio, not vanish. Floored at taxable the total reads
        $15,000; the honest figure is $0."""
        b = self._debt(20_000.0)
        self.assertEqual((b["cash"], b["taxable"]), (0.0, 0.0))
        self.assertEqual((b["td"], b["roth"]), (0.0, 0.0))
        self.assertEqual(b["debt_residual"], 0.0)
        self.assertEqual(b["total"], 0.0)

    def test_partial_spill_stops_in_draw_order(self):
        b = self._debt(12_000.0)                    # spill 7k: td 10k → 3k
        self.assertEqual((b["cash"], b["taxable"]), (0.0, 0.0))
        self.assertEqual((b["td"], b["roth"]), (3_000.0, 5_000.0))
        self.assertEqual(b["total"], 8_000.0)

    def test_insolvent_debt_is_carried_not_dropped(self):
        """Debt beyond EVERYTHING: buckets zero out (the sim can't draw
        negative) but the residual is carried and the total stays honest."""
        b = self._debt(25_000.0)
        self.assertEqual((b["td"], b["roth"]), (0.0, 0.0))
        self.assertEqual(b["debt_residual"], 5_000.0)
        self.assertEqual(b["total"], -5_000.0)

    def test_ordinary_debt_within_cash_is_unchanged(self):
        b = self._debt(250.0)
        self.assertEqual(b["cash"], 4_750.0)
        self.assertEqual((b["td"], b["roth"]), (10_000.0, 5_000.0))
        self.assertEqual(b["debt_residual"], 0.0)
        self.assertEqual(b["total"], 19_750.0)


class ManualCardPaymentBillTests(unittest.TestCase):
    """A manually-tracked "card payment" recurring bill must not
    double-reserve against the live card machinery: Today headroom's
    required is card_debt + due_total (the bill would count the payment a
    second time) and the forecast walks BOTH the bill occurrence and the
    per-card payoff event. Such a bill can never occurrence-match its
    payment either — _spend_rows excludes card payments — so it also
    re-dates "overdue" forever. upcoming_bill_occurrences (the one rule
    every cash surface reads) skips card-payment-shaped bills while live
    card debt exists for the scenarios to pay."""

    def setUp(self):
        self.conn = make_db()          # fixture: chk $5,000 / card $250
        write_config(self.conn)
        add_bill(self.conn, "Chase Credit Card", 250.0,
                 next_due=dt.date(2026, 7, 25),
                 last_seen=dt.date(2026, 6, 25))

    def tearDown(self):
        self.conn.close()

    def _upcoming(self):
        cfg = budget.load_config(self.conn)
        return [b["payee"] for b in budget.upcoming_bill_occurrences(
            self.conn, cfg, TODAY, TODAY + dt.timedelta(days=30))]

    def test_card_payment_bill_is_skipped_while_live_debt_exists(self):
        """The $250 card balance is already reserved by headroom's
        card_debt term and the walk's payoff event — the bill on top would
        reserve the same payment twice."""
        self.assertNotIn("Chase Credit Card", self._upcoming())

    def test_forecast_carries_the_payoff_exactly_once(self):
        fc = forecast.build(self.conn, today=TODAY, days=30)
        self.assertAlmostEqual(
            sum(a for _, a, _ in fc["card_events_stmt"]), -250.0, places=2)
        self.assertEqual(
            [p for _, _, p in fc["events"] if "Chase" in p], [])

    def test_issuer_autopay_names_are_skipped_too(self):
        """flow_match's tight seed regex misses issuer+AUTOPAY payees
        ("DISCOVER AUTOPAY", "VISA AUTOPAY") that the importers' own detector
        (flowmap.looks_like_card_payment) reads as card payments — on that
        detector alone a bill named that way slips past the skip and
        double-reserves with the live card debt."""
        add_bill(self.conn, "DISCOVER AUTOPAY", 100.0,
                 next_due=dt.date(2026, 7, 22),
                 last_seen=dt.date(2026, 6, 22))
        add_bill(self.conn, "VISA AUTOPAY", 80.0,
                 next_due=dt.date(2026, 7, 23),
                 last_seen=dt.date(2026, 6, 23))
        up = self._upcoming()
        self.assertNotIn("DISCOVER AUTOPAY", up)
        self.assertNotIn("VISA AUTOPAY", up)

    def test_issuer_autopay_bill_stays_when_no_live_card_debt(self):
        """Same no-live-debt rule as flow_match names: the bill is the
        only signal an unlinked card exists, so it keeps reserving."""
        add_bill(self.conn, "DISCOVER AUTOPAY", 100.0,
                 next_due=dt.date(2026, 7, 22),
                 last_seen=dt.date(2026, 6, 22))
        self.conn.execute(
            "UPDATE accounts SET balance_current=0 WHERE id='card'")
        self.assertIn("DISCOVER AUTOPAY", self._upcoming())

    def test_bill_stays_when_no_live_card_debt(self):
        """With no live card balance (an unlinked card tracked by hand) the
        bill is the only signal the payment exists — it must keep
        reserving."""
        self.conn.execute(
            "UPDATE accounts SET balance_current=0 WHERE id='card'")
        self.assertIn("Chase Credit Card", self._upcoming())

    def test_month_verdict_drops_the_bill_too(self):
        """The verdict loads bills through the same filter: a card-payment
        bill can never match its payment (spend rows exclude card
        payments), so left in it sits overdue forever and its planned
        amount inflates the fixed month, shrinking the dynamic variable
        budgets every other surface then burns at."""
        st = budget.month_status(self.conn, TODAY)
        self.assertEqual([o[0] for o in st["overdue_unpaid"]], [])
        self.assertAlmostEqual(st["buckets"]["fixed"]["month_budget"], 0.0,
                               places=2)

    def test_month_verdict_keeps_the_bill_without_live_debt(self):
        self.conn.execute(
            "UPDATE accounts SET balance_current=0 WHERE id='card'")
        st = budget.month_status(self.conn, TODAY)
        self.assertAlmostEqual(st["buckets"]["fixed"]["month_budget"], 250.0,
                               places=2)

    def test_ordinary_bills_are_untouched(self):
        add_bill(self.conn, "Rent Payment", 1200.0,
                 next_due=dt.date(2026, 7, 28),
                 last_seen=dt.date(2026, 6, 28))
        self.assertIn("Rent Payment", self._upcoming())


if __name__ == "__main__":
    unittest.main()
