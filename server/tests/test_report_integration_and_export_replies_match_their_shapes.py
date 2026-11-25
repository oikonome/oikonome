"""Every report, integrations, webhook, help, export and manual-sync door
is called and its reply matches the shape it documents
(web/schemas/reports.py, integrations.py, exports.py).

The conformance hook (tests/api_conformance.py) validates each reply; this
module reaches every door itself, seeded so the lists the shapes describe
are not empty: a property layer and a linked loan, a crypto account with
fiat flows, a brokerage with holdings and a workplace plan, investment and
bank fees, paychecks, a reported-income year, a bill, a recorded snapshot,
an active alert, a webhook and a finished export. The integration reads
are made with a read-scope script token, the way a script makes them. The
webhook receiver and the bank pull are stood in for.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine.compat import jsonb

from . import api_conformance
from .util import _ensure_db, add_txn, seed_accounts, write_config

PW = "correct-horse-battery"
TODAY = dt.date.today()
HOOK_URL = "http://127.0.0.1:9/hook"
RANGES = ("cur", "1m", "3m", "6m", "1y", "3y", "5y", "all")


def _seed(conn) -> None:
    seed_accounts(conn)
    write_config(conn, manual_assets=[
        {"name": "House", "kind": "real_estate", "value": 300000,
         "as_of": "2025-01-01"},
        {"name": "Car", "kind": "vehicle", "value": 12000}],
        advisory_fees={"brokerage": 0.0025})
    conn.execute("UPDATE items SET logo=%s WHERE id='it1'",
                 ("iVBORw0KGgo=",))
    for sql, args in (
        ("INSERT INTO items (id, aggregator, institution_name, access_token)"
         " VALUES ('it-inv','test','Big Brokerage','t')", ()),
        ("INSERT INTO items (id, aggregator, institution_name, access_token)"
         " VALUES ('it-cb','coinbase','Coinbase','t')", ()),
        ("INSERT INTO items (id, aggregator, institution_name, access_token)"
         " VALUES ('it-plan','plan_csv','Workplace Plan','t')", ()),
        ("INSERT INTO accounts (id,item_id,name,type,subtype,balance_current,"
         "mask) VALUES ('ira','it-inv','Roth IRA','investment','roth',20000,"
         "'1111')", ()),
        ("INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
         " VALUES ('loan','it1','Home Loan','loan','mortgage',150000)", ()),
        ("INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
         " VALUES ('btc','it-cb','Bitcoin','investment','crypto',900)", ()),
        ("INSERT INTO accounts (id,item_id,name,type,subtype,balance_current)"
         " VALUES ('k401','it-plan','401k','investment','401k',40000)", ()),
        ("INSERT INTO holdings (account_id, symbol, quantity, price, value,"
         " raw) VALUES ('ira','VTI',100,200,20000,%s)",
         (jsonb({"cost_basis": 15000}),)),
        ("INSERT INTO crypto_holdings (account_id, currency, quantity,"
         " native_usd) VALUES ('btc','BTC',0.01,900)", ()),
        ("INSERT INTO networth_snapshot (date, total) VALUES (%s, 4750)",
         (TODAY - dt.timedelta(days=2),)),
        ("INSERT INTO income_annual (year, total_income, wages,"
         " investment_income, tax_paid, joint, ss_earnings)"
         " VALUES (%s, 90000, 85000, 300, 9000, 1, 85000)",
         (TODAY.year - 1,)),
        ("INSERT INTO alerts_log (kind, severity, message, first_seen,"
         " last_seen) VALUES ('stale', 'warn', 'A feed is quiet', %s, %s)",
         (TODAY, TODAY)),
        ("INSERT INTO bills (id, type, payee, amount, frequency,"
         " monthly_amount, due_on, active, is_completed, raw)"
         " VALUES ('rec:power', 'BILL', 'Power Co', -80, 'MONTHLY', -80,"
         " %s, 1, 0, %s::jsonb)",
         (TODAY + dt.timedelta(days=3), json.dumps({
             "source": "manual", "bill_type": "occurrence",
             "recurrence": {"frequency": "MONTHLY", "interval": 1}}))),
        ("INSERT INTO job_runs (job, ran_at, note) VALUES ('export-zip',"
         " now(), %s)", (json.dumps(["one row skipped"]),)),
    ):
        conn.execute(sql, args)
    for back in (5, 40, 75, 400):
        day = TODAY - dt.timedelta(days=back)
        add_txn(conn, day, -2500.0, "ACME PAYROLL", primary="INCOME",
                detailed="INCOME_WAGES", account="chk")
        add_txn(conn, day, 64.0, "CORNER GROCER", primary="FOOD_AND_DRINK",
                merchant="Corner Grocer", account="card")
        add_txn(conn, day, 110.0, "AMAZON MKTPLACE", primary="Amazon Home",
                merchant="Amazon", account="card")
    add_txn(conn, TODAY - dt.timedelta(days=6), 35.0, "OVERDRAFT FEE",
            primary="BANK_FEES", detailed="BANK_FEES_OVERDRAFT_FEES",
            account="chk")
    add_txn(conn, TODAY - dt.timedelta(days=9), 18.0, "INTEREST CHARGE",
            primary="BANK_FEES", detailed="BANK_FEES_INTEREST_CHARGE",
            account="card")
    add_txn(conn, TODAY - dt.timedelta(days=12), 25.0, "ADVISORY FEE",
            primary="BANK_FEES", account="ira")
    for i, (kind, usd) in enumerate((("buy", 600), ("fiat_deposit", 100))):
        conn.execute(
            "INSERT INTO transactions (id,account_id,date,amount,name,"
            "pending,removed,raw) VALUES (%s,'btc',%s,0,'Coinbase',0,0,%s)",
            (f"cb-{i}", TODAY - dt.timedelta(days=20 + i),
             jsonb({"type": kind, "native_amount": {"amount": usd}})))
    conn.execute(
        "INSERT INTO transactions (id,account_id,date,amount,name,pending,"
        "removed,raw) VALUES ('k-1','k401',%s,-1000,'Contribution',0,0,%s)",
        (TODAY - dt.timedelta(days=30),
         jsonb({"type": "Contribution", "amount": 1000})))


class ReportIntegrationExportShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.client = TestClient(appmod.app)
        cls.email = f"rpshape-{uuid.uuid4().hex[:8]}@example.dev"
        r = cls.client.post("/api/signup", data={"email": cls.email,
                                                 "password": PW})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            _seed(conn)
        finally:
            conn.close()

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _ok(self, method, path, client=None, **kw):
        before = sum(api_conformance.validated().values())
        r = (client or self.client).request(method, "/api" + path, **kw)
        self.assertEqual(r.status_code, 200, f"{method} {path}: {r.text}")
        self.assertGreater(sum(api_conformance.validated().values()), before,
                           f"{method} {path}: the reply was not validated")
        return r.json()

    def _elevate(self):
        r = self.client.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_reports(self):
        nw = self._ok("GET", "/reports/networth")
        self.assertTrue(nw["property_items"])
        self.assertTrue(nw["coinbase_pl"])
        self.assertTrue(nw["retirement_combined"]["groups"])
        self.assertNotIn("owner", nw)
        self.assertEqual(self._ok("GET", "/reports/networth?owner=me")
                         ["owner"], "me")
        sp = self._ok("GET", "/reports/spending")
        self.assertTrue(sp["top_merchants"])
        self.assertTrue(sp["yoy_matrix"])
        cf = self._ok("GET", "/reports/cashflow")
        self.assertTrue(cf["savings_by_year"])
        self.assertTrue(cf["reported_income"])
        fees = self._ok("GET", "/reports/fees")
        self.assertTrue(fees["by_platform"])
        costs = self._ok("GET", "/reports/cash-costs")
        self.assertTrue(costs["by_year"])
        self.assertIsNotNone(costs["latest"])
        for rng in RANGES:
            with self.subTest(range=rng):
                self._ok("GET", f"/reports/cashflow/flow?range={rng}")
                self._ok("GET", f"/reports/spending/window?range={rng}")
                self._ok("GET", f"/reports/income/window?range={rng}")

    def test_the_integration_reads_with_a_read_token(self):
        self._elevate()
        token = self._ok("POST", "/tokens", json={"name": "sensor",
                                                  "scope": "read"})["token"]
        script = TestClient(self.app,
                            headers={"Authorization": f"Bearer {token}"})
        self.assertTrue(self._ok("GET", "/integrations/events",
                                 client=script)["events"])
        summary = self._ok("GET", "/integrations/summary", client=script)
        self.assertTrue(summary["accounts"])
        self.assertTrue(summary["connections"])
        self.assertTrue(self._ok("GET", "/integrations/accounts",
                                 client=script)["accounts"])
        txns = self._ok("GET", "/integrations/transactions?limit=5",
                        client=script)
        self.assertTrue(txns["transactions"])
        self._ok("GET", "/integrations/transactions?q=grocer", client=script)
        self.assertTrue(self._ok("GET", "/integrations/spending?months=3",
                                 client=script)["months"])
        bills = self._ok("GET", "/integrations/bills?days=30", client=script)
        self.assertTrue(bills["bills"])
        self.assertTrue(self._ok("GET", "/integrations/alerts",
                                 client=script)["alerts"])
        self.assertTrue(self._ok("GET", "/integrations/networth?months=2",
                                 client=script)["series"])
        # the read token reaches nothing but the integration doors
        self.assertEqual(script.get("/api/reports/networth").status_code,
                         403)

    def test_the_webhook_doors(self):
        from oikonome.notify import webhooks
        self._elevate()
        made = self._ok("POST", "/webhooks", json={
            "url": HOOK_URL, "events": ["test.ping"], "name": "hub"})
        hid = made["id"]
        self.assertTrue(self._ok("GET", "/webhooks")["webhooks"])
        self._ok("POST", f"/webhooks/{hid}", json={"name": "renamed",
                                                   "enabled": False})
        self._ok("POST", f"/webhooks/{hid}", json={"enabled": True})
        self._ok("POST", f"/webhooks/{hid}/rotate", json={})
        with mock.patch.object(webhooks, "_send",
                               return_value=(200, None)):
            tested = self._ok("POST", f"/webhooks/{hid}/test")
        self.assertTrue(tested["ok"])
        self.assertTrue(self._ok("GET", f"/webhooks/{hid}/deliveries")
                        ["deliveries"])
        self._ok("POST", f"/webhooks/{hid}/delete")

    def test_help_export_and_the_manual_sync(self):
        self.assertTrue(self._ok("GET", "/help")["topics"])
        last = self._ok("GET", "/export/last")
        self.assertEqual(last["warnings"], ["one row skipped"])
        self._elevate()
        ticket = self._ok("POST", "/export/token", json={"kind": "zip"})
        r = self.client.get("/api/export/precheck",
                            params={"kind": "zip", "t": ticket["token"]})
        self.assertEqual(r.status_code, 204, r.text)
        with mock.patch("oikonome.web.pages._account_sync",
                        return_value=("ok", "Pulled 2 new rows")):
            self._ok("POST", "/accounts/sync", json={"item_id": "it1"})

    def test_the_logo_doors_send_images(self):
        r = self.client.get("/api/institutions/it1/logo")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["content-type"], "image/png")


if __name__ == "__main__":
    unittest.main()
