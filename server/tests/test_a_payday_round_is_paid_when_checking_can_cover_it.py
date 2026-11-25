"""A payday payer's round is paid when checking can afford it
(specs/today-figures.md, "Your usual: after payday when checking can
cover it"): after paying the round's cards in full, checking stays at or
above the household's FLOOR — the lowest it let itself reach right after
a past round in the last year, at most half a paycheck — through the day
before the next paycheck lands. Money left over after past rounds is no
rule the household follows: a household whose income runs well above its
spending keeps paying its cards every round. A tight household waits for
the due dates. Figures and names are invented."""

import datetime as dt
import unittest

from oikonome.engine import forecast
from oikonome.web import todayview

from . import props
from .test_a_payday_round_reads_the_recent_habit import _payer
from .util import TODAY, add_bill, add_txn

D = dt.date


def _owed_back_to_zero(w, gap_days):
    """Whether the cards' combined balance comes back to about $0 at
    least once in every stretch of `gap_days`."""
    held = [(D.fromisoformat(d), v) for d, v in w["cards_owed"]]
    zeros = [d for d, v in held if v <= 1.0]
    if not zeros:
        return False
    marks = [TODAY] + zeros
    return all((b - a).days <= gap_days for a, b in zip(marks, marks[1:]))


class ComfortablyAhead(unittest.TestCase):
    def test_rounds_continue_monthly(self):
        for seed in props.seeds(4):
            # paid every other biweekly payday for eight months, income
            # well above spending: checking after each round held well
            # over a paycheck; a transfer to savings since leaves it below
            # what the next round pays
            conn = _payer(seed, switch_after=400, checking=1000)
            try:
                add_txn(conn, TODAY - dt.timedelta(days=5), 14000.0,
                        "TRANSFER TO SAVINGS", account="chk",
                        primary="TRANSFER_OUT")
                f = forecast.build(conn, today=TODAY)
            finally:
                conn.close()
            usual, due = f["pace_usual"], f["pace_due"]
            t = {c["card"]: c for c in f["card_timing"]}["Harbor card"]
            with self.subTest(seed=seed, floor=f["card_habit"]["cushion"]):
                self.assertEqual(t["mode"], "rounds")
                self.assertIsNotNone(t.get("last_paid"))
                self.assertGreaterEqual(len(usual["rounds"]), 2)
                # the first round comes when the card's own gap is up, not
                # a payday later for want of a leftover the household
                # never kept as a rule
                self.assertLessEqual(
                    D.fromisoformat(usual["rounds"][0]),
                    t["last_paid"] + dt.timedelta(days=t["gap"] + 3))
                self.assertTrue(_owed_back_to_zero(usual, t["gap"] + 14),
                                usual["cards_owed"])
                self.assertLess(usual["cards_after_horizon"],
                                due["cards_after_horizon"])
                self.assertIsNotNone(todayview.card_timing_note(f))
                assert_rounds_keep_coming(self, f)


class Tight(unittest.TestCase):
    def test_the_round_waits_for_the_due_dates(self):
        conn = _payer(1, switch_after=400, checking=200)
        try:
            # rent before the next paycheck would leave checking below
            # the floor once the cards were paid
            add_bill(conn, "MAPLE APARTMENTS", 2300.0,
                     next_due=D(2026, 7, 30), last_seen=D(2026, 6, 30))
            f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        usual = f["pace_usual"]
        rounds = [D.fromisoformat(r) for r in usual["rounds"]]
        self.assertNotIn(D(2026, 7, 27), rounds)
        due = [p for _d, _a, p in usual["card_rows"]
               if p.endswith("— statement due")]
        self.assertTrue(due, usual["card_rows"])


if __name__ == "__main__":
    unittest.main()


