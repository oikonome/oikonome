""""Pay all cards today" is what its name says (specs/today-figures.md,
"Pay all cards today, then your usual"): every card's whole balance
leaves checking once, on the walk's first day, and the walk then goes on
exactly as "Your usual" does from that state — each card under its own
mode, today counting as each card's last payment. "Cash after card
balances" (pace_now) stays the cautious measure, checking less everything
owed on cards day by day: the list's second column and the quiet warning.
Households are generated; figures and names are invented."""

import datetime as dt
import random
import unittest

from oikonome.engine import forecast
from oikonome.web import report, todayview

from . import props
from .test_each_card_is_paid_its_own_way import _household, simulate
from .util import TODAY, make_db, write_config

D = dt.date
FIRST = (TODAY + dt.timedelta(days=1)).isoformat()


def _payoffs(w):
    return [(d, a, p) for d, a, p in w["card_rows"]
            if p.endswith("— paid in full")]


class APaydayPayer(unittest.TestCase):
    def test_one_payoff_then_the_usual_walk(self):
        for seed in props.seeds(4):
            conn = _household(seed, checking=12000)
            try:
                cards = {c["id"]: c for c in forecast.card_balances(
                    conn, [], with_empty=True)}
                f = forecast.build(conn, today=TODAY)
            finally:
                conn.close()
            usual, today_ = f["pace_usual"], f["pace_today"]
            with self.subTest(seed=seed):
                # each card's whole debt, once, on the first day
                got = sorted((p.split(" — ")[0], d, round(-a, 2))
                             for d, a, p in _payoffs(today_))
                want = sorted((c["name"], FIRST, round(c["debt"], 2))
                              for c in cards.values() if c["debt"] > 0.005)
                self.assertEqual(got, want)
                # never below cash after card balances
                self.assertGreaterEqual(today_["min"] + 0.005,
                                        f["pace_now"]["min"])
                # the round card counts as paid today: its first round
                # waits its own gap, so a round the usual walk takes soon
                # after today can be skipped — the two differ until both
                # have paid every card on the same round day, and are the
                # same walk from then on
                gap = next(t for t in f["card_timing"]
                           if t["mode"] == "rounds")["gap"]
                for r in today_["rounds"]:
                    self.assertGreaterEqual(
                        (D.fromisoformat(r) - TODAY).days,
                        forecast.ROUND_GAP_SHARE * gap, r)
                # one dollar, one exit: what each walk still owes at its
                # end differs only by when it was paid
                self.assertAlmostEqual(
                    usual["series"][-1][1] + usual["cards_after_horizon"],
                    today_["series"][-1][1] + today_["cards_after_horizon"],
                    places=1)

    def test_the_same_walk_once_both_have_paid_every_card(self):
        for seed in props.seeds(4):
            conn = _household(seed, checking=12000)
            try:
                # the round card alone: no statement of another card is
                # still ahead of the usual walk
                for t in ("transactions", "liabilities"):
                    conn.execute(f"DELETE FROM {t} WHERE account_id='auto'")
                conn.execute("DELETE FROM accounts WHERE id='auto'")
                f = forecast.build(conn, today=TODAY)
            finally:
                conn.close()
            usual, today_ = f["pace_usual"], f["pace_today"]
            both = [r for r in usual["rounds"] if r in today_["rounds"]]
            with self.subTest(seed=seed):
                self.assertTrue(both)
                a, b = dict(usual["series"]), dict(today_["series"])
                for d in a:
                    if d >= both[0]:
                        self.assertAlmostEqual(a[d], b[d], places=2, msg=d)
                # before it the usual walk pays a round the pay-today walk
                # skips (the card was paid today), so they differ
                if usual["rounds"][0] != both[0]:
                    self.assertNotIn(usual["rounds"][0], today_["rounds"])

    def test_the_issued_statement_of_an_autopay_card_is_settled(self):
        conn = _household(2, checking=12000)
        try:
            f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        due = [(d, a) for d, a, c in f["pace_usual"]["outflows"]
               if c == "auto"]
        mine = [(d, a) for d, a, c in f["pace_today"]["outflows"]
                if c == "auto"]
        # the usual walk pays the issued statement at its due date; the
        # pay-today walk paid it on its first day, and its first due-date
        # payment is only what was charged after today
        self.assertTrue(due)
        self.assertEqual(mine[0][0], FIRST)
        first_due = due[0][0]
        at_due = sum(a for d, a in mine if d == first_due)
        self.assertLess(at_due, due[0][1])


class ACarriedBalance(unittest.TestCase):
    def test_paid_in_full_then_the_usual_payment(self):
        rng = random.Random(5)
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=600)
            conn.execute("UPDATE accounts SET balance_available=9000, "
                         "balance_current=9000 WHERE id='chk'")
            bal = simulate(conn, "card", rng=rng, how="carried",
                           per_day=(5, 15), carry_payment=200.0)
            f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        w = f["pace_today"]
        self.assertEqual([round(-a, 2) for _d, a, _p in _payoffs(w)],
                         [round(bal, 2)])
        later = [a for d, a, p in w["card_rows"]
                 if p.endswith("— usual payment")]
        # after the payoff only new charges are carried, paid by the habit
        self.assertTrue(all(-a <= 200.0 + 0.005 for a in later), later)


class AnOnItsWayPayment(unittest.TestCase):
    def test_is_not_paid_twice(self):
        from .test_the_cash_walk_adds_up_for_generated_households import (
            ON_WAY, WalkAddsUp)
        for seed in props.seeds(3):
            t = WalkAddsUp()
            conn, h = t._household(seed)
            try:
                f = forecast.build(conn, today=TODAY, days=60)
            finally:
                conn.close()
            w = f["pace_today"]
            with self.subTest(seed=seed):
                self.assertEqual([p for _d, _a, p in w["checking_events"]
                                  ].count(ON_WAY), 1)
                # the payoff is the card's debt after the credit: the
                # payment on its way is not in it
                self.assertEqual(
                    round(-sum(a for _d, a, _p in _payoffs(w)), 2),
                    round(h["posted"] + h["pending"], 2))


class TheWords(unittest.TestCase):
    def test_the_selector_and_the_warning_say_what_is_true(self):
        conn = _household(1, checking=12000)
        try:
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        ctx = todayview.build_context(st)
        now = {s["key"]: s for s in ctx["scenarios"]}["now"]
        fc = st["forecast"]
        self.assertEqual(now["label"], "Pay all cards today")
        self.assertEqual(now["series"], fc["pace_today"]["series"])
        self.assertTrue(now["low_words"].startswith(
            "If you paid every card off today, your low would be "))
        # each row carries cash after card balances that day
        after = dict(fc["pace_now"]["series"])
        for r in now["rows"]:
            self.assertEqual(r["after_cards"], after.get(r["date"]))

    def test_the_warning_reads_cash_after_card_balances(self):
        fc = {"pace_usual": {"min": 400.0, "min_date": "2026-08-01"},
              "pace_now": {"min": -650.0, "min_date": "2026-08-03"},
              "pace_low": {"min": 100.0, "min_date": "2026-08-04"},
              "heavy_factor": {"basis": "household", "factor": 1.3}}
        band = todayview.cash_band(fc)
        self.assertEqual(band["kind"], "after_cards")
        self.assertEqual((band["low"], band["date"]), (-650.0, "2026-08-03"))
        self.assertEqual(band["text"], (
            "Your usual pattern stays above $0 (low $400 on 08/01/26). "
            "On 08/03/26 your card balances would be about $650 more than "
            "your checking — you could not clear every card that day."))


if __name__ == "__main__":
    unittest.main()
