"""What the forecast learned is yesterday's; what it sees today is today's
(specs/today-figures.md). Every learned fact — the usual pace and its
one-offs, the heavier month, the bills' payments and paying accounts, each
card's mode, gap and last payment, the payday habit and its cushion — is
computed from the ledger as it stood at the end of yesterday, so a build
that reuses them after a write dated today equals a build that learns them
again. What happened today is applied live on top. Figures and names are
invented."""

import datetime as dt
import json
import random
import unittest

from oikonome.engine import forecast

from . import props
from .test_each_card_is_paid_its_own_way import _household, _credit
from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date


def _as_json(f) -> str:
    return json.dumps(f, sort_keys=True, default=str)


def _kept_and_fresh(conn):
    kept = _as_json(forecast.build(conn, today=TODAY))
    forecast.forget_learned()
    conn._memo = None
    fresh = _as_json(forecast.build(conn, today=TODAY))
    return kept, fresh


class ACardPaymentToday(unittest.TestCase):
    def test_kept_equals_fresh_and_the_round_sees_the_payment(self):
        conn = _household(1)
        try:
            forecast.forget_learned()
            before = forecast.build(conn, today=TODAY)
            # pay Harbor card today: both legs, and the card's balance
            bal = float(conn.execute(
                "SELECT balance_current FROM accounts WHERE id='card'"
            ).fetchone()["balance_current"])
            _credit(conn, "card", TODAY, bal)
            conn.execute("UPDATE accounts SET balance_current=0 "
                         "WHERE id='card'")
            conn.execute("UPDATE accounts SET balance_current="
                         "balance_current - %s, balance_available="
                         "balance_available - %s WHERE id='chk'", (bal, bal))
            conn._memo = None
            after = forecast.build(conn, today=TODAY)
            kept, fresh = _as_json(after), None
            forecast.forget_learned()
            conn._memo = None
            fresh = _as_json(forecast.build(conn, today=TODAY))
        finally:
            conn.close()
        self.assertEqual(kept, fresh)
        # the round logic sees the payment: the card was last paid today,
        # so no round pays it again before its gap has passed
        gap = {c["card"]: c for c in after["card_timing"]}
        self.assertIn("Harbor card", gap)
        first_before = (before["pace_usual"]["round_cards"] or {})
        days = sorted(d for d, cs in after["pace_usual"]["round_cards"].items()
                      if "card" in cs)
        self.assertTrue(not days or (D.fromisoformat(days[0]) - TODAY).days
                        >= forecast.ROUND_GAP_SHARE * 10, (days,
                                                          first_before))


class ARowTodayTakesABillsClaim(unittest.TestCase):
    def test_a_claim_moving_today_waits_for_tomorrow_on_both_builds(self):
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=600)
            d = TODAY - dt.timedelta(days=120)
            while d < TODAY:
                add_txn(conn, d, 25.0, "CORNER MARKET",
                        primary="FOOD_AND_DRINK")
                d += dt.timedelta(days=1)
            for m in (4, 5, 6):
                add_txn(conn, D(2026, m, 15), 45.0, "CITY WATER",
                        account="chk")
            add_bill(conn, "CITY WATER", 45.0, next_due=D(2026, 7, 15),
                     last_seen=D(2026, 6, 15))
            # a look-alike earlier this month holds the claim ...
            add_txn(conn, D(2026, 7, 9), 45.0, "CITY WATER", account="chk",
                    txn_id="look-alike")
            forecast.forget_learned()
            forecast.build(conn, today=TODAY)
            # ... until the real payment, dated today, takes it
            add_txn(conn, TODAY, 45.0, "CITY WATER", account="chk",
                    txn_id="real-payment")
            conn._memo = None
            kept, fresh = _kept_and_fresh(conn)
        finally:
            conn.close()
        self.assertEqual(kept, fresh)


