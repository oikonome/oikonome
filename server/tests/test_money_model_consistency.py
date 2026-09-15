"""One money model per point: cash-basis consistency across the engine.

* reporting._cash_graph must not mix two money models in the current
  month's point: an ECONOMIC month-to-date (income − spend, where spend
  counts card purchases at charge time) added to a CASH forecast delta
  (the pace_stmt walk, which pays the same card balance off at its
  autopay due date) shows a $300 unpaid card charge as −$600. The
  current month is cash-basis end to end.

* forecast.build schedules the monthly savings plan bill-like: whatever
  the ledger has not seen posted to the goal this month lands at
  max(1st, tomorrow). A `today < d` guard with d = the 1st would never
  schedule the CURRENT month's transfer, because today >= the 1st
  always.

* reporting._reconstruct_trend's cash_baseline must exclude linked
  shadows, or a dual-sourced checking account doubles the whole trend
  baseline.

* the awaiting-reimbursement alert follows the remaining-balance
  partial-receipt model: the outstanding amount is
  COALESCE(expected, amount) − received-so-far, floored at 0.

* the occurrence match window caps at 35 days, so a long-cycle bill
  prepaid weeks early is not reserved twice. Monthly bills keep their
  half-cycle (15-day) window, so the early-payment decoy cases are
  structurally unchanged.

* savings-goal progress (_matched_net) carries the shadow filter, or a
  token-matched goal on a dual-sourced savings account counts every
  transfer once per linked source.
"""

import datetime as dt
import unittest

from oikonome.engine import alerts, budget, forecast, links, reporting, savings

from .util import TODAY, add_bill, add_txn, make_db, write_config


def _mk_account(conn, acct_id, item_id, *, typ, subtype, bal, name="Acct"):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_name) "
        "VALUES (%s,%s,%s) ON CONFLICT (tenant_id, id) DO NOTHING",
        (item_id, "test", item_id))
    conn.execute(
        """INSERT INTO accounts (id, item_id, name, type, subtype, mask,
                                 balance_current, updated_at)
           VALUES (%s,%s,%s,%s,%s,'0000',%s, now())
           ON CONFLICT (tenant_id, id) DO NOTHING""",
        (acct_id, item_id, name, typ, subtype, bal))


