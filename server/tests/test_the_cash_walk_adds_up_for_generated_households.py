"""The cash timeline's walk adds up, and counts nothing twice, for
generated households that carry every shape the walk dates on its own:
pending card charges, a statement already paid in full or in part, a card
payment still on its way to checking, and a payday on a weekend or a bank
holiday (specs/today-figures.md).

Since card spending leaves checking when the card is paid ("Card spending
leaves checking when the household pays the card"), the households also
carry a card paid by hand some days after each charge or at its due
dates, a bill paid by the card, and spending on the card and on checking.

Properties, for each household (tests/props.py seeds) and for each of the
card-aware walks (your usual, pay on the due date):
  - one dollar, one exit: per card, the debt the cash card prints plus
    every dollar charged to it inside the horizon (its share of the pace,
    the bills it pays) equals its payments out of checking inside the
    horizon plus what is left in cards_after_horizon, to the cent;
  - each day's balance is checking plus every checking event through that
    day, less checking's share of the pace for each day walked, less the
    card payments through that day;
  - a bill a card pays is never a checking event, and every other bill is;
  - the payment on its way leaves checking exactly once, on a business
    day, and every paycheck lands on a business day;
  - a paycheck or a bill walked at an estimate (last year's pattern, a
    seasonal bill) is labelled so, and the walk adds up with it.
Cash after card balances (pace_now) starts from checking less the card debt and
is not moved by card timing. Each scenario's heavier-month line sits at or
below it every day.
Figures are invented by the generator."""

from __future__ import annotations

import datetime as dt
import random
import unittest

from oikonome.engine import business_days, forecast
from oikonome.engine.compat import jsonb

from . import props
from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date
ON_WAY = "Card payment on its way — Test Card"


def _cents(x: float) -> int:
    return round(x * 100)


