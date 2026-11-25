"""Each card is paid its own way (specs/today-figures.md, "Each card is
paid its own way"): a card on statement autopay is paid at each
statement's due date; a card the household pays right after payday is
paid in rounds, and only those cards are in a round and in its
affordability test; a card paid down a couple of weeks after each charge
by its lag; a card whose balance is carried by what the household has
been paying, the rest staying on the card. Rounds keep the household's
own spacing. Households are simulated day by day; figures and names are
invented."""

import datetime as dt
import random
import unittest

from oikonome.engine import business_days, forecast
from oikonome.engine.compat import jsonb

from . import props
from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date
PAY = "PAYMENT THANK YOU"


def _card(conn, cid, name):
    conn.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                 "balance_current) VALUES (%s,'it1',%s,'credit',"
                 "'credit card',0) ON CONFLICT (tenant_id, id) DO NOTHING",
                 (cid, name))


def _credit(conn, cid, d, amt):
    """A card payment: the credit on the card and its leg out of checking."""
    add_txn(conn, d, -round(amt, 2), PAY, account=cid,
            primary="LOAN_PAYMENTS",
            detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    add_txn(conn, d, round(amt, 2), "CARD PAYMENT", account="chk",
            primary="LOAN_PAYMENTS",
            detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")


def simulate(conn, cid, *, rng, how, days=200, per_day=(20, 70),
             paydays=(), close_day=10, due_day=5, gap_days=None,
             carry_payment=150.0, grace=None, today=TODAY):
    """Charge the card every day for `days` and pay it `how`: "autopay"
    (the statement balance at its due date), "rounds" (the whole balance
    the day after a payday, every `gap_days` at the least), "carried" (a
    fixed payment a month on the 20th). Leaves the card's balance and its
    issuer's facts as a feed would."""
    bal = 0.0
    stmts = {}
    last_round = None
    start = today - dt.timedelta(days=days)
    d = start
    issued = due = None
    while d < today:
        amt = float(rng.randint(*per_day))
        add_txn(conn, d, amt, f"MARKET {rng.randint(1, 9)}", account=cid)
        bal += amt
        if d.day == close_day:
            issued = d
            # an issuer that dates the due a fixed grace after the close
            # (the day of the month drifts across a short month), or on a
            # fixed day of the next month
            due = (d + dt.timedelta(days=grace) if grace is not None else
                   forecast._add_months(D(d.year, d.month, due_day), 1))
            stmts[due] = round(bal, 2)
        if how == "autopay" and d in stmts and stmts[d] > 0:
            _credit(conn, cid, d, stmts[d])
            bal -= stmts[d]
        if (how == "rounds" and (d - dt.timedelta(days=1)) in paydays
                and bal > 0 and (gap_days is None or last_round is None
                                 or (d - last_round).days >= gap_days)):
            _credit(conn, cid, d, bal)
            bal = 0.0
            last_round = d
        if how == "carried" and d.day == 20:
            _credit(conn, cid, d, carry_payment)
            bal -= carry_payment
        d += dt.timedelta(days=1)
    conn.execute("UPDATE accounts SET balance_current=%s WHERE id=%s",
                 (round(bal, 2), cid))
    conn.execute("INSERT INTO liabilities (account_id, raw) VALUES (%s, %s)",
                 (cid, jsonb({"last_statement_issue_date": issued.isoformat(),
                              "next_payment_due_date": due.isoformat(),
                              "last_statement_balance":
                                  stmts.get(due, round(bal, 2))})))
    return bal


def _household(seed, *, round_gap=None, checking=9000):
    rng = random.Random(seed)
    conn = make_db()
    write_config(conn, food_monthly=600, other_monthly=600)
    conn.execute("UPDATE accounts SET balance_available=%s, "
                 "balance_current=%s WHERE id='chk'", (checking, checking))
    conn.execute("UPDATE accounts SET name='Harbor card' WHERE id='card'")
    _card(conn, "auto", "Summit card")
    pays = []
    d = TODAY - dt.timedelta(days=200)
    while d < TODAY:
        if d.day in (1, 15):
            on = (d if business_days.is_business_day(d)
                  else business_days.previous_business_day(d))
            add_txn(conn, on, -2600.0, "NORTHSTAR PAYROLL", account="chk",
                    primary="INCOME", detailed="INCOME_WAGES")
            pays.append(on)
        d += dt.timedelta(days=1)
    add_bill(conn, "NORTHSTAR PAYROLL", 2600.0, income=True,
             frequency="MONTHLY", next_due=D(2026, 8, 1),
             last_seen=D(2026, 7, 1))
    simulate(conn, "card", rng=rng, how="rounds", paydays=set(pays),
             gap_days=round_gap)
    simulate(conn, "auto", rng=rng, how="autopay")
    return conn


class EachCard(unittest.TestCase):
    def test_an_autopay_card_and_a_payday_card_in_one_household(self):
        conn = _household(1)
        try:
            f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        modes = {c["card"]: c["mode"] for c in f["card_timing"]}
        self.assertEqual(modes, {"Harbor card": "rounds",
                                 "Summit card": "autopay"})
        w = f["pace_usual"]
        # the autopay card leaves only at its statements' due dates (the
        # 5th, rolled to a business day), never in a payday round
        auto = {d for d, _a, c in w["outflows"] if c == "auto"}
        dues = {business_days.next_business_day(forecast._add_months(
                    D(2026, 8, 5), k)).isoformat() for k in (-1, 0, 1)}
        self.assertTrue(auto and auto <= dues, (auto, dues))
        rounds = {p for _d, _a, p in w["card_rows"]
                  if p.endswith("paid after payday")}
        self.assertEqual(rounds, {"Harbor card — paid after payday"})
        # the note is written in sentences
        from oikonome.web import todayview
        note = todayview.card_timing_note(f)
        self.assertTrue(note.startswith("You usually pay Harbor card right "
                                        "after payday when checking can "
                                        "cover it"), note)
        self.assertTrue(note.endswith("; otherwise at the due date. Summit "
                                      "card is paid at the statement due "
                                      "date."), note)

    def test_the_autopay_signal_is_stable_and_a_hand_payer_never_reads_it(self):
        for seed in props.seeds(4):
            conn = _household(seed)
            try:
                cards = forecast.card_balances(conn, [], with_empty=True)
                t = forecast.card_timing(conn, cards, TODAY)
            finally:
                conn.close()
            with self.subTest(seed=seed):
                self.assertEqual((t["auto"]["autopaid"], t["auto"]["judged"]),
                                 (3, 3))
                self.assertEqual(t["card"]["autopaid"], 0)

    def test_each_card_keeps_its_own_spacing(self):
        # paid twice a month: Harbor card after every payday, Pine card
        # about every four weeks
        rng = random.Random(2)
        conn = _household(2)
        try:
            _card(conn, "pine", "Pine card")
            pays = [r["date"] for r in conn.execute(
                "SELECT date FROM transactions WHERE name = "
                "'NORTHSTAR PAYROLL' ORDER BY date").fetchall()]
            simulate(conn, "pine", rng=rng, how="rounds", paydays=set(pays),
                     gap_days=25)
            # paydays every two weeks ahead, so the two spacings differ
            conn.execute("DELETE FROM bills WHERE payee='NORTHSTAR PAYROLL'")
            add_bill(conn, "NORTHSTAR PAYROLL", 2600.0, income=True,
                     frequency="WEEKLY", interval=2,
                     next_due=D(2026, 7, 31), last_seen=D(2026, 7, 17))
            forecast.forget_learned()
            f = forecast.build(conn, today=TODAY)
            cards = forecast.card_balances(conn, [], with_empty=True)
            t = forecast.card_timing(conn, cards, TODAY,
                                     habit=forecast.card_payday_habit(
                                         conn, {}, TODAY, 9000))
        finally:
            conn.close()
        self.assertEqual((t["card"]["mode"], t["pine"]["mode"]),
                         ("rounds", "rounds"))
        self.assertLess(t["card"]["gap"], 20)
        self.assertGreaterEqual(t["pine"]["gap"], 25)
        per_card: dict = {}
        for d, cs in f["pace_usual"]["round_cards"].items():
            for c in cs:
                per_card.setdefault(c, []).append(D.fromisoformat(d))
        self.assertGreater(len(per_card["card"]), len(per_card["pine"]))
        from oikonome.web import todayview
        note = todayview.card_timing_note(f)
        self.assertIn("You usually pay Pine card about once a month, after "
                      "a payday when checking can cover it", note)
        self.assertIn("You usually pay Harbor card right after payday", note)
        for cid in ("card", "pine"):
            days = [t[cid]["last_paid"]] + per_card[cid]
            for x, y in zip(days, days[1:]):
                self.assertGreaterEqual(
                    (y - x).days, forecast.ROUND_GAP_SHARE * t[cid]["gap"],
                    (cid, days))


class ACarriedBalance(unittest.TestCase):
    def test_the_usual_walk_pays_what_the_household_has_been_paying(self):
        for seed in props.seeds(4):
            rng = random.Random(seed)
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
            with self.subTest(seed=seed, balance=bal):
                t = {c["card"]: c for c in f["card_timing"]}["Test Card"]
                self.assertEqual(t["mode"], "carried")
                self.assertEqual(t["payment"], 200.0)
                w = f["pace_usual"]
                outs = sum(a for _d, a, c in w["outflows"] if c == "card")
                # two months of the usual payment, not the balance
                self.assertEqual(round(outs, 2), 400.0)
                self.assertGreater(w["stays"]["card"], 0)
                # pay on the due date stays the full-payment what-if
                self.assertGreater(sum(a for _d, a, c in
                                       f["pace_due"]["outflows"]
                                       if c == "card"), outs)


if __name__ == "__main__":
    unittest.main()


class AutopayAcrossAShortMonth(unittest.TestCase):
    def test_a_due_date_a_grace_after_the_close_still_reads_autopay(self):
        # statements close on the 28th and fall due 25 days later: the
        # day of the month drifts by up to three across February
        march = D(2026, 3, 10)
        for seed in props.seeds(3):
            conn = make_db()
            try:
                write_config(conn, food_monthly=600, other_monthly=600)
                simulate(conn, "card", rng=random.Random(seed),
                         how="autopay", days=200, close_day=28, grace=25,
                         today=march)
                cards = forecast.card_balances(conn, [], with_empty=True)
                t = forecast.card_timing(conn, cards, march)["card"]
            finally:
                conn.close()
            with self.subTest(seed=seed):
                self.assertEqual(t["mode"], "autopay", t)


class TheModesAreAFunctionOfTheLedger(unittest.TestCase):
    def test_no_worker_remembers_a_mode(self):
        # a card linked six weeks ago: one statement judged, too little to
        # call it autopay. A worker that once held another mode for it
        # (any memory kept between builds) must not change what it reads.
        rng = random.Random(5)
        conn = make_db()
        try:
            write_config(conn, food_monthly=600, other_monthly=600)
            simulate(conn, "card", rng=rng, how="autopay", days=45)
            forecast.forget_learned()
            fresh = {c["card"]: c["mode"] for c in forecast.build(
                conn, today=TODAY)["card_timing"]}
            forecast.forget_learned()
            held = getattr(forecast, "_PREVIOUS_TIMING", None)
            if held is not None:
                tid = forecast._tenant(conn)
                held[tid] = (TODAY - dt.timedelta(days=1),
                             {"card": {"mode": "autopay"}})
            again = {c["card"]: c["mode"] for c in forecast.build(
                conn, today=TODAY)["card_timing"]}
        finally:
            conn.close()
        self.assertEqual(fresh, again)

    def test_fresh_processes_and_a_rebuild_after_a_write_agree(self):
        conn = _household(3)
        try:
            forecast.forget_learned()
            a = {c["card"]: c["mode"] for c in forecast.build(
                conn, today=TODAY)["card_timing"]}
            # a second worker (nothing kept), on an earlier day first
            forecast.forget_learned()
            forecast.build(conn, today=TODAY - dt.timedelta(days=20))
            b = {c["card"]: c["mode"] for c in forecast.build(
                conn, today=TODAY)["card_timing"]}
            # a write today, then a rebuild
            add_txn(conn, TODAY, 12.0, "MARKET 3", account="card",
                    txn_id="today-write")
            c = {c["card"]: c["mode"] for c in forecast.build(
                conn, today=TODAY)["card_timing"]}
        finally:
            conn.close()
        self.assertEqual(a, b)
        self.assertEqual(a, c)
