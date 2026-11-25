"""Every ledger door — the transactions page and its edits, owners,
categories, merchants and rules — is called and its reply matches the
shape it documents (web/schemas/ledger.py).

The conformance hook (tests/api_conformance.py) validates each reply; this
module reaches each door itself, with rows that fill the shapes: a month
and a search of the ledger, a split, an owner, a pending charge to retire,
a merchant renamed and the rename undone, a rule written, disabled and
deleted, and two pairs of near-twin merchants the merge finder offers to
join — one offer approved, one rejected.
"""

from __future__ import annotations

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from oikonome.engine import merchant_merge

from . import api_conformance
from .util import _ensure_db, add_txn, seed_accounts, write_config

TODAY = dt.date.today()


class LedgerShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"ledger-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=600, other_monthly=400)
            cls.coffee = add_txn(conn, TODAY, 18.5, "COPPER KETTLE CAFE",
                                 merchant="Copper Kettle",
                                 primary="FOOD_AND_DRINK")
            cls.shop = add_txn(conn, TODAY, 120.0, "ELECTRONICS STORE",
                               merchant="Gadget Hut",
                               primary="GENERAL_MERCHANDISE")
            # pending long past the settling window, so it can be retired
            # two pairs of near-twins, each spelled two ways on the card
            for name in ("Blue Bottle Coffee", "Blue Bottle Coffe",
                         "Fern Street Books", "Fern Street Book"):
                mid = conn.execute(
                    "INSERT INTO merchants (name, name_source) VALUES "
                    "(%s, 'plaid') RETURNING id", (name,)).fetchone()["id"]
                for i in range(4):
                    t = add_txn(conn, TODAY - dt.timedelta(days=7 * i + 1),
                                12.0 + i, name.upper(),
                                primary="FOOD_AND_DRINK")
                    conn.execute("UPDATE transactions SET merchant_id = %s "
                                 "WHERE id = %s", (mid, t))
            merchant_merge.run(conn)
            cls.held = add_txn(conn, TODAY - dt.timedelta(days=20), 9.99,
                               "STREAMING CO",
                               merchant="Streamco", pending=1)
        finally:
            conn.close()

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

    def test_the_transactions_page(self):
        month = self._ok("GET", f"/transactions?y={TODAY.year}&m={TODAY.month}")
        self.assertEqual(month["mode"], "month")
        self.assertTrue(month["rows"])
        search = self._ok("GET", "/transactions?q=kettle")
        self.assertEqual(search["mode"], "search")
        self._ok("GET", f"/transactions?y={TODAY.year}&m={TODAY.month}"
                        "&bucket=food")

    def test_edits_to_one_row(self):
        tid = self.shop
        self._ok("POST", f"/transactions/{tid}/category",
                 json={"category": "Gifts"})
        self._ok("PUT", f"/transactions/{tid}/split", json={"parts": [
            {"category": "GENERAL_MERCHANDISE", "amount": 100.0},
            {"category": "Gifts", "amount": 20.0}]})
        self._ok("DELETE", f"/transactions/{tid}/split")
        self._ok("POST", f"/transactions/{tid}/note",
                 json={"note": "a birthday present"})
        self._ok("POST", f"/transactions/{tid}/owner", json={"owner": "Robin"})
        self._ok("POST", "/accounts/card/owner", json={"owner": "Robin"})
        self.assertIn("Robin", self._ok("GET", "/owners")["owners"])
        self._ok("POST", "/transactions/bulk",
                 json={"ids": [self.coffee], "action": "category",
                       "category": "FOOD_AND_DRINK"})
        self._ok("POST", "/transactions/retire-pending", json={"id": self.held})

    def test_categories_merchants_and_rules(self):
        self._ok("GET", "/categories")
        self._ok("GET", "/merchants")
        cat = self._ok("GET", "/merchants/catalog")
        self.assertTrue(cat["merchants"])
        self._ok("GET", "/merchants/detail?display=Gadget%20Hut")
        self._ok("POST", "/merchants/rename",
                 json={"display": "Gadget Hut", "to": "Gadget House"})
        recent = self._ok("GET", "/merchants/catalog")["recent"]
        self.assertTrue(recent)
        undone = self._ok("POST", "/merchants/undo", json={"id": recent[0]["id"]})
        self.assertIn("raw_merchant", undone)
        self._ok("POST", "/rules/set", json={"merchant": "Streamco",
                                             "category": "Entertainment"})
        self._ok("GET", "/rules?source=user")
        self._ok("POST", "/rules/disable", json={"merchant": "Streamco",
                                                 "disabled": True})
        self._ok("POST", "/rules/delete", json={"merchant": "Streamco"})
        self._ok("POST", "/categories/rename",
                 json={"old": "Entertainment", "new": "Fun"})

    def test_merge_offers_approved_and_rejected(self):
        merges = self._ok("GET", "/merchants/catalog")["merges"]
        self.assertEqual(len(merges), 2, merges)
        self.assertIsNotNone(merges[0]["from_facts"])
        approved = self._ok("POST", "/merchants/merge",
                            json={"pid": merges[0]["id"], "action": "approve"})
        self.assertEqual(approved["approved"], merges[0]["id"])
        rejected = self._ok("POST", "/merchants/merge",
                            json={"pid": merges[1]["id"], "action": "reject"})
        self.assertEqual(rejected["rejected"], merges[1]["id"])


if __name__ == "__main__":
    unittest.main()
