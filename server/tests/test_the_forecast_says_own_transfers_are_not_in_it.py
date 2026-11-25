"""Money moved between the household's own accounts is not forecast, and
the page says so when it matters (specs/today-figures.md, "Own-account
transfers are not in the forecast"): one server sentence under the chart
for a household whose walked checking account sent or took in at least
OWN_TRANSFER_NOTE_MIN, net, between its own accounts over the last 60
days to the end of yesterday — the pairs the self-grading reads as the
household's own (forecast_score.own_transfer_ids). Nothing for a
household without them, for a payment to someone else, or when the
forecast walks transfers of its own (a savings plan). Figures and names
are invented."""

import datetime as dt
import unittest

from oikonome.engine import forecast_score
from oikonome.web import report, todayview

from .util import TODAY, add_txn, make_db, write_config

D = dt.date
XFER_OUT = ("TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER")
XFER_IN = ("TRANSFER_IN", "TRANSFER_IN_ACCOUNT_TRANSFER")


def _move(conn, day, amount, *, frm="chk", to="sav", paired=True):
    """`amount` from `frm` to `to`, both legs as a feed posts them."""
    add_txn(conn, day, amount, "ONLINE TRANSFER OUT", account=frm,
            primary=XFER_OUT[0], detailed=XFER_OUT[1])
    if paired:
        add_txn(conn, day, -amount, "ONLINE TRANSFER IN", account=to,
                primary=XFER_IN[0], detailed=XFER_IN[1])


class OwnTransfers(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=600, other_monthly=600)
        self.conn.execute(
            "INSERT INTO accounts (id, item_id, name, type, subtype, "
            "balance_current, balance_available) VALUES ('sav', 'it1', "
            "'Test Savings', 'depository', 'savings', 4000, 4000)")
        for day in range(1, 15):
            add_txn(self.conn, D(2026, 7, day), 25.0, "CORNER CAFE",
                    primary="FOOD_AND_DRINK")

    def tearDown(self):
        self.conn.close()

    def _note(self):
        st = report.gather(self.conn, TODAY)
        return st, todayview.build_context(st)

    def test_a_household_that_moved_money_out_is_told(self):
        _move(self.conn, D(2026, 6, 2), 720.0)
        _move(self.conn, D(2026, 7, 1), 640.0)
        # before the window: not counted
        _move(self.conn, D(2026, 5, 1), 3000.0)
        # today's own move is not yet a learned fact
        _move(self.conn, TODAY, 400.0)
        self.assertEqual(forecast_score.recent_own_transfers(
            self.conn, "chk", TODAY), {"out": 1360.0, "in": 0.0})
        st, ctx = self._note()
        want = ("Transfers between your own accounts are not in this "
                "forecast unless you set one up as a bill — over the last "
                "60 days they moved about $1,360 out of checking.")
        self.assertEqual(ctx["transfers_note"], want)
        _s, plain, html = report.build(st)
        self.assertIn(want, plain)
        self.assertIn(want, html)

    def test_money_moved_in_is_told_that_way(self):
        _move(self.conn, D(2026, 6, 20), 1200.0, frm="sav", to="chk")
        _st, ctx = self._note()
        self.assertEqual(
            ctx["transfers_note"],
            "Transfers between your own accounts are not in this forecast "
            "unless you set one up as a bill "
            "— over the last 60 days they moved about $1,200 into checking.")

    def test_money_that_went_and_came_back_is_told_both_ways(self):
        _move(self.conn, D(2026, 6, 4), 4000.0)
        _move(self.conn, D(2026, 6, 25), 3800.0, frm="sav", to="chk")
        _st, ctx = self._note()
        self.assertEqual(
            ctx["transfers_note"],
            "Transfers between your own accounts are not in this forecast "
            "unless you set one up as a bill "
            "— over the last 60 days about $4,000 left checking for your "
            "other accounts and about $3,800 came back.")

    def test_a_leg_dated_today_moves_nothing_and_the_fact_is_kept(self):
        from oikonome.engine import budget, forecast
        # checking's leg yesterday, the other account's leg today: as of
        # the end of yesterday it is not yet a pair
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 900.0,
                "ONLINE TRANSFER OUT", account="chk", primary=XFER_OUT[0],
                detailed=XFER_OUT[1])
        cfg = budget.load_config(self.conn)
        forecast.forget_learned()
        kept = forecast_score.kept_own_transfers(self.conn, cfg, "chk", TODAY)
        add_txn(self.conn, TODAY, -900.0, "ONLINE TRANSFER IN", account="sav",
                primary=XFER_IN[0], detailed=XFER_IN[1])
        fresh = forecast_score.recent_own_transfers(self.conn, "chk", TODAY)
        self.assertEqual(fresh, {"out": 0.0, "in": 0.0})
        self.assertEqual(kept, fresh)
        self.assertEqual(
            forecast_score.kept_own_transfers(self.conn, cfg, "chk", TODAY),
            fresh)

    def test_the_figure_is_not_a_function_of_row_order(self):
        rows = [(D(2026, 6, 2), 720.0), (D(2026, 6, 2), 720.0),
                (D(2026, 6, 20), 300.0)]
        for r in rows:
            _move(self.conn, *r)
        a = forecast_score.recent_own_transfers(self.conn, "chk", TODAY)
        other = make_db()
        try:
            other.execute(
                "INSERT INTO accounts (id, item_id, name, type, subtype, "
                "balance_current) VALUES ('sav', 'it1', 'Test Savings', "
                "'depository', 'savings', 4000)")
            for r in reversed(rows):
                _move(other, *r)
            b = forecast_score.recent_own_transfers(other, "chk", TODAY)
        finally:
            other.close()
        self.assertEqual(a, b)
        self.assertEqual(a, {"out": 1740.0, "in": 0.0})

    def test_a_bill_named_like_an_account_does_not_silence_it(self):
        """A name is no test of where a bill's money goes: the sentence
        stays, and carries the exception itself."""
        from .util import add_bill
        _move(self.conn, D(2026, 6, 2), 900.0)
        _move(self.conn, D(2026, 7, 1), 900.0)
        add_bill(self.conn, "TEST SAVINGS CLUB", 40.0, next_due=D(2026, 8, 1),
                 last_seen=D(2026, 7, 1))
        _st, ctx = self._note()
        self.assertIn("unless you set one up as a bill",
                      ctx["transfers_note"])

    def test_small_unpaired_or_none_says_nothing(self):
        _move(self.conn, D(2026, 6, 20), 300.0)
        # a transfer to someone else: no leg on the household's side
        _move(self.conn, D(2026, 7, 2), 2500.0, paired=False)
        _st, ctx = self._note()
        self.assertIsNone(ctx["transfers_note"])

    def test_a_forecast_that_walks_a_savings_plan_says_nothing(self):
        _move(self.conn, D(2026, 6, 2), 900.0)
        _move(self.conn, D(2026, 7, 1), 900.0)
        write_config(self.conn, food_monthly=600, other_monthly=600,
                     savings_goals=[{"name": "Rainy day", "target": 10000,
                                     "monthly_plan": 900,
                                     "account_id": "sav"}])
        st, ctx = self._note()
        self.assertTrue(any(p.endswith("(savings plan)")
                            for _d, _a, p in st["forecast"]["events"]))
        self.assertIsNone(ctx["transfers_note"])


if __name__ == "__main__":
    unittest.main()