class AnAnnualPoolsClaimsAreYesterdaysToo(unittest.TestCase):
    """An annual envelope's pool starts the month part-drawn by what it
    claimed earlier in the year, and that claim is decided over the whole
    year's rows: a yearly bill's real payment dated today takes back the
    occurrence that had held an early look-alike, which the pool then
    claims. Read as of yesterday, the pool's earlier claims ignore today's
    row, so a build that reuses what it learned equals one that learns it
    again."""

    def test_a_yearly_bills_payment_today_moves_no_earlier_claim(self):
        for look in (D(2026, 6, 14), D(2026, 6, 28)):
            conn = make_db()
            try:
                write_config(conn, food_monthly=600, other_monthly=600)
                d = TODAY - dt.timedelta(days=150)
                while d < TODAY:
                    add_txn(conn, d, 25.0, "CORNER MARKET",
                            primary="FOOD_AND_DRINK")
                    d += dt.timedelta(days=1)
                for y in (2024, 2025):
                    add_txn(conn, D(y, 7, 17), 45.0, "CITY WATER",
                            account="chk")
                add_bill(conn, "CITY WATER", 45.0, interval=12,
                         next_due=D(2026, 7, 17), last_seen=D(2025, 7, 17))
                add_bill(conn, "Water Fund", 100.0, bill_type="envelope",
                         frequency=None, interval=12, merchant="CITY WATER")
                # an early look-alike the yearly bill holds as a prepay ...
                add_txn(conn, look, 45.0, "CITY WATER", account="chk",
                        txn_id="look-alike")
                add_txn(conn, D(2026, 7, 5), 70.0, "CITY WATER",
                        account="chk", txn_id="pool-charge")
                forecast.forget_learned()
                forecast.build(conn, today=TODAY)
                # ... until the bill's real payment arrives today
                add_txn(conn, TODAY, 45.0, "CITY WATER", account="chk",
                        txn_id="real-payment")
                conn._memo = None
                kept, fresh = _kept_and_fresh(conn)
            finally:
                conn.close()
            self.assertEqual(kept, fresh, look)


class LastMonthIsYesterdaysToo(unittest.TestCase):
    """A closed month's bills match a little past its end, so a bill's
    payment arriving in the first days of the next month can free last
    month's look-alike row. The forecast reads closed months as of
    yesterday too: kept and fresh builds agree whether the payment is
    dated today or a few days ahead."""

    def test_a_late_payment_in_the_first_week(self):
        for due_day in (28, 31):
            for as_of_day in (1, 3, 5):
                for ahead in (0, 2):
                    as_of = D(2026, 8, as_of_day)
                    conn = make_db()
                    try:
                        write_config(conn, food_monthly=600, other_monthly=600)
                        d = as_of - dt.timedelta(days=150)
                        while d < as_of:
                            add_txn(conn, d, 25.0, "CORNER MARKET",
                                    primary="FOOD_AND_DRINK")
                            d += dt.timedelta(days=1)
                        for m in (4, 5, 6):
                            add_txn(conn, D(2026, m, min(due_day, 30)), 45.0,
                                    "CITY WATER", account="chk")
                        add_bill(conn, "CITY WATER", 45.0,
                                 next_due=D(2026, 7, due_day),
                                 last_seen=D(2026, 6, min(due_day, 30)))
                        # a look-alike earlier in July holds the claim ...
                        add_txn(conn, D(2026, 7, 20), 45.0, "CITY WATER",
                                account="chk", txn_id="look-alike")
                        forecast.forget_learned()
                        forecast.build(conn, today=as_of)
                        # ... until July's real payment arrives
                        add_txn(conn, as_of + dt.timedelta(days=ahead), 45.0,
                                "CITY WATER", account="chk",
                                txn_id="real-payment")
                        conn._memo = None
                        kept = _as_json(forecast.build(conn, today=as_of))
                        forecast.forget_learned()
                        conn._memo = None
                        fresh = _as_json(forecast.build(conn, today=as_of))
                    finally:
                        conn.close()
                    with self.subTest(due=due_day, as_of=as_of, ahead=ahead):
                        self.assertEqual(kept, fresh)


