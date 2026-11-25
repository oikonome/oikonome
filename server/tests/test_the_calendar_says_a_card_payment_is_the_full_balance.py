"""The Bills calendar shows each card's payments as the Today page's
default view walks them (the usual walk's card rows: the same dates and
amounts), each with its card and how it is paid, so the two screens
agree. Figures are invented."""

import datetime as dt
import unittest
from unittest import mock

from oikonome.engine.compat import jsonb
from oikonome.web import api_today

from .util import make_db, write_config

TODAY = dt.date(2026, 11, 17)


class _Kept:
    """The test's connection, left open when the route closes it."""
    def __init__(self, conn):
        self._c = conn

    def __getattr__(self, k):
        return getattr(self._c, k)

    def close(self):
        pass


class CalendarCardWords(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0)
        self.due = TODAY + dt.timedelta(days=10)
        self.conn.execute(
            "INSERT INTO liabilities (account_id, raw) VALUES ('card', %s)",
            (jsonb({"last_statement_balance": 100.0,
                    "next_payment_due_date": self.due.isoformat()}),))

    def tearDown(self):
        self.conn.close()

    def test_the_card_event_is_the_usual_walks_payment(self):
        from oikonome.engine import forecast
        with mock.patch.object(api_today, "_conn",
                               return_value=_Kept(self.conn)), \
             mock.patch.object(api_today, "_today", return_value=TODAY):
            cal = api_today.calendar_api({"tenant_id": "x"}, days=35)
        f = forecast.build(self.conn, today=TODAY)
        want = [(d, a, p) for d, a, p in f["pace_usual"]["card_rows"]
                if d <= (TODAY + dt.timedelta(days=35)).isoformat()]
        got = [(c["date"], e["amount"], e["label"]) for c in cal["calendar"]
               for e in c["events"] if e["kind"] == "card"]
        self.assertTrue(want)
        self.assertEqual(got, want)

    def test_the_name_and_the_note_arrive_apart(self):
        """A narrow calendar cell cut the joined label mid-word ("· fu…"):
        the name and the note come as two fields, so each client places
        the note where it fits."""
        with mock.patch.object(api_today, "_conn",
                               return_value=_Kept(self.conn)), \
             mock.patch.object(api_today, "_today", return_value=TODAY):
            cal = api_today.calendar_api({"tenant_id": "x"}, days=35)
        evs = [e for c in cal["calendar"] for e in c["events"]]
        card = next(e for e in evs if e["kind"] == "card")
        self.assertEqual((card["name"], card["note"]),
                         ("Test Card", "statement due"))
        for e in evs:
            if e["kind"] != "card":
                self.assertIsNone(e["note"])
                self.assertEqual(e["name"], e["label"])


if __name__ == "__main__":
    unittest.main()


class ACardPaidBillSaysItsCard(unittest.TestCase):
    """A bill a card pays is listed on the calendar with its card, in the
    Today timeline's words; its figure and day are unchanged."""

    def test_the_bill_names_the_card_that_pays_it(self):
        from .util import add_bill, add_txn
        conn = make_db()
        try:
            write_config(conn, food_monthly=0, other_monthly=0)
            for m in (8, 9, 10):
                add_txn(conn, dt.date(2026, m, 20), 15.0, "CEDAR STREAMING",
                        account="card")
            add_bill(conn, "CEDAR STREAMING", 15.0,
                     next_due=dt.date(2026, 11, 20),
                     last_seen=dt.date(2026, 10, 20))
            with mock.patch.object(api_today, "_conn",
                                   return_value=_Kept(conn)), \
                 mock.patch.object(api_today, "_today", return_value=TODAY):
                cal = api_today.calendar_api({"tenant_id": "x"}, days=35)
        finally:
            conn.close()
        ev = next(e for c in cal["calendar"] for e in c["events"]
                  if e["name"] == "CEDAR STREAMING")
        self.assertEqual((ev["label"], ev["note"], ev["amount"]),
                         ("CEDAR STREAMING — on Test Card", "on Test Card",
                          -15.0))