class CashGraphCurrentMonthTests(unittest.TestCase):
    """The current month's cash-graph point is CASH-basis end to end.

    INVARIANT: every card dollar reaches the current month's
    point exactly once — through the posted payment if the card was already
    paid this month, or through the forecast walk's scheduled autopay if it
    is still owed. The charge itself never contributes on top of that.
    Mixing models double-counts: an unpaid $300 charge would contribute
    −300 through an economic MTD (income − spend, spend counting card
    purchases at charge time) AND −300 again when the pace_stmt walk pays
    the card off — −$600 for one charge.

    _cash_graph uses the real dt.date.today(), so these tests do too; the
    assertions are algebra over the same forecast series the graph folds
    in, which keeps them month-boundary safe.
    """

    INCOME = 4000.0

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.today = dt.date.today()
        self.ms = self.today.replace(day=1)
        # bank income this month (what reporting._income_by counts)
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

    def test_unpaid_card_charge_counts_once(self):
        """THE CASE: $300 charged this month, still on the card. The walk
        schedules the payoff (once); the MTD term must carry income only."""
        self.conn.execute(
            "UPDATE accounts SET balance_current=300 WHERE id='card'")
        add_txn(self.conn, self.ms, 300.0, "STEREO SHOP", account="card")
        fwd, fc = self._fwd_and_forecast()
        # the payoff IS in the walk — 'once', not 'zero'
        self.assertAlmostEqual(
            sum(a for _, a, _ in fc["card_events_stmt"]), -300.0, places=2)
        self.assertAlmostEqual(fwd, self._expect(fc, self.INCOME), places=1)

    def test_charge_paid_this_month_counts_once_via_the_payment(self):
        """Charge AND payment both this month: the cash already left
        checking, so the payment is the (single) contribution — dropping
        card spend without adding the posted payment back would
        double-EXCLUDE it."""
        self.conn.execute(
            "UPDATE accounts SET balance_current=0 WHERE id='card'")
        add_txn(self.conn, self.ms, 300.0, "STEREO SHOP", account="card")
        add_txn(self.conn, self.ms, 300.0, "CARD AUTOPAY", account="chk",
                primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        fwd, fc = self._fwd_and_forecast()
        self.assertEqual(fc["card_events_stmt"], [])   # nothing left to pay
        self.assertAlmostEqual(fwd, self._expect(fc, self.INCOME - 300.0),
                               places=1)

    def test_checking_debit_spend_still_counts(self):
        """A debit purchase is cash out the moment it posts — it stays in
        the MTD term."""
        self.conn.execute(
            "UPDATE accounts SET balance_current=0 WHERE id='card'")
        add_txn(self.conn, self.ms, 120.0, "GROCERY MART", account="chk",
                primary="FOOD_AND_DRINK")
        fwd, fc = self._fwd_and_forecast()
        self.assertAlmostEqual(fwd, self._expect(fc, self.INCOME - 120.0),
                               places=1)


class ForecastCurrentMonthSavingsPlanTests(unittest.TestCase):
    """forecast.build schedules the CURRENT month's savings-plan transfer
    bill-like: if the ledger hasn't seen it posted to the goal yet, it lands
    at max(1st, tomorrow); once posted it is never double-scheduled. A bare
    `today < d` guard with d = the 1st skips the current month always."""

    GOAL = {"name": "trip fund", "target": 3000.0, "monthly_plan": 250.0,
            "account_id": "sav", "tokens": [], "start_balance": 0.0}

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, savings_goals=[dict(self.GOAL)])
        self.conn.execute(
            "INSERT INTO accounts (id,item_id,name,type,subtype,"
            "balance_current) VALUES ('sav','it1','Test Savings',"
            "'depository','savings',0) ON CONFLICT (tenant_id, id) DO NOTHING")

    def tearDown(self):
        self.conn.close()

    def _plan_events(self, days=60):
        f = forecast.build(self.conn, today=TODAY, days=days)
        return [(d, a) for d, a, p in f["events"]
                if p == "trip fund (savings plan)"]

    def test_unposted_plan_is_scheduled_tomorrow(self):
        """THE CASE: Jul 15, no transfer posted → July's plan outflow is
        in the forecast (at tomorrow, the soonest it can happen), plus each
        following 1st as before."""
        got = self._plan_events()
        self.assertEqual(got, [("2026-07-16", -250.0),
                               ("2026-08-01", -250.0),
                               ("2026-09-01", -250.0)])

    def test_posted_transfer_is_not_double_scheduled(self):
        """The transfer already left checking (it's in the live balance the
        walk starts from) — scheduling it again would double-count."""
        add_txn(self.conn, dt.date(2026, 7, 10), -250.0,
                "TRANSFER TO SAVINGS", primary="TRANSFER_IN", account="sav")
        got = self._plan_events()
        self.assertEqual(got, [("2026-08-01", -250.0),
                               ("2026-09-01", -250.0)])

    def test_partial_posting_schedules_the_remainder(self):
        add_txn(self.conn, dt.date(2026, 7, 10), -100.0,
                "TRANSFER TO SAVINGS", primary="TRANSFER_IN", account="sav")
        got = self._plan_events()
        self.assertEqual(got[0], ("2026-07-16", -150.0))

    def test_plan_only_goal_schedules_the_full_plan(self):
        """A plan-only goal (no account, no tokens) has no ledger to
        verify against — the conservative read is that the transfer is
        still owed, mirroring an unpaid bill."""
        write_config(self.conn, savings_goals=[
            {"name": "trip fund", "monthly_plan": 250.0}])
        got = self._plan_events()
        self.assertEqual(got[0], ("2026-07-16", -250.0))


