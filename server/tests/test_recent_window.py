"""The Today page's "recent transactions" window is yesterday + today.

A wider window buries what you actually just spent. The window feeds the
Today list, the email subject's "$X since yesterday", and the recent-sum
header, so it is pinned here — a silent widening is a product regression,
not a detail.
"""

import datetime as dt
import unittest

from oikonome.web import report

from .util import TODAY, add_txn, make_db, write_config


class RecentWindowTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _names(self, d):
        return {r["name"] for r in d["yesterday_rows"]}

    def test_window_is_yesterday_and_today_only(self):
        # anchor mid-month so the 1st-of-month clamp is not what's under test
        today = TODAY.replace(day=15)
        for days, name in ((0, "TODAY_TXN"), (1, "YESTERDAY_TXN"),
                           (2, "TWO_DAYS_AGO"), (3, "THREE_DAYS_AGO")):
            add_txn(self.conn, today - dt.timedelta(days=days), 10.0, name)
        d = report.gather(self.conn, today)
        self.assertEqual(self._names(d), {"TODAY_TXN", "YESTERDAY_TXN"})
        # the window start IS yesterday — the email subject reads off this
        self.assertEqual(d["yesterday"], today - dt.timedelta(days=1))

    def test_subject_says_since_yesterday(self):
        today = TODAY.replace(day=15)
        add_txn(self.conn, today, 25.0, "SAFEWAY", primary="FOOD_AND_DRINK")
        add_txn(self.conn, today - dt.timedelta(days=1), 15.0, "TARGET")
        add_txn(self.conn, today - dt.timedelta(days=3), 99.0, "OLD_ONE")
        subject, _, _ = report.build(report.gather(self.conn, today))
        self.assertIn("since yesterday", subject)
        # $40 = today + yesterday; the 3-day-old $99 must not be counted
        self.assertIn("$40", subject)

    def test_first_of_month_clamps_to_today(self):
        # month_status rows only cover the current month, so on the 1st the
        # window is today alone and the subject must not claim "yesterday"
        first = TODAY.replace(day=1)
        add_txn(self.conn, first, 12.0, "FIRST_TXN")
        d = report.gather(self.conn, first)
        self.assertEqual(d["yesterday"], first)
        self.assertEqual(self._names(d), {"FIRST_TXN"})
        subject, _, _ = report.build(d)
        self.assertIn("today", subject)
        self.assertNotIn("since yesterday", subject)


if __name__ == "__main__":
    unittest.main()


class RecentPaneShapeTests(unittest.TestCase):
    """The Today payload's recent rows carry the FULL Transactions-page
    row shape — the SPA renders the shared TxnTable, so
    every pill/flag/affordance must be present, not a slimmed copy."""

    def test_recent_rows_are_ledger_shaped(self):
        import os
        import uuid
        from fastapi.testclient import TestClient
        from oikonome.db import tenancy
        from .util import _ensure_db, add_txn, seed_accounts, write_config
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        client = TestClient(app)
        client.post("/api/signup", data={
            "email": f"rp-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            add_txn(conn, dt.date.today(), 12.5, "COFFEE SHOP", account="chk")
        finally:
            conn.close()
        rows = client.get("/api/today/full").json()["recent"]
        self.assertTrue(rows)
        row = rows[0]
        for key in ("id", "date", "amount", "payee", "category", "account",
                    "pending", "reimb", "reimb_flag", "biz_flag",
                    "has_receipt", "recurring_bill"):
            self.assertIn(key, row, key)
        self.assertEqual(row["payee"], "COFFEE SHOP")