def _look_alike(seed, *, checking=2600.0, tax=True):
    """Paid 3,000 every second Friday, spending about 80 % of it: a
    mortgage two days after one payday a month from checking and
    everyday spending on four cards — two paid after every payday, two
    after every other one; what is left goes to savings each month, so
    checking runs level. `tax`: a yearly bill due three days after the
    next round, so checking dips well below where a round leaves it
    before the next paycheck."""
    import random
    from oikonome.engine import business_days
    from oikonome.engine.compat import jsonb
    from .test_each_card_is_paid_its_own_way import _card, _credit
    from .test_a_payday_round_reads_the_recent_habit import _biweekly
    from .util import make_db, write_config
    rng = random.Random(seed)
    conn = make_db()
    write_config(conn, food_monthly=900, other_monthly=900)
    conn.execute("UPDATE accounts SET balance_available=%s, "
                 "balance_current=%s WHERE id='chk'", (checking, checking))
    cards = {"card": ("Harbor card", 14), "c2": ("Cove card", 14),
             "c3": ("Summit card", 28), "c4": ("Pine card", 28)}
    conn.execute("UPDATE accounts SET name='Harbor card' WHERE id='card'")
    for cid, (name, _g) in cards.items():
        if cid != "card":
            _card(conn, cid, name)
    pays = _biweekly(TODAY)
    for p in pays:
        add_txn(conn, p, -3000.0, "NORTHSTAR PAYROLL", account="chk",
                primary="INCOME", detailed="INCOME_WAGES")
    bal = {cid: 0.0 for cid in cards}
    d = TODAY - dt.timedelta(days=240)
    while d < TODAY:
        if d.day == 3:
            add_txn(conn, d, 1400.0, "LAKESIDE MORTGAGE", account="chk")
        if d.day == 20:
            # what is left each month goes to savings: checking runs level
            add_txn(conn, d, 1250.0, "TRANSFER TO SAVINGS", account="chk",
                    primary="TRANSFER_OUT")
        for cid in cards:
            amt = float(rng.randint(15, 50))
            add_txn(conn, d, amt, f"MARKET {rng.randint(1, 9)}", account=cid)
            bal[cid] += amt
        p = business_days.previous_business_day(d - dt.timedelta(days=1))
        if p in pays and business_days.add_business_days(p, 1) == d:
            for cid, (_n, gap) in cards.items():
                if gap == 14 or pays.index(p) % 2 == 0:
                    _credit(conn, cid, d, bal[cid])
                    bal[cid] = 0.0
        d += dt.timedelta(days=1)
    for cid in cards:
        conn.execute("UPDATE accounts SET balance_current=%s WHERE id=%s",
                     (round(bal[cid], 2), cid))
        conn.execute("INSERT INTO liabilities (account_id, raw) VALUES "
                     "(%s, %s)", (cid, jsonb({
                         "last_statement_issue_date": "2026-07-03",
                         "next_payment_due_date": "2026-07-28",
                         "last_statement_balance": round(bal[cid] * .5, 2)})))
    add_bill(conn, "LAKESIDE MORTGAGE", 1400.0, next_due=D(2026, 8, 3),
             last_seen=D(2026, 7, 3))
    if tax:
        add_bill(conn, "COUNTY TAX", 1500.0, frequency="YEARLY",
                 next_due=D(2026, 7, 30), last_seen=D(2025, 7, 30))
    add_bill(conn, "NORTHSTAR PAYROLL", 3000.0, income=True,
             frequency="WEEKLY", interval=2,
             next_due=pays[-1] + dt.timedelta(days=14), last_seen=pays[-1])
    return conn


def assert_rounds_keep_coming(case, f, pay_period=14):
    """Every round card is paid in a round within its own gap plus a pay
    period of its last payment, again and again to the chart's end; the
    usual low is below the due-date low and less is left on the cards."""
    usual, due = f["pace_usual"], f["pace_due"]
    t = {c["card"]: c for c in f["card_timing"] if c["mode"] == "rounds"}
    longest = max(c["gap"] or pay_period for c in t.values())
    case.assertTrue(t)
    end = D.fromisoformat(usual["series"][-1][0])
    by_name = t
    # the round days of each card, by its name as the rows say it
    days_of: dict[str, list] = {}
    for d0, _a, p in usual["card_rows"]:
        if p.endswith("— paid after payday"):
            days_of.setdefault(p.split(" — ")[0], []).append(
                D.fromisoformat(d0))
    for name, c in by_name.items():
        marks = [c["last_paid"]] + sorted(days_of.get(name, [])) + [end]
        with case.subTest(card=name, rounds=marks):
            case.assertTrue(all((b - a).days <= longest + pay_period
                                for a, b in zip(marks, marks[1:])))
    # paying sooner reaches a lower low, unless both lows are today's
    # balance (checking only rises from here)
    if due["min_date"] > usual["series"][0][0]:
        case.assertLess(usual["min"], due["min"])
    else:
        case.assertLessEqual(usual["min"], due["min"])
    case.assertLess(usual["cards_after_horizon"], due["cards_after_horizon"])


class ALookAlike(unittest.TestCase):
    def test_every_card_keeps_being_paid_in_rounds(self):
        # checking right after the next round sits above the floor; the
        # tax bill and the mortgage before the next paycheck take it below
        # the floor but not below $0: the round is affordable
        for seed in props.seeds(4):
            conn = _look_alike(seed)
            try:
                f = forecast.build(conn, today=TODAY)
            finally:
                conn.close()
            with self.subTest(seed=seed, log=f["card_habit_debug"]):
                assert_rounds_keep_coming(self, f)


class NotEveryCard(unittest.TestCase):
    def test_a_round_pays_the_cards_checking_can_cover(self):
        # checking that cannot clear all four cards and the bills before
        # the next paycheck pays the ones it can, the nearest statement
        # due first; the others wait
        conn = _look_alike(1, checking=2000)
        try:
            f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        first = f["card_habit_debug"][0]
        self.assertEqual(first["refused"], "trough")
        self.assertTrue(0 < len(first["paid"]) < len(first["eligible"]),
                        first)