class TrendShadowBaselineTests(unittest.TestCase):
    """_reconstruct_trend cash_baseline excludes linked shadow accounts."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        # an investment anchor so the trend has a real reconstruction
        # (without inv accounts it degenerates to a single point)
        _mk_account(self.conn, "inv", "inv-item", typ="investment",
                    subtype=None, bal=1000, name="Brokerage")
        self.conn.execute(
            "INSERT INTO holdings (account_id, symbol, quantity, price) "
            "VALUES ('inv','VTI',10,100)")
        two_months = (TODAY.replace(day=1) - dt.timedelta(days=35)).replace(day=1)
        add_txn(self.conn, two_months, 100.0, "BUY VTI", account="inv")

    def tearDown(self):
        self.conn.close()

    def _first_point(self):
        # today_total pinned on the LAST point only; earlier points expose
        # the baseline. Expected cash baseline: chk 5000 − card 250 = 4750,
        # inv layer 10 × $100 = 1000.
        trend = reporting._reconstruct_trend(self.conn, 5750.0)
        self.assertGreater(len(trend), 1)
        return trend[0][1]

    def test_dual_source_checking_counts_once(self):
        """THE CASE: the same real checking account through two sources,
        linked — the baseline must carry $5,000, not $10,000."""
        _mk_account(self.conn, "chk2", "it2", typ="depository",
                    subtype="checking", bal=5000, name="Checking (mirror)")
        links.create(self.conn, ["chk", "chk2"])
        self.assertAlmostEqual(self._first_point(), 5750.0, places=2)

    def test_shadow_investment_balance_stays_out_of_cash_baseline(self):
        """A linked shadow INVESTMENT account is excluded from inv_ids;
        otherwise its balance leaks into the CASH baseline instead."""
        _mk_account(self.conn, "inv2", "inv-item2", typ="investment",
                    subtype=None, bal=1000, name="Brokerage (mirror)")
        links.create(self.conn, ["inv", "inv2"])
        self.assertAlmostEqual(self._first_point(), 5750.0, places=2)

    def test_unlinked_accounts_all_count(self):
        """No links → nothing is a shadow; a genuine second checking
        account still counts (the exclusion must not eat real money)."""
        _mk_account(self.conn, "chk2", "it2", typ="depository",
                    subtype="checking", bal=3000, name="Other Checking")
        self.assertAlmostEqual(self._first_point(), 8750.0, places=2)


class ReimbOutstandingTests(unittest.TestCase):
    """alerts.reimb_pending reports what is STILL owed: expected (or the
    charge) minus partial receipts already recorded (the remaining-balance
    model) — not the charges' face value. Exercised through the real
    web.data flag/link paths so the model can't drift."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_partial_receipt_reduces_the_outstanding_total(self):
        """THE CASE: $400 flagged, $150 already came back → the alert
        owes $250, not $400."""
        from oikonome.web import data
        charge = add_txn(self.conn, TODAY - dt.timedelta(days=20), 400.0,
                         "HOTEL FOR WORK", account="chk")
        deposit = add_txn(self.conn, TODAY - dt.timedelta(days=5), -150.0,
                          "EMPLOYER EXPENSE CHECK", account="chk")
        data.flag_reimbursement(self.conn, charge)
        r = data.link_reimbursement(self.conn, charge, deposit, partial=True)
        self.assertNotIn("error", r)
        rp = alerts.reimb_pending(self.conn)
        self.assertEqual(rp["count"], 1)
        self.assertEqual(rp["total"], 250.0)
        self.assertEqual(rp["oldest"], TODAY - dt.timedelta(days=20))

    def test_expected_caps_the_outstanding(self):
        """A fraction-back expectation ($200 of a $500 charge) waits on the
        expected value, not the whole charge."""
        from oikonome.web import data
        charge = add_txn(self.conn, TODAY - dt.timedelta(days=8), 500.0,
                         "TEAM DINNER", account="chk")
        data.flag_reimbursement(self.conn, charge, partial=True,
                                expected=200.0)
        rp = alerts.reimb_pending(self.conn)
        self.assertEqual((rp["count"], rp["total"]), (1, 200.0))

    def test_fully_received_flag_never_alerts(self):
        """Belt and braces: even if a fully-covered flag survives (the link
        path normally deletes it), a $0-outstanding charge is not an alert."""
        charge = add_txn(self.conn, TODAY - dt.timedelta(days=8), 100.0,
                         "SUPPLIES", account="chk")
        self.conn.execute(
            "INSERT INTO reimburse_flags (txn_id) VALUES (%s)", (charge,))
        # the matching deposit has to exist: migration 067 gave
        # reimbursements real FKs to both sides of the pair
        dep = add_txn(self.conn, TODAY - dt.timedelta(days=7), -100.0,
                      "REIMBURSEMENT", account="chk")
        self.conn.execute(
            "INSERT INTO reimbursements (expense_id, reimburse_id, partial,"
            " amount) VALUES (%s,%s,1,100.0)", (charge, dep))
        self.assertIsNone(alerts.reimb_pending(self.conn))

    def test_no_flags_is_none(self):
        self.assertIsNone(alerts.reimb_pending(self.conn))


