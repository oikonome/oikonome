"""A bill row says when it is due, and the attention queue says what the
ledger shows, in the server's words (specs/honest-figures.md, "A bill row
says when in the server's words").

Both clients used to count the days to a due date on the device and word
the countdown, its colour, the cycle bar and the queue's sentences each in
their own copy of the rule — from a clock that is not the household's day.
"""

import datetime as dt
import unittest

from oikonome import localtime
from oikonome.engine import budget
from oikonome.web import api, api_bills  # noqa: F401 — api first

from .util import add_bill, make_db, write_config

DAY = dt.date(2026, 4, 10)


def _row(due, **over):
    row = {"due_on": due.isoformat() if due else None, "used": None,
           "bill_type": "occurrence", "cadence_value": "MONTHLY:1",
           "by_month_day": None, "occurrences": 1, "paid": 0,
           "income": False, "withdrawn_on": None}
    row.update(over)
    return row


def _in(days):
    return DAY + dt.timedelta(days=days)


class DueWordsTests(unittest.TestCase):
    def words(self, row, live=True):
        return api_bills.due_words(row, DAY, live=live)

    def test_the_countdown_is_counted_from_the_households_day(self):
        for days, text, tone in [(0, "due today", "soon"),
                                 (1, "due tomorrow", "soon"),
                                 (3, "in 3 days", "soon"),
                                 (12, "in 12 days", "ok"),
                                 (-1, "1 day late", "late"),
                                 (-4, "4 days late", "late")]:
            w = self.words(_row(_in(days)))
            self.assertEqual((w["due_text"], w["due_tone"]), (text, tone), days)

    def test_income_says_today_and_tomorrow_without_due(self):
        self.assertEqual(self.words(_row(DAY, income=True))["due_text"], "today")
        self.assertEqual(self.words(_row(_in(1), income=True))["due_text"],
                         "tomorrow")

    def test_the_due_column_names_today_tomorrow_or_the_date(self):
        self.assertEqual(self.words(_row(DAY))["due_label"], "today")
        self.assertEqual(self.words(_row(_in(1)))["due_label"], "tomorrow")
        self.assertEqual(self.words(_row(_in(9)))["due_label"], "04/19/26")

    def test_a_late_charge_the_feed_withdrew_is_its_own_tone(self):
        w = self.words(_row(_in(-2), withdrawn_on=_in(-2).isoformat()))
        self.assertEqual((w["due_text"], w["due_tone"]),
                         ("2 days late", "withdrawn"))

    def test_a_paid_row_has_no_countdown_but_keeps_its_date_and_bar(self):
        w = self.words(_row(_in(20), paid=1))
        self.assertIsNone(w["due_text"])
        self.assertIsNone(w["due_tone"])
        self.assertEqual(w["due_label"], "04/30/26")
        self.assertIsNotNone(w["cycle_frac"])

    def test_the_bar_fills_as_the_date_approaches(self):
        self.assertAlmostEqual(self.words(_row(_in(30)))["cycle_frac"],
                               round(1 - 30 / 30.44, 4))
        self.assertEqual(self.words(_row(DAY))["cycle_frac"], 1.0)
        self.assertEqual(self.words(_row(_in(-5)))["cycle_frac"], 1.0)
        # a rule on two days of the month runs a half-month cycle
        w = self.words(_row(_in(5), by_month_day=[1, 15]))
        self.assertAlmostEqual(w["cycle_frac"], round(1 - 5 / 15.22, 4))

    def test_a_closed_month_an_envelope_and_no_date_say_nothing(self):
        empty = {"due_label": None, "due_text": None, "due_tone": None,
                 "cycle_frac": None}
        self.assertEqual(self.words(_row(_in(2)), live=False), empty)
        self.assertEqual(self.words(_row(_in(2), used=40.0)), empty)
        self.assertEqual(self.words(_row(_in(2), bill_type="envelope")), empty)
        self.assertEqual(self.words(_row(None)), empty)


class AttentionWordsTests(unittest.TestCase):
    def test_a_stale_bill_names_the_missed_cycle_and_the_newest_charge(self):
        row = {"health": "stale", "last_match": "2026-02-02",
               "last_seen": {"date": "2026-02-09", "amount": 31.0},
               "cycle_days": 30, "amount": 30.0}
        self.assertEqual(api_bills.attention_words(row),
                         "missed its ~03/11/26 cycle — nothing since 02/09/26")
        self.assertEqual(api_bills.attention_words(
            {"health": "stale", "amount": 30.0}),
            "missed its cycle — no matching charge on record")

    def test_a_mismatch_quotes_the_offer_or_the_last_charge(self):
        self.assertEqual(api_bills.attention_words(
            {"health": "mismatch", "amount": 58.0,
             "suggestion": {"action": "accept_amount", "amount": 64.0}}),
            "charges now run $64.00, bill says $58.00")
        self.assertEqual(api_bills.attention_words(
            {"health": "mismatch", "amount": 58.0,
             "last_seen": {"date": "2026-03-07", "amount": 71.0},
             "suggestion": {"action": "review"}}),
            "charges near $71.00 on 03/07/26, bill says $58.00 — amounts vary")

    def test_a_dismissed_row_keeps_its_line_but_leaves_the_queue(self):
        row = {"health": "drifting", "health_dismissed": True, "amount": 9.0}
        self.assertEqual(api_bills.attention_words(row),
                         "amount wandering — auto-corrects tonight")
        self.assertFalse(api_bills.queued(row))
        self.assertTrue(api_bills.queued({**row, "health_dismissed": False}))

    def test_a_healthy_or_withdrawn_row_has_no_line(self):
        for h in (None, "ok", "withdrawn", "off-ledger"):
            self.assertIsNone(api_bills.attention_words(
                {"health": h, "amount": 9.0}), h)


def _user(conn):
    tid = conn.execute(
        "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    return {"tenant_id": tid, "user_id": "u", "role": "owner",
            "email": "o@example.com"}


class BillsReplyTests(unittest.TestCase):
    def test_every_row_of_the_list_carries_its_words(self):
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        today = localtime.now_local(budget.load_config(conn)).date()
        add_bill(conn, "Kestrel Power", 140.0,
                 next_due=today + dt.timedelta(days=2))
        conn.commit()
        out = api_bills.bills_list(user=_user(conn), month=None)
        row = next(r for r in out["bills"] if r["payee"] == "Kestrel Power")
        self.assertEqual(row["due_text"], "in 2 days")
        self.assertEqual(row["due_tone"], "soon")
        self.assertIn("attention_text", row)


if __name__ == "__main__":
    unittest.main()
