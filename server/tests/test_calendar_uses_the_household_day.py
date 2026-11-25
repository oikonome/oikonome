"""The calendar strip runs on the household's day, like every other page.

Every live day-math site reads the household's configured timezone, never
the instance clock. Miss one endpoint and an evening request from a US
household on a UTC instance has its bill and card events computed and
labelled against tomorrow's date on that one screen, while Today, the
lenses and the bills list all agree on the local day.
"""

import datetime as dt
import unittest
import uuid
import zoneinfo
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_bill, seed_accounts, write_config

# a fixed local moment far from the process clock, so a leak of
# date.today() into either the walk or the "start" field shows up on
# any day this suite runs
LOCAL = dt.datetime(2026, 7, 15, 21, 30,
                    tzinfo=zoneinfo.ZoneInfo("Pacific/Kiritimati"))


class CalendarTodayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"cal-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=0, other_monthly=0,
                         timezone="Pacific/Kiritimati")
        finally:
            conn.close()

    def test_calendar_starts_on_the_households_day(self):
        self.assertNotEqual(LOCAL.date(), dt.date.today())
        with mock.patch("oikonome.localtime.now_local", return_value=LOCAL):
            body = self.client.get("/api/calendar?days=35").json()
        self.assertEqual(body["start"], "2026-07-15")
        # the walk ran on the same day: the card with no due date is
        # scheduled "tomorrow" relative to the household, not the server
        by_date = {d["date"]: d for d in body["calendar"]}
        self.assertIn("2026-07-16", by_date)
        self.assertEqual([e["label"] for e in by_date["2026-07-16"]["events"]],
                         ["Test Card — card payment"])
        self.assertTrue(all(d >= "2026-07-15" for d in by_date))

    def test_a_bill_due_today_sits_in_todays_cell(self):
        """The cash walk starts tomorrow and dates an unpaid bill due today
        there; a calendar shows a bill on the day it is owed, so today's
        cell is never empty while a bill is due today."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            add_bill(conn, "Water Works", 80, next_due=dt.date(2026, 7, 15))
            conn.commit()
        finally:
            conn.close()
        with mock.patch("oikonome.localtime.now_local", return_value=LOCAL):
            body = self.client.get("/api/calendar?days=35").json()
        by_date = {d["date"]: d for d in body["calendar"]}
        self.assertIn("Water Works",
                      [e["label"] for e in by_date["2026-07-15"]["events"]])
        self.assertNotIn(
            "Water Works",
            [e["label"] for e in by_date.get("2026-07-16", {"events": []})["events"]])

    def test_each_day_carries_its_in_and_out(self):
        """The phone lists the days in words at very large text ("+$1,250
        in, $86 out") and prints what the server sends: a day's money in
        and money out, each positive, whose difference is the net the
        grid cell shows."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            add_bill(conn, "Power Co", 60, next_due=dt.date(2026, 7, 20))
            conn.commit()
        finally:
            conn.close()
        with mock.patch("oikonome.localtime.now_local", return_value=LOCAL):
            body = self.client.get("/api/calendar?days=35").json()
        self.assertTrue(body["calendar"])
        for day in body["calendar"]:
            amounts = [e["amount"] for e in day["events"]]
            self.assertEqual(day["came_in"], round(sum(a for a in amounts if a > 0), 2))
            self.assertEqual(day["went_out"], round(-sum(a for a in amounts if a < 0), 2))
            self.assertGreaterEqual(day["came_in"], 0)
            self.assertGreaterEqual(day["went_out"], 0)
            self.assertAlmostEqual(day["total"], day["came_in"] - day["went_out"], places=2)
        power = next(d for d in body["calendar"] if d["date"] == "2026-07-20")
        self.assertEqual(power["went_out"], 60)


class EverythingOwedTodaySitsInTodaysCell(unittest.TestCase):
    """The cash walk starts tomorrow, so it dates everything owed today —
    a bill, a paycheck, a card payment the issuer says is due today —
    tomorrow. A calendar shows each on the day it is owed. A card with no
    due date stays on tomorrow: that day is the walk's assumption, not a
    date anyone gave."""

    @classmethod
    def setUpClass(cls):
        import json
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"cal2-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=0, other_monthly=0,
                         timezone="Pacific/Kiritimati")
            conn.execute(
                "INSERT INTO accounts (id,item_id,name,type,subtype,"
                "balance_current) VALUES ('card2','it1','Invented Rewards',"
                "'credit','credit card',120)")
            conn.execute(
                "INSERT INTO liabilities (account_id, raw) VALUES "
                "('card2', %s)",
                (json.dumps({"next_payment_due_date": "2026-07-15"}),))
            add_bill(conn, "Invented Payroll", 1500, frequency="WEEKLY",
                     interval=2, next_due=dt.date(2026, 7, 15), income=True,
                     last_seen=dt.date(2026, 7, 1))
            conn.commit()
        finally:
            conn.close()

    def test_a_paycheck_and_a_card_due_today_sit_in_todays_cell(self):
        with mock.patch("oikonome.localtime.now_local", return_value=LOCAL):
            body = self.client.get("/api/calendar?days=35").json()
        by_date = {d["date"]: [e["label"] for e in d["events"]]
                   for d in body["calendar"]}
        self.assertIn("Invented Payroll", by_date.get("2026-07-15", []))
        self.assertIn("Invented Rewards — statement due",
                      by_date.get("2026-07-15", []))
        tomorrow = by_date.get("2026-07-16", [])
        self.assertNotIn("Invented Payroll", tomorrow)
        self.assertNotIn("Invented Rewards — statement due", tomorrow)
        # no due date given: stays where the walk assumed it
        self.assertIn("Test Card — card payment", tomorrow)


if __name__ == "__main__":
    unittest.main()
