"""A bill offer's "next" date is never a day already past.

The finder stores the date it expected the series' next charge on; once
that day has gone by, the offer must not show it. `upcoming_due` is the finder's date rolled
forward by the offer's cadence (bills.offer_upcoming_due), and null for an
offer that has no date of its own."""

import datetime as dt
import unittest

from oikonome.engine import bills

from .util import add_bill, make_db, write_config

TODAY = dt.date(2026, 7, 15)


def _p(**over):
    p = {"kind": "add", "bill_type": "occurrence", "frequency": "WEEKLY",
         "interval": 1, "next_due": TODAY - dt.timedelta(days=2),
         "evidence": {}}
    p.update(over)
    return p


class UpcomingDueTests(unittest.TestCase):
    def test_a_weekly_offer_two_days_late_points_at_next_week(self):
        self.assertEqual(bills.offer_upcoming_due(_p(), TODAY),
                         TODAY + dt.timedelta(days=5))

    def test_a_monthly_offer_past_its_day_points_at_next_month(self):
        self.assertEqual(bills.offer_upcoming_due(
            _p(frequency="MONTHLY", next_due=dt.date(2026, 7, 3)), TODAY),
            dt.date(2026, 8, 3))

    def test_a_date_still_ahead_is_kept(self):
        ahead = TODAY + dt.timedelta(days=4)
        self.assertEqual(bills.offer_upcoming_due(_p(next_due=ahead), TODAY),
                         ahead)
        self.assertEqual(bills.offer_upcoming_due(_p(next_due=TODAY), TODAY),
                         TODAY)

    def test_offers_without_a_date_of_their_own_have_none(self):
        for kind in ("attach", "merchant", "remove"):
            self.assertIsNone(bills.offer_upcoming_due(_p(kind=kind), TODAY))
        self.assertIsNone(bills.offer_upcoming_due(
            _p(bill_type="envelope", frequency=None), TODAY))
        self.assertIsNone(bills.offer_upcoming_due(
            _p(frequency=None), TODAY))          # one-time, gone by

    def test_the_bills_reply_sends_it(self):
        from oikonome.web import api_bills
        conn = make_db()
        write_config(conn)
        self.addCleanup(conn.close)
        add_bill(conn, "Pine Ridge Clinic", 120.0)
        today = dt.date.today()
        bills._insert_proposal(
            conn, pid="prop:late", kind="add", payee="Lantern Cleaners",
            amount=40.0, frequency="WEEKLY", interval=1,
            next_due=today - dt.timedelta(days=2),
            evidence={"hits": 9, "cycle_days": 7})
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        out = api_bills.bills_list(user={"tenant_id": tid, "role": "owner",
                                         "id": None}, month=None)
        p = next(x for x in out["proposals"] if x["id"] == "prop:late")
        self.assertEqual(p["upcoming_due"],
                         (today + dt.timedelta(days=5)).isoformat())


if __name__ == "__main__":
    unittest.main()