class WalkAddsUp(unittest.TestCase):
    def _household(self, seed: int):
        rng = random.Random(seed)
        conn = make_db()
        write_config(conn, food_monthly=rng.choice([0, 600, 1500]),
                     other_monthly=rng.choice([0, 600, 1500]))
        checking = rng.randint(2000, 9000)
        conn.execute("UPDATE accounts SET balance_available=%s, "
                     "balance_current=%s WHERE id='chk'", (checking, checking))
        stmt = float(rng.randint(150, 900))
        paid = rng.choice([stmt, round(stmt * rng.uniform(0.2, 0.8), 2)])
        since = float(rng.randint(20, 400))
        posted = round(stmt - paid + since, 2)
        conn.execute("UPDATE accounts SET balance_current=%s WHERE id='card'",
                     (posted,))
        credit_day = TODAY - dt.timedelta(days=rng.randint(0, 4))
        issued = TODAY - dt.timedelta(days=rng.randint(3, 27))
        liab = {"last_statement_balance": stmt,
                "last_statement_issue_date": issued.isoformat(),
                "last_payment_amount": paid,
                "last_payment_date": credit_day.isoformat(),
                "next_payment_due_date":
                    (issued + dt.timedelta(days=rng.randint(21, 28))
                     ).isoformat()}
        if rng.random() < 0.2:
            # an issuer that reports no cycle at all
            for k in ("last_statement_issue_date", "next_payment_due_date"):
                del liab[k]
        conn.execute(
            "INSERT INTO liabilities (account_id, raw) VALUES ('card', %s)",
            (jsonb(liab),))
        # months of spending on the card and on checking; a hand payer
        # pays the card every week or two, a due-date payer monthly
        every = rng.choice([7, 14, 30])
        d = TODAY - dt.timedelta(days=rng.randint(70, 140))
        while d < TODAY - dt.timedelta(days=5):
            if rng.random() < 0.6:
                add_txn(conn, d, float(rng.randint(10, 80)),
                        f"CORNER SHOP {rng.randint(1, 9)}",
                        account=rng.choice(["card", "card", "chk"]))
            if (d - TODAY).days % every == 0:
                add_txn(conn, d, -float(rng.randint(200, 900)),
                        "PAYMENT THANK YOU", primary="LOAN_PAYMENTS",
                        detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
            d += dt.timedelta(days=1)
        # the payment's credit is on the card; checking has not seen it
        add_txn(conn, credit_day, -paid, "PAYMENT THANK YOU",
                primary="LOAN_PAYMENTS",
                detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
        pending = []
        for _ in range(rng.randint(1, 3)):
            amt = float(rng.randint(15, 160))
            add_txn(conn, TODAY - dt.timedelta(days=rng.randint(0, 3)), amt,
                    f"CORNER SHOP {rng.randint(1, 99)}", pending=1)
            pending.append(amt)
        # a monthly payday on the 1st: 08/01/26 is a Saturday; or the
        # Labor Day Monday
        payday = rng.choice([D(2026, 8, 1), D(2026, 9, 7)])
        pay = float(rng.randint(1500, 4000))
        add_bill(conn, "BRIGHTLINE PAYROLL", pay,
                 income=True, next_due=payday,
                 last_seen=payday - dt.timedelta(days=31))
        water = float(rng.randint(40, 120))
        add_bill(conn, "RIVERTOWN WATER", water,
                 next_due=D(2026, 7, 25), last_seen=D(2026, 6, 25))
        if rng.random() < 0.5:
            # a year and more of history: last year's paycheck stepped up
            # in August, and the water bill runs with the seasons
            step = 1 + rng.randint(3, 20) / 100
            y, m = 2025, 4
            while (y, m) < (2026, 7):
                on = D(y, m, payday.day)
                if not business_days.is_business_day(on):
                    on = business_days.previous_business_day(on)
                add_txn(conn, on, -round(pay * (step if (y, m) >= (2025, 8)
                                                 and y == 2025 else 1), 2),
                        "BRIGHTLINE PAYROLL", account="chk",
                        primary="INCOME", detailed="INCOME_WAGES")
                add_txn(conn, D(y, m, 25),
                        round(water * (1 + 0.2 * ((m % 6) - 2.5) / 2.5), 2),
                        "RIVERTOWN WATER", account="chk")
                y, m = (y, m + 1) if m < 12 else (y + 1, 1)
        # a streaming bill the card pays, or checking
        stream = float(rng.randint(9, 30))
        on = rng.choice(["card", "chk"])
        for back in (3, 2, 1):
            add_txn(conn, D(2026, 7 - back, 18), stream, "CEDAR STREAMING",
                    account=on)
        add_bill(conn, "CEDAR STREAMING", stream, next_due=D(2026, 7, 18),
                 last_seen=D(2026, 6, 18))
        return conn, {"checking": checking, "posted": posted,
                      "pending": sum(pending), "paid": paid,
                      "stream_on": on}

    def test_the_walk_adds_up_and_counts_nothing_twice(self):
        for seed in props.seeds():
            with self.subTest(seed=seed):
                conn, h = self._household(seed)
                try:
                    f = forecast.build(conn, today=TODAY, days=60)
                finally:
                    conn.close()
                debt = h["posted"] + h["pending"]
                self.assertEqual(_cents(f["card_debt"]), _cents(debt))
                on_way = [(d, a) for d, a, p in f["events"] if p == ON_WAY]
                self.assertEqual([_cents(-a) for _d, a in on_way],
                                 [_cents(h["paid"])])
                # an estimate is labelled, and stays a paycheck or a bill
                for d, a, p in f["events"]:
                    if p in f["estimated"]:
                        self.assertTrue(p.endswith(" (estimate)") and a < 0
                                        or p.endswith("last year's pattern)")
                                        and a > 0, (p, a))
                for d, _a, p in f["events"]:
                    if p == ON_WAY or p.startswith(("BRIGHTLINE PAYROLL",
                                                    "RIVERTOWN WATER")):
                        self.assertTrue(business_days.is_business_day(
                            D.fromisoformat(d)), (p, d))
                rate = f["pace_daily"]
                share = f["pace_shares"].get("card", 0.0)
                share_ck = 1.0 - share
                for key in ("pace_usual", "pace_due", "pace_today"):
                    w = f[key]
                    with self.subTest(walk=key):
                        self._one_exit(f, w, h, debt, rate, share, share_ck)
                # pay all cards today: the whole debt leaves once, on the
                # walk's first day
                today_rows = f["pace_today"]["card_rows"]
                payoff = [(d, a) for d, a, p in today_rows
                          if p.endswith("— paid in full")]
                self.assertEqual(
                    [(d, _cents(-a)) for d, a in payoff],
                    [((TODAY + dt.timedelta(days=1)).isoformat(),
                      _cents(debt))] if _cents(debt) else [])
                # its low is never below cash after card balances'
                self.assertGreaterEqual(f["pace_today"]["min"] + 0.005,
                                        f["pace_now"]["min"])
                # each scenario's heavier-month line sits at or below it
                for key, walk in (("usual", "pace_usual"),
                                  ("now", "pace_today"),
                                  ("due", "pace_due")):
                    for (d0, b0), (_d, b1) in zip(f[walk]["series"],
                                                  f["heavy"][key]["series"]):
                        self.assertLessEqual(b1, b0 + 0.005, (key, d0))
                # cash after card balances: checking less the debt, every
                # event
                now = f["pace_now"]
                self.assertEqual(_cents(now["series"][0][1]),
                                 _cents(h["checking"] - debt))

    def _one_exit(self, f, w, h, debt, rate, share, share_ck):
        days = f["days"]
        card_bills = -sum(a for _d, a, _p, _c in w["charged_bills"])
        charged = debt + rate * share * days + card_bills
        outs = sum(a for _d, a, c in w["outflows"] if c == "card")
        # a carried balance's rest stays on the card (it is no exit)
        self.assertEqual(_cents(charged),
                         _cents(outs + w["after"].get("card", 0.0)
                                + w["stays"].get("card", 0.0)))
        # the streaming bill is the card's exactly when the card paid it
        ck_payees = {p for _d, _a, p in w["checking_events"]}
        on_card = {p for _d, _a, p, _c in w["charged_bills"]}
        self.assertEqual("CEDAR STREAMING" in on_card,
                         h["stream_on"] == "card")
        self.assertFalse(on_card & ck_payees, on_card & ck_payees)
        self.assertTrue(ck_payees & {"RIVERTOWN WATER",
                                     "RIVERTOWN WATER (estimate)"})
        self.assertEqual([p for _d, _a, p in w["checking_events"]
                          ].count(ON_WAY), 1)
        # each day's balance: checking + checking events − checking's pace
        # share per day − card payments, through that day
        for n, (d, bal) in enumerate(w["series"]):
            want = (h["checking"]
                    + sum(a for dd, a, _p in w["checking_events"] if dd <= d)
                    - rate * share_ck * n
                    - sum(a for dd, a, _c in w["outflows"] if dd <= d))
            self.assertAlmostEqual(bal, want, delta=0.01 * (n + 1), msg=d)


if __name__ == "__main__":
    unittest.main()


class PendingChargesReadAlike(unittest.TestCase):
    def test_the_two_pass_read_is_the_one_statement_rule(self):
        for seed in props.seeds(6):
            conn, _h = WalkAddsUp()._household(seed)
            try:
                # a hold the feed has settled, by its raw link
                add_txn(conn, TODAY - dt.timedelta(days=2), 44.0,
                        "CORNER SHOP HOLD", pending=1, txn_id=f"hold{seed}")
                conn.execute(
                    "INSERT INTO transactions (id, account_id, date, amount,"
                    " name, pending, removed, raw) VALUES (%s, 'card', %s,"
                    " 44.0, 'CORNER SHOP', 0, 0, %s)",
                    (f"settled{seed}", TODAY - dt.timedelta(days=1),
                     jsonb({"pending_transaction_id": f"hold{seed}"})))
                want = {r["account_id"]: round(float(r["pending"]), 2)
                        for r in conn.execute(forecast._CARD_PENDING_SQL,
                                              (["card"],)).fetchall()}
                got = {k: round(v, 2) for k, v in
                       forecast.card_pending(conn, ["card"]).items()}
            finally:
                conn.close()
            with self.subTest(seed=seed):
                self.assertEqual(got, want)
