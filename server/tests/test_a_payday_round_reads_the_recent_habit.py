"""The payday rounds follow what the household does NOW and when it does
it (specs/today-figures.md, "Your usual: after payday when checking can
cover it"): a round is learned from the recent window — the last 120
days when they hold three rounds, else 180 — for its cards, its gap and
its cushion; and a paycheck that landed is offered a round only within
the household's own delay after payday (plus a business day): a payday
let pass is a skipped round, not a payment tomorrow. Figures and names
are invented."""

import datetime as dt
import random
import unittest

from oikonome.engine import business_days, forecast
from oikonome.engine.compat import jsonb
from oikonome.web import todayview

from . import props
from .test_each_card_is_paid_its_own_way import _credit
from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date


def _biweekly(today):
    """Every other Friday through today."""
    d, out = D(2026, 7, 10), []
    while d > today - dt.timedelta(days=240):
        d -= dt.timedelta(days=14)
    while d <= today:
        out.append(d)
        d += dt.timedelta(days=14)
    return out


def _monthly_10th(today):
    out = []
    for k in range(8, -1, -1):
        y, m = today.year, today.month - k
        while m < 1:
            y, m = y - 1, m + 12
        d = D(y, m, 10)
        d = d if business_days.is_business_day(d) else (
            business_days.previous_business_day(d))
        if d <= today:
            out.append(d)
    return out


def _payer(seed, *, biweekly=True, switch_after=None, skip=None,
           checking=9000, today=TODAY):
    """A household paid biweekly (else on the 10th), paying its card in
    full the business day after a payday — every payday until
    `switch_after` days before today, then every other payday; never
    after the payday `skip`."""
    rng = random.Random(seed)
    conn = make_db()
    write_config(conn, food_monthly=600, other_monthly=600)
    conn.execute("UPDATE accounts SET balance_available=%s, "
                 "balance_current=%s WHERE id='chk'", (checking, checking))
    conn.execute("UPDATE accounts SET name='Harbor card' WHERE id='card'")
    pays = _biweekly(today) if biweekly else _monthly_10th(today)
    for p in pays:
        add_txn(conn, p, -2400.0, "NORTHSTAR PAYROLL", account="chk",
                primary="INCOME", detailed="INCOME_WAGES")
    bal = 0.0
    d = today - dt.timedelta(days=240)
    while d < today:
        amt = float(rng.randint(20, 60))
        add_txn(conn, d, amt, f"MARKET {rng.randint(1, 9)}", account="card")
        bal += amt
        p = business_days.previous_business_day(d - dt.timedelta(days=1))
        if (p in pays and p != skip and bal > 0
                and business_days.add_business_days(p, 1) == d):
            monthly = (switch_after is not None
                       and (today - d).days <= switch_after)
            if not monthly or pays.index(p) % 2 == 0:
                _credit(conn, "card", d, bal)
                bal = 0.0
        d += dt.timedelta(days=1)
    conn.execute("UPDATE accounts SET balance_current=%s WHERE id='card'",
                 (round(bal, 2),))
    conn.execute("INSERT INTO liabilities (account_id, raw) VALUES "
                 "('card', %s)", (jsonb({
                     "last_statement_issue_date": "2026-07-03",
                     "next_payment_due_date": "2026-07-28",
                     "last_statement_balance": round(bal * 0.5, 2)}),))
    step = dt.timedelta(days=14) if biweekly else None
    nxt = (pays[-1] + step if biweekly
           else forecast._add_months(D(pays[-1].year, pays[-1].month, 10), 1))
    add_bill(conn, "NORTHSTAR PAYROLL", 2400.0, income=True,
             frequency="WEEKLY" if biweekly else "MONTHLY",
             interval=2 if biweekly else 1, next_due=nxt,
             last_seen=pays[-1])
    return conn


class TheRecentHabit(unittest.TestCase):
    def test_every_payday_then_monthly_is_walked_monthly(self):
        for seed in props.seeds(3):
            conn = _payer(seed, switch_after=120)
            try:
                f = forecast.build(conn, today=TODAY)
            finally:
                conn.close()
            t = {c["card"]: c for c in f["card_timing"]}["Harbor card"]
            rounds = [D.fromisoformat(r) for r in f["pace_usual"]["rounds"]]
            with self.subTest(seed=seed):
                self.assertEqual(t["mode"], "rounds")
                self.assertGreaterEqual(t["gap"], 25)
                # about monthly: no two rounds within three weeks
                self.assertTrue(all((b - a).days >= 21
                                    for a, b in zip(rounds, rounds[1:])),
                                rounds)
                self.assertIn("about once a month, after a payday",
                              todayview.card_timing_note(f))


class ASkippedPayday(unittest.TestCase):
    def test_is_no_round_tomorrow(self):
        # paid the business day after every payday on the 10th; the July
        # paycheck landed three business days ago and nothing was paid
        payday = D(2026, 7, 10)
        self.assertEqual(business_days.add_business_days(payday, 3), TODAY)
        conn = _payer(1, biweekly=False, skip=payday)
        try:
            f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        self.assertEqual(f["card_habit"].get("delay"), 1)
        tomorrow = (TODAY + dt.timedelta(days=1)).isoformat()
        self.assertNotIn(tomorrow, f["pace_usual"]["rounds"])
        self.assertFalse([p for d, _a, p in f["pace_usual"]["card_rows"]
                          if d == tomorrow])

    def test_a_payday_within_the_delay_is_still_paid(self):
        # the same household the day after payday: the round is still due
        today = D(2026, 7, 10)
        conn = _payer(1, biweekly=False, skip=today, today=today)
        try:
            f = forecast.build(conn, today=today)
        finally:
            conn.close()
        self.assertIn(D(2026, 7, 13).isoformat(), f["pace_usual"]["rounds"])


if __name__ == "__main__":
    unittest.main()
