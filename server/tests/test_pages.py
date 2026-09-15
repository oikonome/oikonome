"""Wizard follow-on pages: account classification (type + primary
checking) and CSV/OFX upload with header auto-detection."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.web.pages import _auto_map

from .util import _ensure_db, seed_accounts, write_config
from .export_ticket import export_get

CHASE_CSV = (b"Transaction Date,Post Date,Description,Category,Type,Amount\n"
             b"07/10/2026,07/11/2026,SAFEWAY STORE,Groceries,Sale,-42.50\n"
             b"07/11/2026,07/12/2026,PAYROLL,Income,Payment,2000.00\n")


class PagesTests(unittest.TestCase):
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
            "email": f"pg-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_auto_map(self):
        m = _auto_map(["Transaction Date", "Post Date", "Description",
                       "Category", "Type", "Amount"])
        self.assertEqual(m["date"], "Transaction Date")
        self.assertEqual(m["name"], "Description")
        self.assertEqual(m["amount"], "Amount")
        self.assertIsNone(_auto_map(["Foo", "Bar"]))

    def test_classify_and_primary_checking(self):
        r = self.client.post("/accounts/classify",
                             data={"account_id": "chk",
                                   "kind": "depository/savings",
                                   "primary_checking": ""},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute("SELECT type, subtype FROM accounts "
                               "WHERE id='chk'").fetchone()
            self.assertEqual((row["type"], row["subtype"]),
                             ("depository", "savings"))
            # set back + mark primary
            self.client.post("/accounts/classify",
                             data={"account_id": "chk",
                                   "kind": "depository/checking",
                                   "primary_checking": "1"})
            cfg = budget.load_config(conn)
            self.assertEqual(cfg["checking_account_id"], "chk")
        finally:
            conn.close()

    def test_csv_upload_auto_detects_chase_headers(self):
        r = self.client.post("/import",
                             data={"account_id": "card",
                                   "amount_sign": "bank"},
                             files={"file": ("chase.csv", CHASE_CSV,
                                             "text/csv")})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Imported <b>2</b>", r.text)
        conn = tenancy.tenant_connect(self.tid)
        try:
            rows = conn.execute(
                "SELECT amount FROM transactions WHERE id LIKE 'csv:%' "
                "AND account_id='card' "
                "AND name IN ('SAFEWAY STORE','PAYROLL') "
                "ORDER BY amount").fetchall()
            self.assertEqual([r["amount"] for r in rows], [-2000.0, 42.5])
        finally:
            conn.close()

    def test_csv_unknown_headers_offers_mapping_then_imports(self):
        import re
        weird = (b"When,What,How Much\n"
                 b"2026-07-10,SHOP A,-12.00\n"
                 b"2026-07-11,SHOP B,-8.50\n")
        r = self.client.post("/import",
                             data={"account_id": "card",
                                   "amount_sign": "bank"},
                             files={"file": ("weird.csv", weird, "text/csv")})
        self.assertIn("Map the columns", r.text)
        token = re.search(r'name="token" value="([^"]+)"', r.text).group(1)
        r = self.client.post("/import/mapped",
                             data={"token": token, "date_col": "When",
                                   "amount_col": "How Much",
                                   "name_col": "What"})
        self.assertIn("Imported <b>2</b>", r.text)
        conn = tenancy.tenant_connect(self.tid)
        try:
            rows = conn.execute(
                "SELECT name, amount FROM transactions WHERE name LIKE 'SHOP %'"
                " ORDER BY name").fetchall()
            self.assertEqual([(r["name"], r["amount"]) for r in rows],
                             [("SHOP A", 12.0), ("SHOP B", 8.5)])
        finally:
            conn.close()
        # expired/reused token → friendly error
        r = self.client.post("/import/mapped",
                             data={"token": token, "date_col": "When",
                                   "amount_col": "How Much",
                                   "name_col": "What"})
        self.assertIn("expired", r.text)


    def test_manual_account_and_balance(self):
        r = self.client.post("/accounts/add",
                             data={"name": "Cash Stash",
                                   "kind": "depository/savings"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.client.post("/accounts/balance",
                         data={"account_id": "manual:cash-stash",
                               "balance": "1,234.56"})
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                "SELECT balance_current, type, subtype FROM accounts "
                "WHERE id='manual:cash-stash'").fetchone()
            self.assertEqual(row["balance_current"], 1234.56)
            self.assertEqual(row["subtype"], "savings")
            # balance endpoint refuses non-manual accounts
            self.client.post("/accounts/balance",
                             data={"account_id": "chk", "balance": "1"})
            chk = conn.execute("SELECT balance_current FROM accounts "
                               "WHERE id='chk'").fetchone()
            self.assertEqual(chk["balance_current"], 5000.0)
        finally:
            conn.close()

    def test_settings_round_trip(self):
        r = self.client.post("/settings",
                             data={"food_monthly": "900",
                                   "other_monthly": "1100",
                                   "budgeted_income_monthly": "6000",
                                   "dynamic_variable_budget": "1"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
            self.assertEqual(cfg["food_monthly"], 900.0)
            self.assertEqual(cfg["other_monthly"], 1100.0)
            self.assertEqual(cfg["budgeted_income_monthly"], 6000.0)
            self.assertTrue(cfg["dynamic_variable_budget"])
        finally:
            conn.close()
        self.client.post("/settings/email",
                         data={"email_recipients": "a@x.dev, b@x.dev"})
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
            self.assertEqual(cfg["email_recipients"], ["a@x.dev", "b@x.dev"])
        finally:
            conn.close()
        # restore fixture budgets for any later tests in this tenant
        conn = tenancy.tenant_connect(self.tid)
        try:
            write_config(conn)
        finally:
            conn.close()


    def test_transactions_page_browse_search_recategorize(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            from .util import add_txn, TODAY
            tid = add_txn(conn, TODAY, 33.33, "PAGE TEST SHOP")
        finally:
            conn.close()
        # Jinja page retired → 303 to the SPA, query preserved
        r = self.client.get("/transactions", params={"q": "page test"},
                            follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"],
                         "/app/transactions?q=page+test")
        # browse + search through the API the SPA uses
        r = self.client.get("/api/transactions",
                            params={"y": TODAY.year, "m": TODAY.month})
        self.assertIn("PAGE TEST SHOP", str(r.json()))
        r = self.client.get("/api/transactions", params={"q": "page test"})
        self.assertEqual(r.json()["total"], 1)
        # recategorize through the form endpoint
        r = self.client.post("/transactions/recategorize",
                             data={"txn_id": tid, "category": "TRAVEL",
                                   "back": "/transactions"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute("SELECT category_override FROM transactions "
                               "WHERE id=%s", (tid,)).fetchone()
            self.assertEqual(row["category_override"], "TRAVEL")
        finally:
            conn.close()


    def test_alerts_page_and_export(self):
        import io, zipfile
        from oikonome.engine import alerts
        from .util import TODAY
        conn = tenancy.tenant_connect(self.tid)
        try:
            alerts.log(conn, [{"kind": "anomaly", "severity": "warn",
                               "message": "export-test alert"}], TODAY)
        finally:
            conn.close()
        # Jinja page retired → SPA; the data comes from the API
        r = self.client.get("/alerts", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/app/alerts")
        r = self.client.get("/api/alerts/history")
        self.assertIn("export-test alert", str(r.json()))
        r = export_get(self.client, "/export")
        self.assertEqual(r.status_code, 200)
        z = zipfile.ZipFile(io.BytesIO(r.content))
        names = set(z.namelist())
        self.assertIn("transactions.csv", names)
        self.assertIn("bills.csv", names)
        txt = z.read("transactions.csv").decode()
        self.assertNotIn("tenant_id", txt.splitlines()[0] if txt else "")
        items = z.read("items.csv").decode()
        self.assertNotIn("access_token", items.splitlines()[0] if items else "")
        self.assertNotIn("tok-test", items)      # no tokens ever


if __name__ == "__main__":
    unittest.main()
