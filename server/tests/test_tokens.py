"""Script tokens: mint/list/revoke lifecycle, bearer auth on the
import doors, and the deliberate narrowness (a token can push data in,
never read the ledger or change configuration)."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config

CSV = b"Date,Description,Amount\n2026-07-01,KROGER,-42.50\n"


class ScriptTokenTests(unittest.TestCase):
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
            "email": f"tok-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _mint(self, name="test-script"):
        r = self.client.post("/api/tokens", json={"name": name, "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_mint_shows_plaintext_once_and_lists_hashless(self):
        minted = self._mint("nightly-collector")
        self.assertTrue(minted["token"].startswith("oik_"))
        listed = self.client.get("/api/tokens").json()["tokens"]
        me = [t for t in listed if t["id"] == minted["id"]]
        self.assertEqual(len(me), 1)
        self.assertNotIn("token", me[0])
        self.assertNotIn("token_hash", me[0])
        self.assertEqual(me[0]["name"], "nightly-collector")

    def test_bearer_pushes_through_the_import_hub(self):
        minted = self._mint("bank-export")
        bare = TestClient(self.client.app)          # no session cookie
        r = bare.post("/api/import",
                      headers={"Authorization": f"Bearer {minted['token']}"},
                      data={"account_id": "chk", "amount_sign": "bank"},
                      files={"file": ("bank-export.csv", CSV, "text/csv")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse((r.json().get("result") or {}).get("error"))

    def test_bearer_cannot_read_the_ledger(self):
        minted = self._mint()
        bare = TestClient(self.client.app)
        for path in ("/api/transactions", "/api/settings", "/api/tokens",
                     "/api/me", "/api/accounts"):
            r = bare.get(path, headers={
                "Authorization": f"Bearer {minted['token']}"})
            self.assertEqual(r.status_code, 403, f"{path} -> {r.status_code}")

    def test_revoked_token_stops_working(self):
        minted = self._mint("stale")
        self.assertTrue(self.client.post(
            "/api/tokens/revoke", json={"id": minted["id"], "password": "correct-horse-battery"}).json()["ok"])
        bare = TestClient(self.client.app)
        r = bare.post("/api/import",
                      headers={"Authorization": f"Bearer {minted['token']}"},
                      data={"account_id": "chk", "amount_sign": "bank"},
                      files={"file": ("x.csv", CSV, "text/csv")})
        self.assertEqual(r.status_code, 401)
        # revoking twice is a 404, not a crash
        self.assertEqual(self.client.post(
            "/api/tokens/revoke", json={"id": minted["id"], "password": "correct-horse-battery"}).status_code, 404)

    def test_garbage_token_is_401(self):
        bare = TestClient(self.client.app)
        r = bare.post("/api/import",
                      headers={"Authorization": "Bearer oik_deadbeef"},
                      data={"account_id": "chk", "amount_sign": "bank"},
                      files={"file": ("x.csv", CSV, "text/csv")})
        self.assertEqual(r.status_code, 401)

    def test_revoke_garbage_id_is_404(self):
        self.assertEqual(self.client.post(
            "/api/tokens/revoke", json={"id": "not-a-uuid", "password": "correct-horse-battery"}).status_code, 404)

    # ---- push-only means PUSH only ------------------------------------------
    # The allowlist is exact (method, path): a prefix match on /api/import
    # would also cover rollback, the batch list and bulk analyze/run, letting
    # a leaked collector token UNDO imports and read import history.

    def _bearer(self):
        minted = self._mint("collector")
        return TestClient(self.client.app), {
            "Authorization": f"Bearer {minted['token']}"}

    def test_token_cannot_rollback_an_import(self):
        bare, hdr = self._bearer()
        r = bare.post("/api/import/rollback", headers=hdr,
                      json={"batch_id": "anything"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("push", r.text)

    def test_token_cannot_read_import_history_or_run_bulk(self):
        bare, hdr = self._bearer()
        self.assertEqual(
            bare.get("/api/import/batches", headers=hdr).status_code, 403)
        for path in ("/api/import/bulk/analyze", "/api/import/bulk/run",
                     "/api/import/taxdoc/analyze", "/api/import/taxdoc/commit"):
            r = bare.post(path, headers=hdr)
            self.assertEqual(r.status_code, 403, f"{path} -> {r.status_code}")

    def test_token_still_opens_every_documented_push_door(self):
        # each allowed door must get PAST the auth gate — the 4xx that
        # comes back is the route's own validation, never the 403 wall
        bare, hdr = self._bearer()
        r = bare.post("/api/accounts/balance", headers=hdr,
                      json={"account_id": "nope", "balance": 1})
        self.assertEqual(r.status_code, 404)          # route ran: no account
        r = bare.post("/api/import/plan-activity", headers=hdr, json={"plan": "XX"})
        self.assertEqual(r.status_code, 400)          # route ran: bad plan
        r = bare.post("/api/import/mapped", headers=hdr,
                      data={"account_id": "chk", "amount_sign": "bank",
                            "filename": "x.csv", "date_col": "0",
                            "desc_col": "1", "amount_col": "2"},
                      files={"file": ("x.csv", CSV, "text/csv")})
        self.assertNotEqual(r.status_code, 403)       # gate open


if __name__ == "__main__":
    unittest.main()
