"""The transactions list stamps each row with `recurring_bill` — the
active bill whose matcher counts it — so the ledger shows what is
already recurring without a detour to the Bills page. Same
semantics as the history page's ✓: merchant match AND amount inside the
bill's tolerance; envelopes take any spend row on their merchant."""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config


class TxnRecurringFlagTests(unittest.TestCase):
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
            "email": f"txnbill-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            today = dt.date.today()
            cls.d = today.replace(day=10)
            add_txn(conn, cls.d, 89.99, "CITY POWER & LIGHT", account="chk",
                    merchant="City Power")
            add_txn(conn, cls.d, 300.0, "CITY POWER & LIGHT", account="chk",
                    merchant="City Power")     # off-amount: outside tolerance
            add_txn(conn, cls.d, 55.0, "SAFEWAY 123", account="chk",
                    merchant="Safeway")        # envelope spend
            add_txn(conn, cls.d, 42.0, "RANDOM SHOP", account="chk",
                    merchant="Random Shop")    # no bill at all
        finally:
            conn.close()
        cls.client.post("/api/bills/save", json={
            "payee": "City Power", "amount": 90, "cadence": "MONTHLY:1",
            "next_due": (dt.date.today() + dt.timedelta(days=20)).isoformat()})
        cls.client.post("/api/bills/save", json={
            "payee": "Safeway", "amount": 400, "cadence": "ENVELOPE:1"})

    def _rows(self):
        r = self.client.get("/api/transactions",
                            params={"y": self.d.year, "m": self.d.month})
        self.assertEqual(r.status_code, 200)
        return {(row["payee"], row["amount"]): row["recurring_bill"]
                for row in r.json()["rows"]}

    def test_matched_row_names_its_bill(self):
        rows = self._rows()
        self.assertEqual(rows[("City Power", 89.99)], "City Power")

    def test_off_amount_row_is_not_claimed(self):
        # $300 against a $90 bill (tol = max(30, 22.5) = 30) — the matcher
        # would NOT count it, so no indicator
        rows = self._rows()
        self.assertIsNone(rows[("City Power", 300.0)])

    def test_envelope_takes_any_spend_on_its_merchant(self):
        rows = self._rows()
        self.assertEqual(rows[("Safeway", 55.0)], "Safeway")

    def test_unmatched_merchant_is_none(self):
        rows = self._rows()
        self.assertIsNone(rows[("Random Shop", 42.0)])

    def test_search_mode_stamps_too(self):
        r = self.client.get("/api/transactions", params={"q": "city power"})
        rows = {row["amount"]: row["recurring_bill"]
                for row in r.json()["rows"]}
        self.assertEqual(rows[89.99], "City Power")
        self.assertIsNone(rows[300.0])


if __name__ == "__main__":
    unittest.main()


class ReimbFilterTests(unittest.TestCase):
    """Transactions reimb quick filter: only linked
    pairs (either side) and awaiting flags survive reimb=1."""

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
            "email": f"rf-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cls.d = dt.date.today().replace(day=10)
            cls.exp = add_txn(conn, cls.d, 120.0, "CLINIC VISIT",
                              account="chk")
            cls.dep = add_txn(conn, cls.d, -120.0, "INSURANCE PAYOUT",
                              account="chk")
            cls.flagged = add_txn(conn, cls.d, 60.0, "PENDING CLAIM",
                                  account="chk")
            cls.plain = add_txn(conn, cls.d, 40.0, "GROCERIES",
                                account="chk")
            from oikonome.web import data
            data.link_reimbursement(conn, cls.exp, cls.dep)
            data.flag_reimbursement(conn, cls.flagged)
        finally:
            conn.close()

    def test_month_mode_filters_to_reimb_rows(self):
        r = self.client.get("/api/transactions",
                            params={"y": self.d.year, "m": self.d.month,
                                    "reimb": 1})
        ids = {row["id"] for row in r.json()["rows"]}
        self.assertEqual(ids, {self.exp, self.dep, self.flagged})

    def test_search_mode_filters_too(self):
        r = self.client.get("/api/transactions",
                            params={"q": "clinic", "reimb": 1})
        ids = {row["id"] for row in r.json()["rows"]}
        self.assertEqual(ids, {self.exp})
        # without the filter the plain row is present in month mode
        r = self.client.get("/api/transactions",
                            params={"y": self.d.year, "m": self.d.month})
        ids = {row["id"] for row in r.json()["rows"]}
        self.assertIn(self.plain, ids)
