"""Without a paycheck figure, the Cash timeline's month-end tile says WHY,
and its figure stays the cash one (checking − card debt − bills still due).

A household whose income is a budgeted figure (an income scenario or the
monthly amount) has no paycheck schedule to forecast from, and one that
switched its income series off has asked the budget to ignore them. Telling
either to "add your income" asks for something they already have. The
reason is decided once, server-side (`income_due_note`), and the Today
payload, the email HTML and its plain-text mirror all read that key."""

import calendar
import datetime as dt
import unittest
import uuid

from oikonome.web import report

from .util import TODAY, add_bill, add_txn, make_db, write_config

MONTH_END = dt.date(TODAY.year, TODAY.month,
                    calendar.monthrange(TODAY.year, TODAY.month)[1])
PAYROLL = "ACME HOLDINGS PAYROLL"

NONE_TEXT = "no paycheck set up"
BUDGETED_TEXT = "your income is budgeted, not scheduled"
OFF_TEXT = "your income is switched off under Bills &"


def _shape(conn, shape):
    """Configure one of the four household shapes on a fresh tenant."""
    cfg = {}
    if shape in ("feed", "off"):
        payday = TODAY + dt.timedelta(days=1)
        add_bill(conn, PAYROLL, 2400.0, income=True, frequency="WEEKLY",
                 interval=2, next_due=payday)
    if shape == "off":
        cfg["disabled_bills"] = [PAYROLL]
        # a budgeted figure too: the switch-off is still the reason named
        cfg["budgeted_income_monthly"] = 5100
    if shape == "budgeted":
        cfg["income_scenarios"] = [{"name": "main", "take_home": 2550,
                                    "cadence": "biweekly"}]
        cfg["active_scenario"] = "main"
    write_config(conn, **cfg)


class CashTileWordingTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        add_txn(self.conn, TODAY - dt.timedelta(days=3), 40.0, "GROCER")

    def tearDown(self):
        self.conn.close()

    def _render(self, shape):
        _shape(self.conn, shape)
        d = report.gather(self.conn, TODAY)
        subj, plain, html = report.build(d)
        return d, plain, html

    def _assert_cash_figure(self, d, plain, html):
        rw = d["runway"]
        after = rw["checking"] - rw["card_debt"] - d["bills_remaining_month"]
        self.assertIsNone(d["income_due_total"])
        self.assertIn("after cards & bills " + report._money(after), plain)
        self.assertIn("after cards &amp; bills", html)
        self.assertNotIn("paychecks still due", plain)
        self.assertNotIn("after cards, bills &amp; paychecks", html)

    def test_no_income_at_all_asks_for_it(self):
        d, plain, html = self._render("none")
        self.assertEqual(d["income_due_note"], "none")
        self._assert_cash_figure(d, plain, html)
        self.assertIn(NONE_TEXT, plain)
        self.assertIn(NONE_TEXT, html)

    def test_budgeted_income_is_named_not_asked_for(self):
        d, plain, html = self._render("budgeted")
        self.assertEqual(d["income_due_note"], "budgeted")
        self._assert_cash_figure(d, plain, html)
        for text in (plain, html):
            self.assertIn(BUDGETED_TEXT, text)
            self.assertNotIn(NONE_TEXT, text)

    def test_switched_off_series_is_named_not_asked_for(self):
        d, plain, html = self._render("off")
        self.assertEqual(d["income_due_note"], "off")
        self._assert_cash_figure(d, plain, html)
        self.assertIn(OFF_TEXT, plain)
        self.assertIn(OFF_TEXT.replace("&", "&amp;"), html)
        for text in (plain, html):
            self.assertNotIn(NONE_TEXT, text)
            self.assertNotIn(BUDGETED_TEXT, text)

    def test_a_paycheck_feed_carries_no_note(self):
        if TODAY + dt.timedelta(days=1) > MONTH_END:
            self.skipTest("no payday left in this month from today")
        d, plain, html = self._render("feed")
        self.assertIsNone(d["income_due_note"])
        self.assertIsNotNone(d["income_due_total"])
        for text in (plain, html):
            self.assertNotIn(NONE_TEXT, text)
            self.assertNotIn(BUDGETED_TEXT, text)
            self.assertNotIn("your income is switched off", text)

    def test_a_past_day_carries_no_note(self):
        _shape(self.conn, "budgeted")
        d = report.gather(self.conn, TODAY - dt.timedelta(days=2), live=False)
        self.assertIsNone(d.get("income_due_note"))


class TodayPayloadCarriesTheNoteTests(unittest.TestCase):
    """The SPA and mobile read the key off /api/today/full — they never
    re-derive it from config."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        from .util import _ensure_db
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from fastapi.testclient import TestClient

        from oikonome.web.app import app
        cls.client = TestClient(app)

    def _payload(self, shape):
        from oikonome.db import tenancy

        from .util import seed_accounts
        self.client.cookies.clear()
        r = self.client.post(
            "/api/signup",
            data={"email": f"ctw-{uuid.uuid4().hex[:8]}@x.dev",
                  "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            add_txn(conn, TODAY - dt.timedelta(days=3), 40.0, "GROCER")
            _shape(conn, shape)
        finally:
            conn.close()
        return self.client.get("/api/today/full").json()

    def test_each_shape_reaches_the_payload(self):
        expect = {"none": "none", "budgeted": "budgeted", "off": "off"}
        if TODAY + dt.timedelta(days=1) <= MONTH_END:
            expect["feed"] = None
        for shape, key in expect.items():
            with self.subTest(shape=shape):
                self.assertEqual(self._payload(shape).get("income_due_note"),
                                 key)


if __name__ == "__main__":
    unittest.main()
