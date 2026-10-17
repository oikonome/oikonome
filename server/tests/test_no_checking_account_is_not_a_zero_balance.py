"""A household with no checking account has an UNKNOWN cash position, not
a $0 one.

Card-only and savings-only households have nothing the cash runway can
start from. Read as $0, every card balance and bill turned into "Short
$X today — checking doesn't cover cards & bills" (or "Cash is tight —
$0 left") on the Today page, the app and the morning email from the first
day. The runway now carries checking None and `no_checking`, headroom and
the forecast go quiet, and every surface asks for the account instead.
A real checking account at $0 is still believed and still alarms.
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import forecast
from oikonome.web import report, todayview

from .util import _ensure_db, add_txn, make_db, seed_accounts, write_config

TODAY = dt.date.today()


def _card_only(conn, card=1250.0):
    conn.execute("DELETE FROM accounts WHERE id='chk'")
    conn.execute("UPDATE accounts SET balance_current=%s WHERE id='card'",
                 (card,))


class NoCheckingAccountTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _assert_quiet(self, st):
        rw = st["runway"]
        self.assertIsNone(rw["checking"])
        self.assertTrue(rw["no_checking"])
        self.assertFalse(rw["balance_unreliable"])
        ctx = todayview.build_context(st)
        self.assertIsNone(ctx["day"]["headroom"])
        self.assertIsNone(ctx["cash"])
        self.assertIsNone(ctx["fc"])
        self.assertEqual(ctx["fc_rows"], [])
        subject, plain, html = report.build(st)
        for text in (plain, html):
            self.assertNotIn("Short", text)
            self.assertNotIn("SHORT", text.upper().replace("SHORTFALL", ""))
            self.assertNotIn("Cash is tight", text)
            self.assertNotIn("Cash gets tight", text)
            self.assertIn("No checking account linked", text)
        self.assertIn("link your checking account", plain)

    def test_a_card_only_household_gets_a_nudge_not_a_shortfall(self):
        _card_only(self.conn)
        add_txn(self.conn, TODAY, 40.0, "KROGER")
        self._assert_quiet(report.gather(self.conn, TODAY))

    def test_a_savings_only_household_gets_a_nudge_not_tight_cash(self):
        self.conn.execute("UPDATE accounts SET subtype='savings' "
                          "WHERE id='chk'")
        self.conn.execute("UPDATE accounts SET balance_current=0 "
                          "WHERE id='card'")
        add_txn(self.conn, TODAY, 40.0, "KROGER")
        self._assert_quiet(report.gather(self.conn, TODAY))

    def test_the_forecast_says_it_has_no_checking_to_start_from(self):
        _card_only(self.conn)
        self.assertIsNone(forecast._checking_balance(self.conn, {}))
        fc = forecast.build(self.conn, today=TODAY)
        self.assertTrue(fc["no_checking"])
        # the walk still runs (the calendar reads its events)
        self.assertIn("events", fc)

    def test_a_real_checking_account_at_zero_still_alarms(self):
        # the other side: an aggregator-fed checking reporting $0 is data,
        # and a $1,250 card against it is a real shortfall
        self.conn.execute("UPDATE accounts SET balance_current=0, "
                          "balance_available=0 WHERE id='chk'")
        self.conn.execute("UPDATE accounts SET balance_current=1250 "
                          "WHERE id='card'")
        add_txn(self.conn, TODAY, 40.0, "KROGER")
        st = report.gather(self.conn, TODAY)
        self.assertFalse(st["runway"]["no_checking"])
        self.assertFalse(forecast.build(self.conn, today=TODAY)["no_checking"])
        ctx = todayview.build_context(st)
        self.assertIsNotNone(ctx["day"]["headroom"])
        self.assertEqual((ctx["cash"] or {}).get("state"), "critical")
        _s, plain, _h = report.build(st)
        self.assertNotIn("No checking account linked", plain)


class TodayApiNoCheckingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"nochk-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            _card_only(conn)
        finally:
            conn.close()

    def test_today_payload_flags_no_checking_and_drops_the_cash_figures(self):
        r = self.client.get("/api/today/full")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["runway"]["no_checking"])
        self.assertIsNone(body["runway"]["checking"])
        self.assertIsNone(body["headroom"])
        self.assertIsNone(body["forecast"])
        self.assertEqual(body["forecast_rows"], [])


if __name__ == "__main__":
    unittest.main()
