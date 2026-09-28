"""JSON API surface: auth-gated, tenant-scoped, transactions/categories/
reimbursements round-trips. In-process TestClient against the test DB."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"    # non-Secure cookie for http test client
        _ensure_db()                       # redirect DSNs BEFORE app import
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        email = f"api-{uuid.uuid4().hex[:8]}@example.dev"
        r = cls.client.post("/api/signup",
                            data={"email": email,
                                  "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cls.t1 = add_txn(conn, TODAY, 42.5, "SAFEWAY",
                             primary="FOOD_AND_DRINK")
            cls.dep = add_txn(conn, TODAY, -42.5, "EMPLOYER REIMB",
                              account="chk", primary="INCOME")
        finally:
            conn.close()

    def test_linked_sources_card_dismissal_persists(self):
        """Putting the linked-sources card away is a household setting,
        not a browser one: it survives a reload and reaches every device,
        and showing it again is the same door with `card: false`. A
        suggestion key is still refused when malformed."""
        r = self.client.get("/api/accounts/links")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["card_dismissed"])
        r = self.client.post("/api/accounts/links/dismiss",
                             json={"card": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(
            self.client.get("/api/accounts/links").json()["card_dismissed"])
        r = self.client.post("/api/accounts/links/dismiss",
                             json={"card": False})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(
            self.client.get("/api/accounts/links").json()["card_dismissed"])
        r = self.client.post("/api/accounts/links/dismiss",
                             json={"key": "no-pipe"})
        self.assertEqual(r.status_code, 422)

    def test_requires_auth(self):
        from oikonome.web.app import app
        anon = TestClient(app)
        self.assertEqual(anon.get("/api/transactions").status_code, 401)

    def test_non_uuid_path_id_is_404_not_500(self):
        """A path id Postgres cannot cast to uuid (sqlstate 22P02) names no
        record — the caller's 404, never our 500. Only a handful of routes
        coerce ids by hand; the app-level handler covers the class, so any
        uuid-taking route fed '/not-a-uuid' answers like a miss."""
        r = self.client.delete("/api/accounts/links/not-a-uuid")
        self.assertEqual(r.status_code, 404, r.text)
        r = self.client.post("/api/accounts/links/not-a-uuid/order",
                             json={"account_ids": ["a", "b"]})
        self.assertEqual(r.status_code, 404, r.text)

    def test_link_ids_with_non_string_elements_do_not_500(self):
        """A nested dict/list in account_ids must not reach set()/SQL raw
        and crash; coercion turns it into an ordinary refusal."""
        r = self.client.post("/api/accounts/links",
                             json={"account_ids": [{"x": 1}, "acct2"]})
        self.assertIn(r.status_code, (400, 404), r.text)
        # a non-list scalar must refuse too, not TypeError into a 500
        for bad in (5, True, "abc"):
            r = self.client.post("/api/accounts/links",
                                 json={"account_ids": bad})
            self.assertEqual(r.status_code, 400, r.text)
            r = self.client.post(
                f"/api/accounts/links/{uuid.uuid4()}/order",
                json={"account_ids": bad})
            self.assertEqual(r.status_code, 400, r.text)

    def test_me_donate_url(self):
        """self-host shows the project donate link; paying hosted
        tenants hide it; env override force-shows or hides ('off')."""
        clean = {k: v for k, v in os.environ.items()
                 if k not in ("OIKONOME_HOSTED", "OIKONOME_DONATE_URL")}
        # self-host default (no HOSTED, no override) → project donate page
        with mock.patch.dict(os.environ, clean, clear=True):
            os.environ["OIKONOME_DEV"] = "1"
            self.assertEqual(self.client.get("/api/me").json()["donate_url"],
                             "https://oikonome.com/donate")
        # hosted (paying) hides it by default
        with mock.patch.dict(os.environ, clean, clear=True):
            os.environ["OIKONOME_DEV"] = "1"
            os.environ["OIKONOME_HOSTED"] = "1"
            self.assertIsNone(self.client.get("/api/me").json()["donate_url"])
        # explicit URL force-shows even on hosted
        with mock.patch.dict(os.environ, clean, clear=True):
            os.environ["OIKONOME_DEV"] = "1"
            os.environ["OIKONOME_HOSTED"] = "1"
            os.environ["OIKONOME_DONATE_URL"] = "https://example.test/give"
            self.assertEqual(self.client.get("/api/me").json()["donate_url"],
                             "https://example.test/give")
        # "off" hides it even on self-host
        with mock.patch.dict(os.environ, clean, clear=True):
            os.environ["OIKONOME_DEV"] = "1"
            os.environ["OIKONOME_DONATE_URL"] = "off"
            self.assertIsNone(self.client.get("/api/me").json()["donate_url"])
        # empty string (compose's unset default) → falls through to default
        with mock.patch.dict(os.environ, clean, clear=True):
            os.environ["OIKONOME_DEV"] = "1"
            os.environ["OIKONOME_DONATE_URL"] = ""
            self.assertEqual(self.client.get("/api/me").json()["donate_url"],
                             "https://oikonome.com/donate")

    def test_month_browse_and_search(self):
        r = self.client.get("/api/transactions",
                            params={"y": TODAY.year, "m": TODAY.month})
        body = r.json()
        self.assertEqual(body["mode"], "month")
        payees = {t["payee"] for t in body["rows"]}
        self.assertIn("SAFEWAY", payees)
        r = self.client.get("/api/transactions", params={"q": "safeway"})
        body = r.json()
        self.assertEqual(body["mode"], "search")
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["amount_sum"], 42.5)

    def test_month_reports_money_in_out_net(self):
        """The month view must report its own totals: without them the
        ledger's DEFAULT state answers nothing about the month on screen.
        The fixture holds one charge (42.50 spend) and one income deposit
        (42.50); the headline is in / out / net."""
        body = self.client.get("/api/transactions", params={
            "y": TODAY.year, "m": TODAY.month}).json()
        self.assertAlmostEqual(body["out_sum"], 42.5, places=2)
        self.assertAlmostEqual(body["in_sum"], 42.5, places=2)
        self.assertAlmostEqual(body["net_sum"], 0.0, places=2)

    def test_month_header_excludes_money_merely_moved(self):
        """The header is the month's CASH FLOW, not a sum of the listed
        rows. A raw row sum counts a credit-card payment as money out on
        top of the card swipes it paid for, and counts a transfer between
        own accounts as out (or in) — so a month that actually paid for
        itself read as overspent. Transfers, card payments and own-money
        inflows must move neither figure; the rows themselves stay listed."""
        conn = tenancy.tenant_connect(self.tid)
        extra = []
        try:
            extra.append(add_txn(conn, TODAY, 300, "TO SAVINGS",
                                 account="chk", primary="TRANSFER_OUT"))
            extra.append(add_txn(conn, TODAY, 500, "CARD AUTOPAY",
                                 account="chk", primary="LOAN_PAYMENTS",
                                 detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"))
            extra.append(add_txn(conn, TODAY, -200, "FROM BROKERAGE",
                                 account="chk", primary="TRANSFER_IN"))
            # a stock sell landing in checking: Plaid stamps it INCOME, the
            # budget engine calls it shortfall funding — never income
            # a descriptor the income definition excludes by name
            # (engine/reporting._INCOME_WHERE matches the provider token)
            extra.append(add_txn(conn, TODAY, -900, "BETTERMENT INVEST XFER",
                                 account="chk", primary="INCOME"))
            body = self.client.get("/api/transactions", params={
                "y": TODAY.year, "m": TODAY.month}).json()
            self.assertAlmostEqual(body["out_sum"], 42.5, places=2)
            self.assertAlmostEqual(body["in_sum"], 42.5, places=2)
            self.assertAlmostEqual(body["net_sum"], 0.0, places=2)
            listed = {r["id"] for r in body["rows"]}
            for tid in extra:
                self.assertIn(tid, listed)
        finally:
            conn.execute("DELETE FROM transactions WHERE id = ANY(%s)",
                         (extra,))
            conn.close()

    def test_recategorize_and_clear(self):
        r = self.client.post(f"/api/transactions/{self.t1}/category",
                             json={"category": "ENTERTAINMENT"})
        self.assertTrue(r.json()["ok"])
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                "SELECT category_override FROM transactions WHERE id=%s",
                (self.t1,)).fetchone()
            self.assertEqual(row["category_override"], "ENTERTAINMENT")
        finally:
            conn.close()
        self.client.post(f"/api/transactions/{self.t1}/category",
                         json={"category": ""})
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                "SELECT category_override FROM transactions WHERE id=%s",
                (self.t1,)).fetchone()
            self.assertIsNone(row["category_override"])
        finally:
            conn.close()

    def test_reimburse_flow(self):
        # flag → pending list → candidates → link → pending empties
        self.client.post(f"/api/reimburse/{self.t1}/flag")
        pend = self.client.get("/api/reimburse/pending").json()["pending"]
        self.assertIn(self.t1, [p["id"] for p in pend])
        cands = self.client.get(
            f"/api/reimburse/{self.t1}/candidates").json()["candidates"]
        self.assertIn(self.dep, [c["id"] for c in cands])
        r = self.client.post("/api/reimburse/link",
                             json={"txn_id": self.dep,
                                   "other_ids": [self.t1]})
        self.assertEqual(r.json()["linked"], 1)
        pend = self.client.get("/api/reimburse/pending").json()["pending"]
        self.assertNotIn(self.t1, [p["id"] for p in pend])
        # unlink restores
        self.client.post("/api/reimburse/unlink",
                         json={"expense_id": self.t1,
                               "reimburse_id": self.dep})
        cands = self.client.get(
            f"/api/reimburse/{self.t1}/candidates").json()
        self.assertEqual(cands["pairs"], [])

    def test_tenant_isolation_via_api(self):
        """A second signup sees NONE of the first tenant's rows."""
        from oikonome.web.app import app
        other = TestClient(app)
        other.post("/api/signup",
                   data={"email": f"other-{uuid.uuid4().hex[:8]}@example.dev",
                         "password": "correct-horse-battery"})
        r = other.get("/api/transactions", params={"q": "safeway"})
        self.assertEqual(r.json()["total"], 0)


if __name__ == "__main__":
    unittest.main()

