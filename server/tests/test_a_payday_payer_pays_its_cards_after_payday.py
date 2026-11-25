"""A household that pays its cards right after payday is walked that way
in "Your usual" (specs/today-figures.md, "Your usual: after payday when
checking can cover it"): the business day after each payday every card is
paid in full when checking stays at or above the household's own cushion,
and each statement at its due date otherwise. Without the habit — or
without a paycheck to have it around — the measured-lag rule stands. Every
dollar still leaves checking once. Households are generated (tests/props.py
seeds); figures and names are invented."""

import datetime as dt
import random
import unittest

from oikonome.engine import business_days, forecast
from oikonome.engine.compat import jsonb
from oikonome.web import report, todayview

from . import props
from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date


def _month_days(day: int, months: int):
    y, m = TODAY.year, TODAY.month
    out = []
    for _ in range(months):
        y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        out.append(D(y, m, day))
    return sorted(out)


def household(seed: int, *, paychecks=True, earners=1, payer=True,
              checking=None, posted=None, payday=None):
    rng = random.Random(seed)
    conn = make_db()
    write_config(conn, food_monthly=900, other_monthly=900)
    chk = checking if checking is not None else rng.randint(300, 6000)
    conn.execute("UPDATE accounts SET balance_available=%s, "
                 "balance_current=%s WHERE id='chk'", (chk, chk))
    posted = posted or float(rng.randint(200, 1500))
    conn.execute("UPDATE accounts SET balance_current=%s WHERE id='card'",
                 (posted,))
    conn.execute(
        "INSERT INTO liabilities (account_id, raw) VALUES ('card', %s)",
        (jsonb({"last_statement_balance": round(posted * 0.6, 2),
                "last_statement_issue_date": "2026-07-08",
                "next_payment_due_date": "2026-08-03"}),))
    # nine months of card spending
    d = TODAY - dt.timedelta(days=270)
    while d < TODAY:
        if rng.random() < 0.7:
            add_txn(conn, d, float(rng.randint(10, 70)),
                    f"CORNER SHOP {rng.randint(1, 8)}")
        d += dt.timedelta(days=1)
    payees = [("NORTHSTAR PAYROLL", payday or 1),
              ("RIVERBEND CLINIC PAYROLL", 15)][:earners]
    pay_dates = []
    if paychecks:
        for name, day in payees:
            amount = float(rng.randint(1800, 3200))
            for p in _month_days(day, 9):
                # a direct deposit lands the business day before a
                # weekend or holiday payday
                if payday:
                    p = business_days.previous_business_day(p) if (
                        not business_days.is_business_day(p)) else p
                add_txn(conn, p, -amount, name, account="chk",
                        primary="INCOME", detailed="INCOME_WAGES")
                pay_dates.append(p)
            # the next: the 1st of September is a Tuesday; the 15th of
            # August a Saturday (walked in the Friday before)
            nxt = (D(2026, 8, 1) if day == 1 else D(2026, 7, 15)
                   if day == 15 else D(2026, 7, day))
            add_bill(conn, name, amount, income=True,
                     next_due=nxt if nxt > TODAY else D(2026, 8, day),
                     last_seen=_month_days(day, 1)[0])
    # card payments from checking: a day or two after payday, or on
    # unrelated days
    for p in sorted(pay_dates) or _month_days(9, 9):
        on = p + dt.timedelta(days=rng.randint(1, 2) if payer
                              else rng.randint(8, 12))
        if on >= TODAY:
            continue
        amt = float(rng.randint(300, 900))
        add_txn(conn, on, amt, "CARD PAYMENT", account="chk",
                primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        add_txn(conn, on, -amt, "PAYMENT THANK YOU",
                primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    return conn, {"checking": chk}


def _paid_after_payday(w):
    return [(d, a) for d, a, p in w["card_rows"]
            if p.endswith("paid after payday")]


class PaydayPayer(unittest.TestCase):
    def _build(self, **kw):
        conn, h = household(kw.pop("seed", 1), **kw)
        try:
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        return st, st["forecast"], h

    def test_the_habit_is_learned_from_the_households_own_payments(self):
        _st, f, _h = self._build()
        self.assertTrue(f["card_habit"]["payer"], f["card_habit"])
        self.assertGreaterEqual(f["card_habit"]["share"],
                                forecast.PAYDAY_HABIT_SHARE)
        _st, f, _h = self._build(payer=False)
        self.assertFalse(f["card_habit"]["payer"], f["card_habit"])
        # no paycheck series: no payday to pay around, the lag rule
        _st, f, _h = self._build(paychecks=False)
        self.assertFalse(f["card_habit"]["payer"])
        self.assertEqual(_paid_after_payday(f["pace_usual"]), [])

    def test_the_server_says_which_rule_it_used(self):
        st, _f, _h = self._build(checking=9000)
        ctx = todayview.build_context(st)
        # paid monthly, after the 1st's check: about once a month
        self.assertTrue(ctx["card_timing_note"].startswith(
            "You usually pay Test Card about once a month, after a payday "
            "when checking can cover it"), ctx["card_timing_note"])
        self.assertTrue(ctx["card_timing_note"].endswith(
            "; otherwise at the due date."))

    def test_an_affordable_payday_pays_every_card_the_next_business_day(self):
        _st, f, _h = self._build(checking=9000)
        rows = _paid_after_payday(f["pace_usual"])
        self.assertTrue(rows)
        landings = {D.fromisoformat(d) for d, a, _p in f["events"] if a > 0}
        cushion = f["card_habit"]["cushion"]
        series = dict(f["pace_usual"]["series"])
        for d, _a in rows:
            day = D.fromisoformat(d)
            self.assertIn(day, {business_days.add_business_days(p, 1)
                                for p in landings})
            self.assertGreaterEqual(series[d] + 0.01, cushion, d)

    def test_an_unaffordable_payday_waits_for_the_due_date(self):
        # a card balance no single paycheck covers
        _st, f, _h = self._build(checking=50, earners=1, posted=7000.0)
        stmt = [p for _d, _a, p in f["pace_usual"]["card_rows"]
                if p.endswith("statement due")]
        self.assertTrue(stmt, f["pace_usual"]["card_rows"])
        # no round is paid that would leave checking under the cushion,
        # and at least one payday passes without one
        series = dict(f["pace_usual"]["series"])
        rows = _paid_after_payday(f["pace_usual"])
        for d, _a in rows:
            self.assertGreaterEqual(series[d] + 0.01,
                                    f["card_habit"]["cushion"], d)
        landings = {D.fromisoformat(d) for d, a, _p in f["events"] if a > 0}
        paid = {D.fromisoformat(d) for d, _a in rows}
        self.assertTrue(any(business_days.add_business_days(p, 1) not in paid
                            for p in landings), (landings, paid))

    def test_a_holiday_payday_pays_the_business_day_after_it_lands(self):
        # the 15th of August is a Saturday; this household's deposits land
        # on the day itself (payday_habit), and its cards are paid the
        # business day after it: Monday the 17th
        _st, f, _h = self._build(checking=9000, earners=2)
        days = {d for d, _a in _paid_after_payday(f["pace_usual"])}
        self.assertIn("2026-08-17", days)


class TheHeavierLineKeepsTheRounds(unittest.TestCase):
    """A heavier month pays the cards on the same days the usual walk
    does — the rounds are decided once, on the usual walk — so it never
    sits above it: a heavier balance owed on payday is still paid then,
    not pushed past the chart by a cushion test it fails."""

    def test_a_heavier_month_never_skips_a_round_the_usual_walk_pays(self):
        from unittest import mock
        for seed, chk in ((1, 500), (3, 500), (3, 1500)):
            conn, _h = household(seed, checking=chk, posted=2500.0)
            try:
                with mock.patch.object(
                        forecast, "heavy_month_factor",
                        return_value={"factor": 2.0, "measured": 0,
                                      "basis": "default"}):
                    f = forecast.build(conn, today=TODAY)
            finally:
                conn.close()
            with self.subTest(seed=seed, checking=chk):
                self.assertTrue(f["card_habit"]["payer"])
                for (d0, b0), (_d, b1) in zip(
                        f["pace_usual"]["series"],
                        f["heavy"]["usual"]["series"]):
                    self.assertLessEqual(b1, b0 + 0.005, d0)
                self.assertEqual(f["pace_low"]["rounds"],
                                 f["pace_usual"]["rounds"])


class TodayIsPayday(unittest.TestCase):
    def test_a_paycheck_that_landed_today_starts_a_round_tomorrow(self):
        # the second earner is paid on the 15th — today — and the check
        # has landed: tomorrow is a round like any other payday's
        conn, _h = household(2, checking=9000, earners=2)
        try:
            add_txn(conn, TODAY, -2500.0, "RIVERBEND CLINIC PAYROLL",
                    account="chk", primary="INCOME", detailed="INCOME_WAGES")
            conn.execute("UPDATE bills SET raw = raw || %s::jsonb "
                         "WHERE payee = 'RIVERBEND CLINIC PAYROLL'",
                         ('{"dueOn": "2026-08-15", "lastDueOn": "2026-07-15"}',))
            f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        self.assertTrue(f["card_habit"]["payer"])
        self.assertIn("2026-07-16", f["pace_usual"]["rounds"])


class APaycheckThatLandedDaysAgo(unittest.TestCase):
    def test_a_round_still_to_come_after_monday_is_tomorrow(self):
        # paid Monday the 13th; the cards were last paid after the 1st's
        # check; today is Wednesday: the round is tomorrow
        conn, _h = household(2, checking=9000)
        try:
            add_txn(conn, D(2026, 7, 13), -2500.0, "NORTHSTAR PAYROLL",
                    account="chk", primary="INCOME", detailed="INCOME_WAGES")
            forecast.forget_learned()
            f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        self.assertTrue(f["card_habit"]["payer"])
        self.assertLess(f["card_habit"]["last_round"], D(2026, 7, 13))
        self.assertIn("2026-07-16", f["pace_usual"]["rounds"])


class ARoundCardWithNoCycle(unittest.TestCase):
    def test_it_is_paid_in_rounds_not_every_day(self):
        conn, _h = household(4, checking=9000)
        try:
            conn.execute("DELETE FROM liabilities WHERE account_id='card'")
            forecast.forget_learned()
            f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        modes = {c["card"]: c["mode"] for c in f["card_timing"]}
        self.assertEqual(modes, {"Test Card": "rounds"})
        rows = f["pace_usual"]["card_rows"]
        paid = [d for d, _a, p in rows if p.endswith("paid after payday")]
        self.assertTrue(paid)
        # every payment is a round, or the fallback a month after a charge
        # no round covered — never a payment a day
        self.assertLessEqual(len(rows), len(paid) + 3, rows)
        from oikonome.web import todayview
        self.assertTrue(todayview.card_timing_note(f).endswith(
            "; otherwise about 30 days after the charge."),
            todayview.card_timing_note(f))


class AHolidayPayday(unittest.TestCase):
    def test_the_cards_are_paid_the_business_day_after_the_rolled_payday(self):
        # paid on the 7th; Labor Day 2026 is Monday the 7th of September,
        # so the check lands Friday the 4th and the cards are paid Tuesday
        # the 8th
        conn, _h = household(3, checking=9000, payday=7)
        try:
            f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        self.assertTrue(f["card_habit"]["payer"], f["card_habit"])
        self.assertIn("2026-09-04", [d for d, a, _p in f["events"] if a > 0])
        self.assertIn("2026-09-08",
                      {d for d, _a in _paid_after_payday(f["pace_usual"])})


class EveryDollarLeavesOnce(unittest.TestCase):
    """For generated payday households (one or two earners, affordable or
    not) every card dollar leaves checking once, and each day's balance is
    checking plus its events, less its pace share, less card payments."""

    def test_one_dollar_one_exit(self):
        seen = {"payday": 0, "due": 0}
        for seed in props.seeds(12):
            rng = random.Random(seed * 31)
            conn, h = household(seed, earners=rng.choice([1, 2]),
                                payer=rng.random() < 0.8)
            try:
                f = forecast.build(conn, today=TODAY)
            finally:
                conn.close()
            w = f["pace_usual"]
            with self.subTest(seed=seed, habit=f["card_habit"]):
                share = f["pace_shares"].get("card", 0.0)
                debt = f["card_debt"]
                bills_ = -sum(a for _d, a, _p, _c in w["charged_bills"])
                charged = debt + f["pace_daily"] * share * f["days"] + bills_
                outs = sum(a for _d, a, c in w["outflows"] if c == "card")
                self.assertEqual(round(charged * 100),
                                 round((outs + w["after"].get("card", 0.0)
                                        + w["stays"].get("card", 0.0))
                                       * 100))
                for n, (d, bal) in enumerate(w["series"]):
                    want = (h["checking"]
                            + sum(a for dd, a, _p in w["checking_events"]
                                  if dd <= d)
                            - f["pace_daily"] * (1 - share) * n
                            - sum(a for dd, a, _c in w["outflows"]
                                  if dd <= d))
                    self.assertAlmostEqual(bal, want, delta=0.01 * (n + 1))
                # each scenario's heavier line sits at or below it, payday
                # rounds included
                for key, walk in (("usual", "pace_usual"),
                                  ("now", "pace_today"),
                                  ("due", "pace_due")):
                    for (d0, b0), (_d, b1) in zip(f[walk]["series"],
                                                  f["heavy"][key]["series"]):
                        self.assertLessEqual(b1, b0 + 0.005, (key, d0))
                kinds = {p.rsplit(" — ", 1)[1] for _d, _a, p in w["card_rows"]}
                seen["payday"] += "paid after payday" in kinds
                seen["due"] += "statement due" in kinds
        # both decisions happen among the generated households
        self.assertTrue(seen["payday"] and seen["due"], seen)



class OnACardsDueDay(unittest.TestCase):
    def test_only_the_statements_own_row_moves_into_todays_cell(self):
        from unittest import mock
        from oikonome.engine.compat import jsonb as _j
        from oikonome.web import api_today

        class Kept:
            def __init__(self, c):
                self._c = c

            def __getattr__(self, k):
                return getattr(self._c, k)

            def close(self):
                pass
        conn, _h = household(2, checking=9000)
        try:
            add_txn(conn, TODAY, -2500.0, "NORTHSTAR PAYROLL", account="chk",
                    primary="INCOME", detailed="INCOME_WAGES")
            conn.execute("UPDATE liabilities SET raw = %s WHERE "
                         "account_id = 'card'", (_j({
                             "last_statement_balance": 300.0,
                             "last_statement_issue_date": "2026-06-20",
                             "next_payment_due_date": TODAY.isoformat()}),))
            forecast.forget_learned()
            with mock.patch.object(api_today, "_conn",
                                   return_value=Kept(conn)), \
                 mock.patch.object(api_today, "_today", return_value=TODAY):
                cal = api_today.calendar_api({"tenant_id": "x"}, days=35)
        finally:
            conn.close()
        by = {c["date"]: [e["label"] for e in c["events"]]
              for c in cal["calendar"]}
        tomorrow = (TODAY + dt.timedelta(days=1)).isoformat()
        self.assertIn("Test Card — statement due", by.get(TODAY.isoformat(),
                                                           []))
        self.assertNotIn("Test Card — paid after payday",
                         by.get(TODAY.isoformat(), []))
        self.assertIn("Test Card — paid after payday", by.get(tomorrow, []))


if __name__ == "__main__":
    unittest.main()