class AnyWriteDatedToday(unittest.TestCase):
    """For generated households, after any write dated today, the build
    that reuses the learned facts equals the one that learns them again."""

    dynamic = False

    def test_kept_equals_fresh(self):
        kinds = ("card charge", "checking charge", "card payment",
                 "paycheck", "pending", "bill payment", "refund")
        for seed in props.seeds(10):
            rng = random.Random(seed)
            conn = _household(seed)
            try:
                if self.dynamic:
                    from oikonome.engine import budget
                    cfg = budget.load_config(conn)
                    budget.save_config(conn, dict(
                        cfg, dynamic_variable_budget=True))
                forecast.forget_learned()
                forecast.build(conn, today=TODAY)
                for _ in range(rng.randint(1, 3)):
                    kind = rng.choice(kinds)
                    amt = float(rng.randint(5, 900))
                    if kind == "card charge":
                        add_txn(conn, TODAY, amt, "MARKET 7", account="card")
                    elif kind == "checking charge":
                        add_txn(conn, TODAY, amt, "MARKET 8", account="chk")
                    elif kind == "card payment":
                        _credit(conn, rng.choice(["card", "auto"]), TODAY, amt)
                    elif kind == "paycheck":
                        add_txn(conn, TODAY, -2600.0, "NORTHSTAR PAYROLL",
                                account="chk", primary="INCOME",
                                detailed="INCOME_WAGES")
                    elif kind == "pending":
                        add_txn(conn, TODAY, amt, "MARKET 9", pending=1)
                    elif kind == "bill payment":
                        add_txn(conn, TODAY, amt, "MARKET 1", account="auto")
                    else:
                        add_txn(conn, TODAY, -amt, "MARKET 2 REFUND")
                    conn.execute(
                        "UPDATE accounts SET balance_current = "
                        "balance_current + %s WHERE id = %s",
                        (rng.choice([-1, 1]) * amt,
                         rng.choice(["card", "auto", "chk"])))
                conn._memo = None
                kept, fresh = _kept_and_fresh(conn)
            finally:
                conn.close()
            with self.subTest(seed=seed):
                self.assertEqual(kept, fresh)


class AnyWriteDatedTodayWithADynamicBudget(AnyWriteDatedToday):
    """The same with the variable budget scaled by the month's bills."""
    dynamic = True


class ThePlansDailyIsYesterdays(unittest.TestCase):
    """The plan's daily the pace is floored at (and the plan walk spends)
    is learned too: with the variable budget scaled to what income leaves
    after the bills, a paycheck landing today lower than expected shrinks
    today's plan — the kept build and a fresh one must agree, so both read
    the plan as it stood at the end of yesterday."""

    def test_a_short_paycheck_today_moves_nothing_learned(self):
        for amount in (1900.0, 2500.0, 3100.0):
            conn = make_db()
            try:
                write_config(conn, food_monthly=1500, other_monthly=1500,
                             dynamic_variable_budget=True)
                d = TODAY - dt.timedelta(days=150)
                while d < TODAY:
                    add_txn(conn, d, 20.0, "CORNER MARKET",
                            primary="FOOD_AND_DRINK")
                    d += dt.timedelta(days=1)
                for m in (4, 5, 6):
                    add_txn(conn, D(2026, m, 15), 300.0, "CITY WATER",
                            account="chk")
                    add_txn(conn, D(2026, m, 1), -2500.0, "OAK PAYROLL",
                            account="chk", primary="INCOME",
                            detailed="INCOME_WAGES")
                add_bill(conn, "CITY WATER", 300.0, next_due=TODAY,
                         last_seen=D(2026, 6, 15))
                add_bill(conn, "OAK PAYROLL", 2500.0, income=True,
                         next_due=TODAY, last_seen=D(2026, 6, 1))
                forecast.forget_learned()
                forecast.build(conn, today=TODAY)
                add_txn(conn, TODAY, -amount, "OAK PAYROLL", account="chk",
                        primary="INCOME", detailed="INCOME_WAGES",
                        txn_id="pay-today")
                conn._memo = None
                kept, fresh = _kept_and_fresh(conn)
            finally:
                conn.close()
            self.assertEqual(kept, fresh, amount)


