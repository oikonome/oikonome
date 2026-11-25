"""The "Cash is tight — $X left until your next paycheck" and "Short $X
today" notices assume every card's whole balance is paid now
(specs/today-figures.md, "The notice says it pays every card"): they now
say so in plain words, and they read the same card debt the cash card
prints — posted balances and pending charges alike, decided once by
forecast.card_debt_total. The notice used to subtract the posted balances
alone, so its dollar and the card tile beside it disagreed by every
pending charge. The `kind` values keep their meaning. Figures and names
are invented."""

import datetime as dt
import unittest

from oikonome.engine import budget, forecast
from oikonome.web import report, todayview

from .util import TODAY, add_bill, add_txn, make_db, write_config


class TheSentences(unittest.TestCase):
    def _note(self, **kw):
        base = {"state": "tight", "kind": "headroom", "amount": 640.0,
                "slow_to_per_day": None, "until": "2026-03-20", "ask": None,
                "card_debt": 2310.0}
        base.update(kw)
        return base

    def test_the_tight_notice_names_what_it_pays(self):
        self.assertEqual(
            todayview.cash_sentence(self._note()),
            "Cash is tight — $640 left until your next paycheck "
            "(03/20/26) if you paid off all $2,310 on your cards and the "
            "bills due before then.")
        self.assertEqual(
            todayview.cash_sentence(self._note(card_debt=0.0)),
            "Cash is tight — $640 left until your next paycheck "
            "(03/20/26) after the bills due before then.")

    def test_the_short_today_notice_names_what_it_pays(self):
        self.assertEqual(
            todayview.cash_sentence(self._note(
                state="critical", kind="today", amount=210.0)),
            "Short $210 today if you paid off all $2,310 on your cards "
            "and the bills due before your next paycheck (03/20/26). "
            "Cover it from savings.")
        self.assertEqual(
            todayview.cash_sentence(self._note(
                state="critical", kind="today", amount=210.0,
                card_debt=None)),
            "Short $210 today — checking doesn't cover the bills due "
            "before your next paycheck (03/20/26). Cover it from savings.")


class TheSameCardDebt(unittest.TestCase):
    def setUp(self):
        # checking $5,000; the card $250 posted plus $900 pending; a big
        # paycheck in four days: the headroom reads tight
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0,
                     budgeted_income_monthly=20000)
        self.payday = TODAY + dt.timedelta(days=4)
        add_bill(self.conn, "ACME HOLDINGS PAYROLL", 20000.0, income=True,
                 frequency="MONTHLY", next_due=self.payday,
                 last_seen=self.payday - dt.timedelta(days=31))
        add_txn(self.conn, TODAY, 900.0, "HARBOR OUTFITTERS", pending=1)
        self.d = report.gather(self.conn, TODAY)

    def tearDown(self):
        self.conn.close()

    def test_one_card_debt_figure(self):
        debt = forecast.card_debt_total(
            self.conn, budget.excluded_account_ids(self.d["config"]))
        self.assertAlmostEqual(debt, 1150.0, places=2)
        self.assertEqual(self.d["forecast"]["card_debt"], debt)
        self.assertEqual(self.d["runway"]["card_debt"], debt)
        ctx = todayview.build_context(self.d)
        note = ctx["cash_notice"]
        self.assertEqual(note["kind"], "headroom")
        self.assertEqual(note["card_debt"], debt)
        self.assertAlmostEqual(note["amount"], 5000.0 - 1150.0, places=2)
        self.assertIn("if you paid off all $1,150 on your cards", note["text"])
        _s, plain, html = report.build(self.d)
        self.assertIn(note["text"], plain)
        self.assertIn("card debt $1,150", plain)


    def test_the_runway_reuses_the_forecasts_figure(self):
        from unittest import mock
        with mock.patch.object(forecast, "card_debt_total",
                               side_effect=AssertionError("read twice")):
            d = report.gather(self.conn, TODAY)
        self.assertEqual(d["runway"]["card_debt"], d["forecast"]["card_debt"])


if __name__ == "__main__":
    unittest.main()
