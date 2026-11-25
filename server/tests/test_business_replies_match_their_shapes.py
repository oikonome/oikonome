"""The business doors the rest of the suite leaves unread, and the parts of
their replies it leaves empty, match the shapes they document
(web/schemas/business.py).

The conformance hook (tests/api_conformance.py) validates each reply; this
module reaches them with an entity that fills them: a two-member LLC (one
member this household, half each) whose card carries business charges, a
flagged personal charge to bring in, a logged trip, a filing, a vendor, and
the line suggestions, balance sheet and estimated tax over all of it.
"""

from __future__ import annotations

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from . import api_conformance
from .util import _ensure_db, add_txn, seed_accounts, write_config

TODAY = dt.date.today()


class BusinessShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"biz-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            for i, (amt, name) in enumerate(((-1800.0, "CLIENT PAYMENT"),
                                             (64.0, "OFFICE SUPPLY CO"),
                                             (120.0, "HOSTING CO"))):
                add_txn(conn, TODAY - dt.timedelta(days=5 + i), amt, name,
                        account="card", merchant=name.title(),
                        primary="GENERAL_SERVICES")
            cls.personal = add_txn(conn, TODAY - dt.timedelta(days=3), 42.0,
                                   "PRINT SHOP", account="chk",
                                   merchant="Print Shop")
        finally:
            conn.close()
        c = cls.client
        ent = c.post("/api/business/entities", json={
            "name": "Fern Studio LLC", "structure": "multi_member_llc",
            "state": "ID", "business_start_date":
                (TODAY - dt.timedelta(days=400)).isoformat()}).json()
        cls.eid = ent["id"]
        c.post(f"/api/business/entities/{cls.eid}/members", json={
            "member_name": "Robin", "ownership_pct": 50,
            "is_household": True})
        c.post(f"/api/business/entities/{cls.eid}/members", json={
            "member_name": "Sam", "ownership_pct": 50})
        r = c.post("/api/accounts/card/entity", json={"entity_id": cls.eid})
        assert r.status_code == 200, r.text
        r = c.post(f"/api/business/{cls.personal}/flag", json={})
        assert r.status_code == 200, r.text

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _ok(self, method, path, **kw):
        before = sum(api_conformance.validated().values())
        r = self.client.request(method, "/api" + path, **kw)
        self.assertEqual(r.status_code, 200, f"{method} {path}: {r.text}")
        self.assertGreater(sum(api_conformance.validated().values()), before,
                           f"{method} {path}: the reply was not validated")
        return r.json()

    def test_books_suggestions_balance_sheet_and_tax(self):
        e = f"/business/entities/{self.eid}"
        sug = self._ok("GET", f"{e}/suggestions")
        self.assertTrue(sug["lines"])
        self._ok("GET", f"{e}/balance-sheet")
        tax = self._ok("GET", f"{e}/estimated-tax?year={TODAY.year}")
        self.assertEqual(len(tax["members"]), 2, tax)
        self.assertIsNotNone(tax["household"], tax)
        self._ok("GET", f"{e}/delete-impact")

    def test_flags_brought_in_trips_filings_and_vendors(self):
        e = f"/business/entities/{self.eid}"
        moved = self._ok("POST", f"{e}/import-flags", json={})
        self.assertGreaterEqual(moved["moved"], 0)
        trip = self._ok("POST", f"{e}/mileage", json={
            "date": TODAY.isoformat(), "miles": 42.5, "purpose": "site visit"})
        listed = self._ok("GET", f"{e}/mileage?year={TODAY.year}")
        self.assertTrue(listed["trips"])
        self.assertTrue(listed["summary"]["rates"])
        self._ok("DELETE", f"{e}/mileage/{trip['id']}")
        filing = self._ok("POST", f"{e}/compliance", json={
            "title": "Annual report", "due_date":
                (TODAY + dt.timedelta(days=60)).isoformat(),
            "fee": "25", "recurrence": "yearly"})
        self._ok("GET", f"{e}/compliance")
        self._ok("DELETE", f"{e}/compliance/{filing['id']}")
        self._ok("POST", f"{e}/vendors", json={"merchant": "Hosting Co",
                                               "reportable": True})
        vendors = self._ok("GET", f"{e}/vendors?year={TODAY.year}")
        self.assertTrue(vendors["vendors"], vendors)


if __name__ == "__main__":
    unittest.main()