if __name__ == "__main__":
    unittest.main()


class YesterdaysBalances(unittest.TestCase):
    def test_checking_is_the_walks_own_start_and_a_later_row_undoes_nothing(self):
        from .test_a_payday_payer_pays_its_cards_after_payday import household
        # checking well above what the rounds take: a cushion to learn
        conn, _h = household(1, checking=60000)
        try:
            forecast.forget_learned()
            a = forecast.build(conn, today=TODAY)["card_habit"]["cushion"]
            # a bank that reports only an available balance
            conn.execute("UPDATE accounts SET balance_current = NULL "
                         "WHERE id = 'chk'")
            forecast.forget_learned()
            conn._memo = None
            b = forecast.build(conn, today=TODAY)["card_habit"]["cushion"]
            # a posted row dated after today is in no balance yet
            add_txn(conn, TODAY + dt.timedelta(days=3), 40.0, "MARKET 5",
                    txn_id="later")
            cards = forecast.card_balances(conn, [], with_empty=True)
            _k, eod, _c = forecast.yesterday_balances(conn, cards, "chk",
                                                      TODAY, 60000.0)
        finally:
            conn.close()
        self.assertGreater(a, 0)
        self.assertEqual(a, b)
        card = next(c for c in cards if c["id"] == "card")
        self.assertEqual(eod["card"], round(card["posted"], 2))


class AnyWriteInAMonthsFirstWeek(unittest.TestCase):
    """The random-write property again, as of a day in the first week of
    a month, with a bill whose late payment a row dated today can move."""

    def test_kept_equals_fresh(self):
        for seed in props.seeds(8):
            rng = random.Random(seed * 13)
            as_of = D(2026, 8, rng.randint(1, 7))
            conn = make_db()
            try:
                write_config(conn, food_monthly=600, other_monthly=600)
                d = as_of - dt.timedelta(days=150)
                while d < as_of:
                    add_txn(conn, d, float(rng.randint(10, 60)),
                            f"CORNER MARKET {rng.randint(1, 4)}",
                            account=rng.choice(["card", "chk"]),
                            primary="FOOD_AND_DRINK")
                    d += dt.timedelta(days=1)
                for m in (4, 5, 6):
                    add_txn(conn, D(2026, m, 29), 45.0, "CITY WATER",
                            account="chk")
                add_bill(conn, "CITY WATER", 45.0, next_due=D(2026, 7, 29),
                         last_seen=D(2026, 6, 29))
                add_txn(conn, D(2026, 7, rng.randint(5, 25)), 45.0,
                        "CITY WATER", account="chk")
                forecast.forget_learned()
                forecast.build(conn, today=as_of)
                for _ in range(rng.randint(1, 3)):
                    kind = rng.choice(["bill", "charge", "refund", "later"])
                    when = as_of + dt.timedelta(
                        days=rng.randint(1, 4) if kind == "later" else 0)
                    if kind in ("bill", "later"):
                        add_txn(conn, when, 45.0, "CITY WATER", account="chk")
                    elif kind == "charge":
                        add_txn(conn, when, float(rng.randint(5, 300)),
                                "CORNER MARKET 2")
                    else:
                        add_txn(conn, when, -20.0, "CORNER MARKET 1 REFUND")
                conn._memo = None
                kept = _as_json(forecast.build(conn, today=as_of))
                forecast.forget_learned()
                conn._memo = None
                fresh = _as_json(forecast.build(conn, today=as_of))
            finally:
                conn.close()
            with self.subTest(seed=seed, as_of=as_of):
                self.assertEqual(kept, fresh)
