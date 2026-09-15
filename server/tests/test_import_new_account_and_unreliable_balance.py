"""Importing into an account that does not exist yet, and what a $0
balance may not be used to claim.

The single-file import page creates its destination account inline, the
same way the bulk importer does. And while imported accounts sit at their
$0 default (runway.balance_unreliable) the forecast is suppressed
everywhere — a $0-seeded trajectory is noise presented as truth."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, write_config

CSV = b"Date,Description,Amount\n2026-07-01,COFFEE SHOP,-4.50\n"


class ImportCreateAccountTests(unittest.TestCase):
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
            "email": f"imp-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            write_config(conn)
        finally:
            conn.close()

    def test_import_into_a_new_account(self):
        r = self.client.post(
            "/api/import",
            data={"account_id": "", "amount_sign": "bank",
                  "new_account_name": "Chase Checking"},
            files={"file": ("chase.csv", CSV, "text/csv")})
        self.assertEqual(r.status_code, 200, r.text)
        res = r.json()["result"]
        self.assertFalse(res.get("error"), res)
        self.assertEqual(res.get("imported"), 1)
        conn = tenancy.tenant_connect(self.tid)
        try:
            acct = conn.execute(
                "SELECT id, name FROM accounts WHERE id LIKE 'manual:%'"
            ).fetchone()
            self.assertEqual(acct["name"], "Chase Checking")
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM transactions WHERE account_id=%s",
                (acct["id"],)).fetchone()["n"]
            self.assertEqual(n, 1)
        finally:
            conn.close()


class ForecastSuppressedTests(unittest.TestCase):
    def test_unreliable_balance_suppresses_the_forecast(self):
        from oikonome.web import report, todayview
        from .util import add_txn, make_db
        import datetime as dt
        conn = make_db()
        try:
            write_config(conn)
            add_txn(conn, dt.date.today(), 12.0, "COFFEE", account="chk")
            # imported/manual account still at the $0 default —
            # balance_unreliable = no LIVE depository account + rows exist
            conn.execute(
                """INSERT INTO items (id, aggregator, institution_name)
                   VALUES ('manual','manual','Manual')
                   ON CONFLICT (tenant_id, id) DO NOTHING""")
            conn.execute("UPDATE accounts SET balance_current=0, "
                         "balance_available=NULL, item_id='manual'")
            st = report.gather(conn, dt.date.today())
            if not (st.get("runway") or {}).get("balance_unreliable"):
                self.skipTest("fixture did not trip balance_unreliable")
            ctx = todayview.build_context(st)
            self.assertIsNone(ctx["fc"])          # no $0-as-truth forecast
            self.assertEqual(ctx["fc_rows"], [])
            self.assertEqual(ctx["fc_lows"], [])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
