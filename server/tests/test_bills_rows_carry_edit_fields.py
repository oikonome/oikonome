"""Every row of /api/bills carries the per-bill edit fields /api/bills/bill
returns, so a page of N bills is one request rather than N+1. The per-bill
route stays; the two must agree on the same bill, field for field.
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


def _make_client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


# the fields the per-bill route returns that a list row must carry too
# (cadence is the label on the row and the select value on the detail, so
# the detail's value travels as cadence_value)
FOLDED = {"bill_type": "bill_type", "source": "source",
          "category": "category", "merchant": "merchant", "cap": "cap",
          "disabled": "disabled", "show_today": "show_today",
          "match_category": "match_category", "txn_category": "txn_category",
          "cadence": "cadence_value"}


class BillsRowsCarryEditFieldsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.client.post("/api/signup", data={
            "email": f"bre-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def test_list_row_matches_the_per_bill_detail(self):
        self.client.post("/api/bills/save", json={
            "payee": "Streamflix", "amount": "15.99", "cadence": "MONTHLY:1",
            "next_due": "2026-08-01", "cap": 2, "show_today": True,
            "category": "Entertainment"})
        self.client.post("/api/bills/save", json={
            "payee": "Groceries", "amount": "600", "cadence": "ENVELOPE:1",
            "match_category": True, "category": "FOOD_AND_DRINK"})
        self.client.post("/api/bills/toggle", json={"payee": "Groceries"})
        page = self.client.get("/api/bills").json()
        self.assertTrue(page["cadences"])        # the select options, once
        by_payee = {r["payee"]: r for r in page["bills"]}
        for payee in ("Streamflix", "Groceries"):
            detail = self.client.get(
                f"/api/bills/bill?payee={payee}").json()
            row = by_payee[payee]
            for dfield, rfield in FOLDED.items():
                self.assertIn(rfield, row, (payee, rfield))
                self.assertEqual(row[rfield], detail[dfield],
                                 (payee, rfield))
        self.assertEqual(by_payee["Streamflix"]["cadence_value"], "MONTHLY:1")
        self.assertEqual(by_payee["Streamflix"]["cap"], 2)
        self.assertTrue(by_payee["Streamflix"]["show_today"])
        self.assertEqual(by_payee["Groceries"]["bill_type"], "envelope")
        self.assertEqual(by_payee["Groceries"]["cadence_value"], "ENVELOPE:1")
        self.assertTrue(by_payee["Groceries"]["match_category"])
        self.assertTrue(by_payee["Groceries"]["disabled"])


if __name__ == "__main__":
    unittest.main()
