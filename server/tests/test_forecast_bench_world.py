"""The forecast benchmark's simulated households are consistent with
themselves (specs/forecast-benchmark.md, "The simulator").

Pure: no database. A benchmark is only as good as its ground truth, so the
truth must reconcile with the ledger to the cent, card statements must add
up, every card payment must leave one account and reach the other, money
moved by ACH must land on business days, the same seed must give the same
household, and each persona must really behave as its name says — the
revolver carries a balance, the sweeper sweeps, the hand payer pays on
paydays. The feed must serve that world the way Plaid serves a bank.
"""

import datetime as dt
import json
import statistics
import unittest

import httpx

from .forecast_bench import feed, personas, world

DAY = dt.timedelta(days=1)
_WORLDS: dict = {}


def sim(key, seed=1):
    if (key, seed) not in _WORLDS:
        _WORLDS[(key, seed)] = world.simulate(key, seed)
    return _WORLDS[(key, seed)]


def days(w):
    d = w.start
    while d <= w.end:
        yield d
        d += DAY


class TheTruthAddsUp(unittest.TestCase):
    def test_checking_truth_is_the_opening_plus_every_settled_row(self):
        for key in personas.PERSONAS:
            w = sim(key)
            rows = [t for t in w.ledger if t.account == "chk"]
            for d in days(w):
                # a debit counts from the day it was made, a credit from
                # the day it posted (the bank's available balance)
                cents = round(w.persona.checking_open * 100) - sum(
                    round(t.amount * 100) for t in rows
                    if (t.date if t.amount > 0 else t.post) <= d)
                self.assertEqual(round(w.checking[d] * 100), cents,
                                 f"{key} {d}")

    def test_every_account_posted_balance_is_its_posted_rows(self):
        for key in personas.PERSONAS:
            w = sim(key)
            for a in w.accounts:
                aid = a["id"]
                rows = [t for t in w.ledger if t.account == aid]
                card = a["type"] == "credit"
                opening = (next(c["open"] for c in w.persona.cards
                                if c["id"] == aid) if card else
                           {"chk": w.persona.checking_open,
                            "chk2": w.persona.chk2_open,
                            "sav": w.persona.savings_open}[aid])
                sign = 1 if card else -1
                d = w.end
                cents = round(opening * 100) + sign * sum(
                    round(t.amount * 100) for t in rows if t.post <= d)
                self.assertEqual(round(w.posted[aid][d] * 100), cents,
                                 f"{key} {aid}")

    def test_every_statement_is_carried_plus_its_cycle(self):
        for key in personas.PERSONAS:
            w = sim(key)
            for cid, stmts in w.statements.items():
                rows = [t for t in w.ledger if t.account == cid]
                prev_close = w.start - DAY
                prev_bal = next(c["open"] for c in w.persona.cards
                                if c["id"] == cid)
                for s in stmts:
                    cycle = sum(round(t.amount * 100) for t in rows
                                if prev_close < t.post <= s.close)
                    self.assertEqual(round(s.balance * 100),
                                     round(prev_bal * 100) + cycle,
                                     f"{key} {cid} {s.close}")
                    self.assertEqual(s.due - s.close, dt.timedelta(
                        days=next(c["grace"] for c in w.persona.cards
                                  if c["id"] == cid)))
                    prev_close, prev_bal = s.close, s.balance

    def test_every_card_payment_leaves_one_account_and_reaches_the_card(self):
        for key in personas.PERSONAS:
            w = sim(key)
            by_id = {t.id: t for t in w.ledger}
            pays = [t for t in w.ledger if t.kind == "card_payment"]
            for t in pays:
                other = by_id[t.pair]
                self.assertEqual(other.pair, t.id)
                self.assertAlmostEqual(t.amount, -other.amount, places=2)
                card, bank = (t, other) if t.amount < 0 else (other, t)
                self.assertTrue(card.account.startswith("card"), key)
                self.assertIn(bank.account, ("chk", "chk2"), key)
                self.assertEqual(card.detailed,
                                 "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
                self.assertEqual(bank.detailed, card.detailed)
            if w.persona.cards:
                self.assertTrue(pays, f"{key} never pays a card")

    def test_ach_money_lands_on_business_days(self):
        for key in personas.PERSONAS:
            w = sim(key)
            cal = w.persona.calendar
            for t in w.ledger:
                if t.account in ("chk", "chk2") and t.kind in (
                        "bill", "paycheck"):
                    self.assertTrue(world.business_day(t.date, cal),
                                    f"{key} {t.name} on {t.date}")

    def test_a_pending_row_posts_later_under_its_own_id(self):
        w = sim("biweekly_hand_payer")
        lagged = [t for t in w.ledger if t.post > t.date]
        self.assertTrue(lagged)
        for t in w.ledger:
            self.assertGreaterEqual(t.post, t.date)
            self.assertEqual(t.pending_id is not None, t.post > t.date)


class TheSameSeedIsTheSameHousehold(unittest.TestCase):
    def test_a_seed_gives_the_same_world_twice_and_another_seed_another(self):
        def fp(w):
            return ([(t.id, t.date, t.post, t.amount, t.name, t.account)
                     for t in w.ledger],
                    sorted(w.checking.items()))
        for key in ("monthly_autopay", "gig_weekly"):
            a, b = world.simulate(key, 7), world.simulate(key, 7)
            self.assertEqual(fp(a), fp(b))
            self.assertNotEqual(fp(a), fp(world.simulate(key, 8)))


def _payer_rows(w, payer):
    return [t for t in w.ledger if t.kind == "paycheck"
            and t.series == payer]


def _cv(xs):
    return statistics.pstdev(xs) / statistics.mean(xs)


class EachPersonaDoesWhatItsNameSays(unittest.TestCase):
    def test_the_revolver_carries_a_balance_and_pays_interest(self):
        w = sim("revolver_minimum")
        for cid, stmts in w.statements.items():
            self.assertTrue(all(s.balance > 3000 for s in stmts), cid)
        paid = [-t.amount for t in w.ledger if t.kind == "card_payment"
                and t.amount < 0]
        self.assertLess(max(paid), 1000)
        self.assertTrue(any(t.kind == "interest" for t in w.ledger))

    def test_the_sweeper_moves_money_to_savings_every_payday(self):
        w = sim("payday_sweep")
        paydays = {t.date for t in _payer_rows(w, "Brightline Payroll")}
        swept = {t.date for t in w.ledger if t.account == "chk"
                 and t.detailed == "TRANSFER_OUT_SAVINGS"}
        self.assertLessEqual(paydays, swept)
        self.assertTrue(any(t.date.day == 1 for t in w.ledger
                            if t.detailed == "TRANSFER_OUT_SAVINGS"))

    def test_debit_only_has_no_card_and_withdraws_cash(self):
        w = sim("debit_only")
        self.assertFalse(any(a["type"] == "credit" for a in w.accounts))
        self.assertTrue(any(t.kind == "cash" for t in w.ledger))

    def test_the_hand_payer_pays_whole_balances_on_paydays(self):
        w = sim("biweekly_hand_payer")
        paydays = {t.date for t in _payer_rows(w, "Brightline Payroll")}
        credits = [t for t in w.ledger if t.kind == "card_payment"
                   and t.amount < 0]
        self.assertTrue(credits)
        for t in credits:
            self.assertIn(t.date, paydays)
            # the whole posted balance as it stood the night before, at
            # least (charges that posted that morning ride along)
            self.assertGreaterEqual(-t.amount + 0.01,
                                    w.posted[t.account][t.date - DAY])

    def test_autopay_pays_the_statement_on_its_due_date(self):
        w = sim("monthly_autopay")
        for cid, stmts in w.statements.items():
            for s in stmts:
                if s.balance <= 0 or s.due > w.end:
                    continue
                pay = [t for t in w.ledger if t.account == cid
                       and t.kind == "card_payment" and t.date == s.due]
                self.assertEqual(len(pay), 1, f"{cid} {s.due}")
                self.assertAlmostEqual(-pay[0].amount, s.balance, places=2)

    def test_two_earners_are_paid_biweekly_and_semimonthly(self):
        w = sim("two_earners")
        semi = _payer_rows(w, "Copperleaf Clinic Payroll")
        noms = {o.nominal.day for s in w.schedule
                if s.payee == "Copperleaf Clinic Payroll"
                for o in s.occurrences}
        self.assertLessEqual(noms, {15, 28, 29, 30, 31})
        self.assertGreater(len(semi), 25)

    def test_gig_income_is_lumpy_with_dry_weeks(self):
        w = sim("gig_weekly")
        for payer in ("Quickcart Payouts", "Ridealong Driver Pay",
                      "Taskhive Earnings"):
            rows = _payer_rows(w, payer)
            self.assertGreater(_cv([-t.amount for t in rows]), 0.25)
            weeks = (w.end - w.start).days // 7
            self.assertLess(len(rows), weeks)
            self.assertFalse(rows[0].scheduled)

    def test_hourly_checks_vary(self):
        rows = _payer_rows(sim("hourly_variable"), "Brightline Payroll")
        self.assertGreater(_cv([-t.amount for t in rows]), 0.08)

    def test_the_quarterly_bonus_lands_in_quarter_months(self):
        rows = _payer_rows(sim("commission_quarterly"), "Brightline Bonus")
        self.assertTrue(rows)
        self.assertTrue(all(t.date.month in (3, 6, 9, 12) for t in rows))

    def test_the_retiree_benefit_follows_its_weekday_rule(self):
        w = sim("retiree")
        for s in w.schedule:
            if s.payee == "Retirement Benefit Deposit":
                for o in s.occurrences:
                    self.assertEqual(o.nominal.weekday(), 2)
                    self.assertTrue(8 <= o.nominal.day <= 14)

    def test_the_student_hovers_near_zero_and_pays_overdraft_fees(self):
        fees = 0
        for seed in (1, 2, 3):
            w = sim("student_near_zero", seed)
            self.assertLess(statistics.median(w.checking.values()), 1500)
            fees += sum(1 for t in w.ledger if t.kind == "fee")
        self.assertGreater(fees, 0)

    def test_one_rent_is_often_late(self):
        w = sim("landlord")
        late = [o for s in w.schedule if s.payee == "Unit B rent"
                for o in s.occurrences if (o.day - o.nominal).days > 1]
        self.assertGreaterEqual(len(late), 3)

    def test_heating_doubles_in_winter(self):
        w = sim("seasonal_utilities")
        gas = {o.nominal.month: o.amount for s in w.schedule
               if s.payee == "Hollow Creek Gas" for o in s.occurrences}
        self.assertGreater(gas[1], 1.6 * gas[7])

    def test_annual_and_semiannual_bills_come_once_and_twice_a_year(self):
        w = sim("annual_bills")
        for s in w.schedule:
            if s.cadence in ("annual", "semiannual"):
                per = 1 if s.cadence == "annual" else 2
                first = [o for o in s.occurrences
                         if o.nominal < w.start + dt.timedelta(days=365)]
                self.assertEqual(len(first), per, s.payee)

    def test_the_job_change_leaves_a_three_week_gap(self):
        w = sim("job_change")
        a = _payer_rows(w, "Brightline Payroll")
        b = _payer_rows(w, "Northgate Works Payroll")
        self.assertGreaterEqual((b[0].date - a[-1].date).days, 21)

    def test_two_checking_pays_bills_from_the_second_account(self):
        w = sim("two_checking")
        bills = [t for t in w.ledger if t.kind == "bill"]
        self.assertTrue(all(t.account == "chk2" for t in bills))
        self.assertTrue(any(t.account == "chk" and t.primary == "TRANSFER_OUT"
                            for t in w.ledger))

    def test_the_non_us_calendar_is_not_the_us_one(self):
        self.assertTrue(world.business_day(dt.date(2025, 7, 4), "other"))
        self.assertFalse(world.business_day(dt.date(2025, 7, 4), "us"))
        self.assertFalse(world.business_day(dt.date(2025, 5, 1), "other"))

    def test_a_bill_is_missed_once_and_paid_late_with_a_fee(self):
        for key in ("biweekly_hand_payer", "debit_only"):
            w = sim(key)
            doubled = [o for s in w.schedule if s.kind == "bill"
                       for o in s.occurrences
                       if o.amount > 1.8 * statistics.median(
                           x.amount for x in s.occurrences)]
            self.assertEqual(len(doubled), 1, key)

    def test_a_card_charge_is_refunded_weeks_later(self):
        w = sim("monthly_autopay")
        refunds = [t for t in w.ledger if t.kind == "refund"]
        self.assertEqual(len(refunds), 1)
        self.assertLess(refunds[0].amount, 0)

    def test_the_counterfactual_is_the_truth_until_a_reaction(self):
        w = sim("debit_only")
        self.assertTrue(w.reactions)
        first = w.reactions[0][0]
        d0 = first - DAY
        cf = w.counterfactual(d0)
        self.assertTrue(all(cf[d] == w.checking[d] for d in days(w)
                            if d <= first))
        self.assertTrue(any(cf[d] != w.checking[d] for d in days(w)
                            if d > first))


class TheFeedServesTheWorldAsPlaidWould(unittest.TestCase):
    def _post(self, f, path, body=None):
        r = f.handle(httpx.Request("POST", f"https://sim{path}",
                                   content=json.dumps(body or {})))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_a_pending_row_is_removed_and_returns_posted(self):
        w = sim("monthly_autopay")
        f = feed.SimPlaid(w)
        t = next(x for x in w.ledger if x.post - x.date == 2 * DAY
                 and x.date > w.start + dt.timedelta(days=30))
        f.day = t.date
        got, cur = [], None
        while True:
            page = self._post(f, "/transactions/sync",
                              {"cursor": cur} if cur else {})
            got += page["added"]
            cur = page["next_cursor"]
            if not page["has_more"]:
                break
        pend = [r for r in got if r["transaction_id"] == t.pending_id]
        self.assertEqual(len(pend), 1)
        self.assertTrue(pend[0]["pending"])
        f.day = t.post
        page = self._post(f, "/transactions/sync", {"cursor": cur})
        self.assertIn({"transaction_id": t.pending_id}, page["removed"])
        posted = [r for r in page["added"] if r["transaction_id"] == t.id]
        self.assertEqual(posted[0]["pending_transaction_id"], t.pending_id)
        self.assertFalse(posted[0]["pending"])

    def test_balances_and_liabilities_are_the_days_truth(self):
        w = sim("monthly_autopay")
        f = feed.SimPlaid(w)
        f.day = dt.date(2025, 6, 20)
        acc = {a["account_id"]: a for a in
               self._post(f, "/accounts/get")["accounts"]}
        self.assertEqual(acc["chk"]["balances"]["available"],
                         round(w.checking[f.day], 2))
        lia = self._post(f, "/liabilities/get")["liabilities"]["credit"]
        for row in lia:
            last = [s for s in w.statements[row["account_id"]]
                    if s.close < f.day][-1]
            self.assertEqual(row["last_statement_balance"], last.balance)
            # a due date already passed reads as the next one
            self.assertGreaterEqual(row["next_payment_due_date"],
                                    f.day.isoformat()
                                    if last.due < f.day else
                                    last.due.isoformat())

    def test_a_household_that_linked_late_sees_nothing_before_the_link(self):
        w = sim("new_household")
        link = dt.date(2025, 10, 18)
        f = feed.SimPlaid(w, link=link)
        rows = f.view(dt.date(2025, 11, 1)).values()
        self.assertTrue(rows)
        self.assertTrue(all(r["authorized_date"] >= link.isoformat()
                            for r in rows))


if __name__ == "__main__":
    unittest.main()
