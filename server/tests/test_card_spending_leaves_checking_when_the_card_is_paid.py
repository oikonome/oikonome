"""Card spending leaves checking when the household pays the card, not on
the day of the charge (specs/today-figures.md, "Card spending leaves
checking when the household pays the card"): each bill's paying account
from its recent payments, each card's timing from its issuer's cycle and
the household's own payments, one rule for when a charged dollar leaves
(forecast.card_leaves_checking), and three scenarios — your usual, pay all
cards now, pay on the due date. Figures and names are invented."""

import datetime as dt
import random
import unittest

from oikonome.engine import budget, forecast
from oikonome.engine.compat import jsonb
from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date
CYCLE = {"issued": D(2026, 7, 10), "due": D(2026, 8, 5)}


def _timing(mode, lag=None, cycle=True):
    t = {"mode": mode, "lag": lag, "name": "Northwind card",
         "issued": None, "due": None}
    if cycle:
        t.update(CYCLE)
    return t


def _card(conn, cid, name="Harbor card"):
    conn.execute("INSERT INTO accounts (id,item_id,name,type,subtype,"
                 "balance_current) VALUES (%s,'it1',%s,'credit',"
                 "'credit card',0)", (cid, name))


def _pay_card(conn, day, amount, account="card"):
    add_txn(conn, day, -amount, "PAYMENT THANK YOU", account=account,
            primary="LOAN_PAYMENTS",
            detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")


class TheRule(unittest.TestCase):
    leave = staticmethod(forecast.card_leaves_checking)

    def test_a_statement_card_pays_each_charge_at_its_statements_due_date(self):
        t = _timing("statement")
        # on the closing day: this statement; the day after: the next one,
        # due on a Saturday before Labor Day, so the Tuesday after
        self.assertEqual(self.leave(t, D(2026, 7, 10), TODAY), D(2026, 8, 5))
        self.assertEqual(self.leave(t, D(2026, 7, 11), TODAY), D(2026, 9, 8))

    def test_a_card_paid_sooner_is_paid_its_lag_after_never_past_the_due(self):
        t = _timing("lag", 15.0)
        self.assertEqual(self.leave(t, D(2026, 7, 16), TODAY), D(2026, 7, 31))
        # a Sunday rolls to Monday
        self.assertEqual(self.leave(t, D(2026, 7, 25), TODAY), D(2026, 8, 10))
        # the statement holding the charge is due before charge + lag
        t30 = _timing("lag", 30.0)
        self.assertEqual(self.leave(t30, D(2026, 7, 9), TODAY), D(2026, 8, 5))
        # never before tomorrow
        self.assertEqual(self.leave(t, D(2026, 6, 20), TODAY),
                         TODAY + dt.timedelta(days=1))
        # no cycle: the lag alone
        self.assertEqual(self.leave(_timing("lag", 15.0, cycle=False),
                                    D(2026, 7, 16), TODAY), D(2026, 7, 31))

    def test_pay_on_the_due_date_ignores_the_habit(self):
        t = _timing("lag", 15.0)
        self.assertEqual(self.leave(t, D(2026, 7, 16), TODAY, "due"),
                         D(2026, 9, 8))
        self.assertEqual(self.leave(_timing("charge", cycle=False),
                                    D(2026, 7, 20), TODAY, "due"),
                         D(2026, 7, 20))

    def test_a_longer_lag_never_pays_a_dollar_sooner(self):
        rng = random.Random(7)
        for _ in range(400):
            d = TODAY + dt.timedelta(days=rng.randint(-40, 60))
            l1 = float(rng.randint(0, 40))
            l2 = l1 + rng.randint(0, 20)
            for cyc in (True, False):
                self.assertLessEqual(
                    self.leave(_timing("lag", l1, cyc), d, TODAY),
                    self.leave(_timing("lag", l2, cyc), d, TODAY))

    def test_a_charge_belongs_to_the_first_statement_closing_on_or_after_it(self):
        t = _timing("statement")
        self.assertEqual(forecast.statement_of(t, D(2026, 7, 10)),
                         (D(2026, 7, 10), D(2026, 8, 5)))
        self.assertEqual(forecast.statement_of(t, D(2026, 6, 11)),
                         (D(2026, 7, 10), D(2026, 8, 5)))
        self.assertEqual(forecast.statement_of(t, D(2026, 6, 10)),
                         (D(2026, 6, 10), D(2026, 7, 5)))
        self.assertIsNone(forecast.statement_of(_timing("charge", cycle=False),
                                                TODAY))


class PayingAccounts(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        _card(self.conn, "card2")
        self.cfg = budget.load_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _pays(self, payee, amount=60.0, **kw):
        add_bill(self.conn, payee, amount, next_due=D(2026, 7, 25),
                 last_seen=D(2026, 6, 25), **kw)
        bills_ = [b for b in budget._recurring_bills(self.conn)
                  if b["payee"] == payee]
        return forecast.bill_paying_accounts(self.conn, self.cfg, TODAY,
                                             bills_)[payee]

    def test_the_majority_of_the_last_three(self):
        for day, acct in ((D(2026, 3, 25), "card2"), (D(2026, 4, 25), "card"),
                          (D(2026, 5, 25), "card2"), (D(2026, 6, 25), "card")):
            add_txn(self.conn, day, 60.0, "RIVERTOWN WATER", account=acct)
        # the last three: card2, card, card -> card
        self.assertEqual(self._pays("RIVERTOWN WATER"), "card")

    def test_a_tie_goes_to_the_most_recent(self):
        add_txn(self.conn, D(2026, 5, 25), 60.0, "RIVERTOWN WATER",
                account="card2")
        add_txn(self.conn, D(2026, 6, 25), 60.0, "RIVERTOWN WATER",
                account="card")
        self.assertEqual(self._pays("RIVERTOWN WATER"), "card")

    def test_checking_no_payment_or_an_excluded_card_walk_from_checking(self):
        add_txn(self.conn, D(2026, 6, 25), 60.0, "RIVERTOWN WATER",
                account="chk")
        self.assertIsNone(self._pays("RIVERTOWN WATER"))
        self.assertIsNone(self._pays("PINE VALLEY GAS", 45.0))
        add_txn(self.conn, D(2026, 6, 20), 30.0, "CEDAR STREAMING",
                account="card2")
        write_config(self.conn, excluded_accounts=["card2"])
        self.cfg = budget.load_config(self.conn)
        self.assertIsNone(self._pays("CEDAR STREAMING", 30.0))

    def test_a_yearly_bill_follows_its_one_payment(self):
        add_txn(self.conn, D(2025, 9, 1), 240.0, "SUMMIT INSURANCE",
                account="card2")
        add_bill(self.conn, "SUMMIT INSURANCE", 240.0, frequency="YEARLY",
                 next_due=D(2026, 9, 1), last_seen=D(2025, 9, 1))
        bills_ = [b for b in budget._recurring_bills(self.conn)
                  if b["payee"] == "SUMMIT INSURANCE"]
        self.assertEqual(forecast.bill_paying_accounts(
            self.conn, self.cfg, TODAY, bills_)["SUMMIT INSURANCE"], "card2")

    def test_a_payment_more_than_400_days_old_is_no_evidence(self):
        add_txn(self.conn, TODAY - dt.timedelta(days=401), 60.0,
                "RIVERTOWN WATER", account="card2")
        self.assertIsNone(self._pays("RIVERTOWN WATER"))


class TheWalk(unittest.TestCase):
    """A household paying its card about two weeks after each charge."""

    def setUp(self):
        self.conn = c = make_db()
        write_config(c, food_monthly=600, other_monthly=600)
        c.execute("UPDATE accounts SET balance_available=6000, "
                  "balance_current=6000 WHERE id='chk'")
        c.execute("UPDATE accounts SET balance_current=420 WHERE id='card'")
        c.execute("INSERT INTO liabilities (account_id, raw) VALUES "
                  "('card', %s)", (jsonb({
                      "last_statement_balance": 380.0,
                      "last_statement_issue_date": "2026-07-10",
                      "next_payment_due_date": "2026-08-05"}),))
        # four months of charges, paid by hand about two weeks after
        d = TODAY - dt.timedelta(days=120)
        while d < TODAY:
            add_txn(c, d, 30.0, "CORNER MARKET", primary="FOOD_AND_DRINK")
            if d.day in (1, 15):
                # a little under what is charged, never far under: a card
                # paid down, not a carried balance
                _pay_card(c, d, 460.0)
            d += dt.timedelta(days=1)
        add_bill(c, "RIVERTOWN WATER", 60.0, next_due=D(2026, 7, 25),
                 last_seen=D(2026, 6, 25))
        add_txn(c, D(2026, 6, 25), 60.0, "RIVERTOWN WATER", account="chk")

    def tearDown(self):
        self.conn.close()

    def test_the_card_is_paid_by_its_lag_and_every_dollar_leaves_once(self):
        f = forecast.build(self.conn, today=TODAY)
        t = {c["card"]: c for c in f["card_timing"]}["Test Card"]
        self.assertEqual(t["mode"], "lag")
        self.assertLess(t["lag"], 20)
        _check_once(self, f)

    def test_moving_a_bill_onto_a_card_lowers_no_day(self):
        before = forecast.build(self.conn, today=TODAY)
        # two of its last three payments on the card, older than the
        # window the card's timing is read over (so the timing holds)
        add_txn(self.conn, D(2026, 3, 20), 60.0, "RIVERTOWN WATER")
        add_txn(self.conn, D(2026, 4, 10), 60.0, "RIVERTOWN WATER")
        after = forecast.build(self.conn, today=TODAY)
        self.assertEqual({b[2] for b in after["pace_usual"]["charged_bills"]},
                         {"RIVERTOWN WATER"})
        self.assertNotIn("RIVERTOWN WATER",
                         [p for _d, _a, p in
                          after["pace_usual"]["checking_events"]])
        for (d0, b0), (d1, b1) in zip(before["pace_usual"]["series"],
                                      after["pace_usual"]["series"]):
            self.assertGreaterEqual(b1 + 0.005, b0, d0)
        self.assertGreaterEqual(after["cards_after_horizon"] + 0.005,
                                before["cards_after_horizon"])
        # cash after card balances is unchanged by card timing
        self.assertEqual(after["pace_now"]["series"],
                         before["pace_now"]["series"])
        _check_once(self, after)

    def test_figures_that_read_other_walks_do_not_move(self):
        st = report.gather(self.conn, TODAY)
        ctx = todayview.build_context(st)
        fc = st["forecast"]
        # the headline and "checking at month end" read the usual walk
        self.assertEqual(ctx["month_end"]["balance"],
                         dict(fc["pace_usual"]["series"])["2026-07-31"])
        self.assertEqual(ctx["scenarios"][0]["key"], "usual")
        # one label, one figure: only the usual walk's month-end row is
        # called "checking at month end", the tile's words
        ends = {sc["key"]: [r["label"] for r in sc["rows"]
                            if r["kind"] == "month_end"]
                for sc in ctx["scenarios"]}
        self.assertEqual(ends, {
            "usual": ["checking at month end"],
            "now": ["checking at month end — pay all cards today"],
            "due": ["checking at month end — pay on the due date"]})
        self.assertEqual(ctx["scenarios"][0]["series"],
                         fc["pace_usual"]["series"])
        # the card debt tile, headroom and the calendar's full-balance
        # card events are not the card-aware walk's
        self.assertAlmostEqual(fc["card_debt"], 420.0)
        self.assertEqual([(d, a) for d, a, _p in fc["card_events"]],
                         [("2026-08-05", -420.0)])
        self.assertEqual(ctx["day"]["headroom"],
                         st["runway"]["checking"] - st["runway"]["card_debt"]
                         - st["runway"]["due_total"])
        # three lows side by side, each in the server's words
        self.assertEqual([lo["label"] for lo in ctx["fc_lows"]],
                         ["Your usual", "Pay all cards today",
                          "Pay on the due date"])
        _s, plain, html = report.build(st)
        for lo in ctx["fc_lows"]:
            # a low that ends its own sentence gives up its period to the
            # cards' clause in the plain text
            self.assertIn(lo["words"].rstrip("."), plain)
            self.assertIn(lo["words"].replace("$", "$"), html)
        self.assertIn(ctx["card_timing_note"], plain)
        self.assertTrue(ctx["card_timing_note"].startswith(
            "Card spending reaches checking about "), ctx["card_timing_note"])


def _check_once(test, f):
    """Per card, every charge the usual and due walks make inside the
    horizon plus the card's debt leaves checking once: in a card outflow
    inside the horizon, or in cards_after_horizon."""
    for key in ("pace_usual", "pace_due"):
        w = f[key]
        outs = sum(a for _d, a, _c in w["outflows"])
        after = sum(w["after"].values())
        test.assertAlmostEqual(w["cards_after_horizon"], after, places=2)
        # what was charged = debt + card pace share + card bills (the
        # envelope drip is none here)
        rate = f["pace_basis"]["daily"]
        shares = {c["card"]: c for c in f["card_timing"]}
        test.assertTrue(shares)
        charged_bills = -sum(a for _d, a, _p, _c in w["charged_bills"])
        card_share = (outs + after - f["card_debt"] - charged_bills) / (
            rate * f["days"])
        test.assertTrue(0.0 <= card_share <= 1.0 + 1e-9, (key, card_share))


if __name__ == "__main__":
    unittest.main()


class ADueDateIsPairedWithItsOwnStatement(unittest.TestCase):
    """Once a due date passes, an issuer reports the next cycle's due date
    while its last statement date is still the old one. A due date more
    than a cycle after the statement date belongs to the open statement
    (forecast.card_cycle)."""

    def _card(self, issued, due, posted=500.0, stmt=300.0):
        conn = make_db()
        write_config(conn, food_monthly=0, other_monthly=0)
        conn.execute("UPDATE accounts SET balance_current=%s "
                     "WHERE id='card'", (posted,))
        conn.execute("INSERT INTO liabilities (account_id, raw) VALUES "
                     "('card', %s)", (jsonb({
                         "last_statement_balance": stmt,
                         "last_statement_issue_date": issued,
                         "next_payment_due_date": due}),))
        return conn

    def test_a_reported_due_date_a_cycle_on_is_the_open_statements(self):
        conn = self._card("2026-06-08", "2026-08-03")
        try:
            cards = forecast.card_balances(conn, [], with_empty=True)
            t = forecast.card_timing(conn, cards, TODAY)["card"]
            f = forecast.build(conn, today=TODAY)
        finally:
            conn.close()
        self.assertEqual((t["issued"], t["due"], t["stmt_lag"]),
                         (D(2026, 6, 8), D(2026, 7, 3), 40))
        self.assertEqual(forecast.statement_of(t, D(2026, 7, 10)),
                         (D(2026, 8, 8), D(2026, 9, 3)))
        # the closed statement's due date has passed: what is owed of it
        # leaves tomorrow; the rest is the open statement's, due 08/03
        self.assertEqual([(d, a) for d, a, _p in f["card_events_stmt"]],
                         [("2026-07-16", -300.0), ("2026-08-03", -200.0)])

    def test_a_due_date_inside_the_cycle_is_the_closed_statements(self):
        conn = self._card("2026-07-04", "2026-08-01")
        try:
            cards = forecast.card_balances(conn, [], with_empty=True)
            t = forecast.card_timing(conn, cards, TODAY)["card"]
        finally:
            conn.close()
        self.assertEqual((t["issued"], t["due"], t["stmt_lag"]),
                         (D(2026, 7, 4), D(2026, 8, 1), 43))
