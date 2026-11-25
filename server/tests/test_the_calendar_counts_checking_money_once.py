"""The Calendar counts checking money once (specs/today-figures.md, "The
Calendar counts checking money once"): a bill a card pays is listed on
the day the timeline lists it, as on the card, and is not in that day's
"out" — the card payment that pays it is the money leaving checking. It
used to be counted twice (on its own day and inside the card payment),
and dated by the checking rule (rolled to a business day) while every
timeline view dated it on the card's charge day. Figures and names are
invented."""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine import forecast
from oikonome.engine.compat import jsonb
from oikonome.web import api_today, report, todayview

from .test_card_spending_leaves_checking_when_the_card_is_paid import _pay_card
from .util import TODAY, add_bill, add_txn, make_db, write_config

D = dt.date
BILL = "PINECONE GAS UTILITY"


class _Kept:
    def __init__(self, c):
        self._c = c

    def __getattr__(self, k):
        return getattr(self._c, k)

    def close(self):
        pass


class ACardPaidBill(unittest.TestCase):
    """A card paid about two weeks after each charge, and a monthly bill
    due on a Saturday that the card pays."""

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
        d = TODAY - dt.timedelta(days=120)
        while d < TODAY:
            add_txn(c, d, 30.0, "CORNER MARKET", primary="FOOD_AND_DRINK")
            if d.day in (1, 15):
                _pay_card(c, d, 460.0)
            d += dt.timedelta(days=1)
        add_bill(c, BILL, 70.0, next_due=D(2026, 7, 25),
                 last_seen=D(2026, 6, 25))
        add_txn(c, D(2026, 6, 25), 70.0, BILL, account="chk")
        # two of its last three payments on the card
        add_txn(c, D(2026, 3, 20), 70.0, BILL)
        add_txn(c, D(2026, 4, 10), 70.0, BILL)
        forecast.forget_learned()

    def tearDown(self):
        self.conn.close()

    def _calendar(self):
        with mock.patch.object(api_today, "_conn",
                               return_value=_Kept(self.conn)), \
             mock.patch.object(api_today, "_today", return_value=TODAY):
            return api_today.calendar_api({"tenant_id": "x"}, days=35)

    def test_the_bill_is_on_the_card_not_in_the_days_out(self):
        cal = self._calendar()
        f = forecast.build(self.conn, today=TODAY)
        w = f["pace_usual"]
        charged = [b for b in w["charged_bills"] if b[2] == BILL]
        self.assertTrue(charged)
        day = charged[0][0]
        # the charge day is a Saturday: the checking rule would roll it
        self.assertEqual(day, "2026-07-25")
        cells = {c["date"]: c for c in cal["calendar"]}
        evs = [e for c in cal["calendar"] for e in c["events"]
               if e["name"] == BILL]
        self.assertEqual([(e["kind"], e["note"]) for e in evs],
                         [("on_card", "on Test Card")])
        self.assertIn(evs[0], cells[day]["events"])
        self.assertEqual(cells[day]["went_out"], 0.0)
        self.assertEqual(cells[day]["total"], 0.0)
        # every day's out is what leaves checking that day: its checking
        # bills and the card payments, never a card-paid bill
        end = (TODAY + dt.timedelta(days=35)).isoformat()
        want = round(-sum(a for d0, a, _p in w["checking_events"]
                          + w["card_rows"] if d0 <= end and a < 0), 2)
        self.assertAlmostEqual(
            sum(c["went_out"] for c in cal["calendar"]), want, places=2)

    def test_a_card_paid_bill_owed_today_sits_in_todays_cell(self):
        add_bill(self.conn, BILL, 70.0, next_due=TODAY,
                 last_seen=D(2026, 6, 15))
        forecast.forget_learned()
        cal = self._calendar()
        evs = {c["date"]: [e for e in c["events"] if e["name"] == BILL]
               for c in cal["calendar"]}
        self.assertTrue(evs.get(TODAY.isoformat()), evs)
        tomorrow = (TODAY + dt.timedelta(days=1)).isoformat()
        self.assertFalse(evs.get(tomorrow))
        e = evs[TODAY.isoformat()][0]
        # whichever field a phone prints says it is on the card
        self.assertEqual(e["label"], f"{BILL} — on Test Card")
        self.assertEqual(e["note"], "on Test Card")
        today_cell = next(c for c in cal["calendar"]
                          if c["date"] == TODAY.isoformat())
        self.assertEqual(today_cell["went_out"], round(-sum(
            x["amount"] for x in today_cell["events"]
            if x["kind"] != "on_card" and x["amount"] < 0), 2))

    def test_the_same_date_in_the_calendar_and_every_timeline_view(self):
        cal = self._calendar()
        on_cal = {c["date"] for c in cal["calendar"]
                  for e in c["events"] if e["name"] == BILL}
        self.assertTrue(on_cal)
        ctx = todayview.build_context(report.gather(self.conn, TODAY))
        for sc in ctx["scenarios"]:
            # the days both cover
            last = min(sc["rows"][-1]["date"],
                       (TODAY + dt.timedelta(days=35)).isoformat())
            with self.subTest(scenario=sc["key"]):
                self.assertEqual(
                    {r["date"] for r in sc["rows"]
                     if r["label"].startswith(BILL) and r["date"] <= last},
                    {d0 for d0 in on_cal if d0 <= last})
                self.assertIn("2026-07-25", on_cal)


if __name__ == "__main__":
    unittest.main()