class PrepaidLongCycleBillTests(unittest.TestCase):
    """The occurrence match window was capped at 16 days, so a
    long-cycle (quarterly/annual) bill prepaid more than 16 days early was
    still reserved. The cap (and the pre-month-start payment lookback) is
    now 35 days. The window never exceeds HALF the bill's cycle, so monthly
    and shorter bills keep their 15/7/3-day windows — the decoy geometry
    (a prior-month payment can never be nearer to next month's occurrence
    than to its own) is structurally unchanged."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def upcoming(self, today, end):
        return [(b["payee"], b["due"])
                for b in budget.upcoming_bill_occurrences(
                    self.conn, budget.load_config(self.conn), today, end)]

    def test_quarterly_bill_prepaid_29_days_early_is_not_reserved(self):
        """Due Aug 10, paid Jul 12: the money already left checking, so
        the occurrence must not be reserved again. A 16-day match window
        would miss the payment and reserve $900 a second time."""
        add_bill(self.conn, "Quarterly Insurance", 900.0, frequency="MONTHLY",
                 interval=3, next_due=dt.date(2026, 8, 10),
                 last_seen=dt.date(2026, 5, 10))
        add_txn(self.conn, dt.date(2026, 7, 12), 900.0,
                "Quarterly Insurance", primary="GENERAL_SERVICES")
        self.assertEqual(self.upcoming(TODAY, dt.date(2026, 9, 15)), [])

    def test_lookback_covers_a_prepay_before_month_start(self):
        """Annual bill due Jul 10, paid Jun 10 — 30 days early, before
        the month start. The lookback must still match it; unmatched, it
        goes overdue-unpaid and is re-dated to tomorrow."""
        add_bill(self.conn, "Registration Annual", 400.0, frequency="YEARLY",
                 next_due=dt.date(2026, 7, 10),
                 last_seen=dt.date(2025, 7, 10))
        add_txn(self.conn, dt.date(2026, 6, 10), 400.0,
                "Registration Annual", primary="GENERAL_SERVICES")
        self.assertEqual(self.upcoming(TODAY, dt.date(2026, 8, 15)), [])

    def test_unpaid_long_cycle_bill_is_still_reserved(self):
        add_bill(self.conn, "Quarterly Insurance", 900.0, frequency="MONTHLY",
                 interval=3, next_due=dt.date(2026, 8, 10),
                 last_seen=dt.date(2026, 5, 10))
        self.assertEqual(self.upcoming(TODAY, dt.date(2026, 9, 15)),
                         [("Quarterly Insurance", dt.date(2026, 8, 10))])

    def test_monthly_decoy_case_unchanged(self):
        """The decoy world must survive the wider cap: a Jul 1 payment of
        JUNE's bill (due Jun 28, paid late) attaches to June's occurrence —
        July 28 stays reserved. Monthly windows are half-cycle (15d), not
        the 35-day cap."""
        add_bill(self.conn, "Fitness Club", 50.0,
                 next_due=dt.date(2026, 7, 28), last_seen=dt.date(2026, 6, 28))
        add_txn(self.conn, dt.date(2026, 7, 1), 50.0, "Fitness Club",
                primary="PERSONAL_CARE")
        self.assertEqual(self.upcoming(TODAY, dt.date(2026, 8, 10)),
                         [("Fitness Club", dt.date(2026, 7, 28))])


class GoalProgressShadowTests(unittest.TestCase):
    """Savings-goal progress excludes linked shadow accounts: without the
    filter a token-matched goal over a dual-sourced savings account counts
    every transfer once per linked source."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        for aid, item in (("sav1", "src-a"), ("sav2", "src-b")):
            _mk_account(self.conn, aid, item, typ="depository",
                        subtype="savings", bal=500, name="Trip Savings")
            # the same real-world transfer, reported by both sources
            add_txn(self.conn, TODAY - dt.timedelta(days=10), -500.0,
                    "TRIP FUND TRANSFER", primary="TRANSFER_IN", account=aid)

    def tearDown(self):
        self.conn.close()

    def _saved(self):
        cfg = {"savings_goals": [{"name": "trip", "target": 3000.0,
                                  "monthly_plan": 0.0, "tokens": ["trip"]}]}
        return savings.progress(self.conn, cfg, TODAY)[0]["saved"]

    def test_dual_source_transfer_counts_once(self):
        links.create(self.conn, ["sav1", "sav2"])
        self.assertEqual(self._saved(), 500.0)

    def test_unlinked_accounts_all_count(self):
        """No links → nothing is a shadow; two genuinely separate savings
        accounts both count."""
        self.assertEqual(self._saved(), 1000.0)


if __name__ == "__main__":
    unittest.main()
