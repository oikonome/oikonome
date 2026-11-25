"""Today's cash figures are decided on the server and only displayed by
the clients: the cash notice (state, the dollar it quotes, the date it
runs to, the slow-down rate), the month-end tiles, the day meter and the
recent variable spending. The tight notice is about the paycheck horizon
("left until your next paycheck"); the tile is about month end ("left at
month end after cards & bills") — the same words once named both. The
page payload, the email HTML and the plain text carry the same values.
Figures are invented."""

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import report, todayview

from .util import (TODAY, _ensure_db, add_bill, add_txn, make_db,
                   seed_accounts, write_config)


class TightNoticeTests(unittest.TestCase):
    def setUp(self):
        # checking $5,000, card $250, no bill before the paycheck, no
        # variable burn: headroom $4,750 against a big paycheck reads tight
        self.conn = make_db()
        write_config(self.conn, food_monthly=0, other_monthly=0,
                     budgeted_income_monthly=20000)
        self.payday = TODAY + dt.timedelta(days=4)
        add_bill(self.conn, "ACME HOLDINGS PAYROLL", 20000.0, income=True,
                 frequency="MONTHLY", next_due=self.payday,
                 last_seen=self.payday - dt.timedelta(days=31))
        add_txn(self.conn, TODAY - dt.timedelta(days=1), 30.0,
                "CORNER GROCER", primary="FOOD_AND_DRINK")
        self.d = report.gather(self.conn, TODAY)

    def tearDown(self):
        self.conn.close()

    def test_the_notice_is_sent_with_its_amount_and_date(self):
        note = todayview.build_context(self.d)["cash_notice"]
        self.assertEqual(note["state"], "tight")
        self.assertEqual(note["kind"], "headroom")
        self.assertAlmostEqual(note["amount"], 4750.0, places=2)
        self.assertEqual(note["until"], self.payday.isoformat())
        self.assertIsNone(note["ask"])

    def test_the_tight_notice_and_the_tile_say_different_horizons(self):
        _s, plain, html = report.build(self.d)
        self.assertIn("left until your next paycheck", plain)
        self.assertIn("left until your next paycheck", html)
        self.assertNotIn("left after cards & bills before", plain)
        self.assertIn("checking at month end", plain)
        self.assertIn("checking at month end", html)

    def test_the_month_end_tiles_are_status_figures(self):
        rw = self.d["runway"]
        self.assertAlmostEqual(
            self.d["after_cards_bills"],
            rw["checking"] - rw["card_debt"] - self.d["bills_remaining_month"],
            places=2)
        self.assertAlmostEqual(
            self.d["after_cards_bills_paychecks"],
            self.d["after_cards_bills"] + self.d["income_due_total"],
            places=2)
        _s, plain, _h = report.build(self.d)
        self.assertIn(report._money(self.d["after_cards_bills_paychecks"]),
                      plain)

    def test_a_past_day_has_no_month_end_tiles(self):
        d = report.gather(self.conn, TODAY - dt.timedelta(days=2),
                          live=False)
        self.assertIsNone(d["after_cards_bills"])
        self.assertIsNone(d["after_cards_bills_paychecks"])


class TodayPayloadTests(unittest.TestCase):
    """The page payload carries each figure, equal to the context's."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"figs-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            import datetime as _dt
            today = _dt.date.today()
            add_bill(conn, "ROOFTOP RENT", 1500.0,
                     next_due=today.replace(day=1))
            add_txn(conn, today, 42.0, "CORNER GROCER",
                    primary="FOOD_AND_DRINK")
        finally:
            conn.close()
        cls.client.post("/api/settings", json={
            "budgeted_income_monthly": 6000, "food_monthly": 800,
            "other_monthly": 700,
            "savings_goals": [
                {"name": "Savings", "target": 0, "monthly_plan": 300,
                 "tokens": [], "start_balance": 0},
                {"name": "Trip fund", "target": 0, "monthly_plan": 200,
                 "tokens": [], "start_balance": 0}]})
        cls.d = cls.client.get("/api/today/full").json()

    def test_the_planner_figures_ride_the_payload(self):
        d = self.d
        self.assertEqual({g["name"] for g in d["plan_goals"]},
                         {"Savings", "Trip fund"})
        self.assertEqual(d["avg_bills_count"], 1)
        self.assertAlmostEqual(d["savings_plan"], 500.0, places=2)

    def test_a_ledger_begun_this_month_says_where_the_pace_runs_from(self):
        # the household's ledger began today: the progress line's budget
        # is that day's share of the month, and the payload names the day
        # so the line can say so (never under the plan's "to spend" words)
        import datetime as _dt
        today = _dt.date.fromisoformat(self.d["date"])
        self.assertEqual(self.d["paced_from"],
                         today.isoformat() if today.day > 1 else None)

    def test_the_plan_rows_and_their_total_are_sent(self):
        d = self.d
        labels = [r["label"] for r in d["plan_rows"]]
        self.assertEqual(labels[0], "Food")
        self.assertIn("Bills", labels)
        sav = next(r for r in d["plan_rows"]
                   if r["label"] == "Savings / Investment")
        self.assertAlmostEqual(sav["budget"], d["savings_plan"], places=2)
        judged = [r for r in d["plan_rows"] if r["judged"]]
        self.assertAlmostEqual(d["plan_total"]["budget"],
                               sum(r["budget"] for r in judged), places=2)

    def test_the_cash_figures_ride_the_payload(self):
        d = self.d
        rw = d["runway"]
        self.assertAlmostEqual(
            d["after_cards_bills"],
            rw["checking"] - rw["card_debt"] - d["bills_remaining_month"],
            places=2)
        self.assertIn("state", d["cash_notice"])
        self.assertEqual(set(d["day_meter"]), {"spent", "allow"})
        self.assertAlmostEqual(d["recent_variable_spending"], 42.0,
                               places=2)


if __name__ == "__main__":
    unittest.main()
