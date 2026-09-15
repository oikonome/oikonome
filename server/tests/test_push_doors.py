"""the Coinbase and Amazon push doors — HTTP endpoints so those
collectors ride script tokens like the plan-CSV collector, instead of podman-exec'ing
a raw-SQL bridge into the container."""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, write_config

TODAY = dt.date.today()

CB_PAYLOAD = {
    "prefix": "coinbase:",
    "items": [{"id": "coinbase-test", "institution_name": "Coinbase"}],
    "accounts": [{"id": "coinbase-test", "item_id": "coinbase-test",
                  "name": "Coinbase (test)", "type": "investment",
                  "subtype": "crypto", "balance_current": 1234.56}],
    "transactions": [
        {"id": "coinbase:t1", "account_id": "coinbase-test",
         "date": TODAY.isoformat(), "amount": 100.0, "name": "BUY BTC"},
        {"id": "coinbase:t2", "account_id": "coinbase-test",
         "date": TODAY.isoformat(), "amount": -25.0, "name": "SELL ETH"},
    ],
}

AZ_PAYLOAD = {"orders": [
    {"dedup_key": "az-test-1", "account": "main",
     "date": TODAY.isoformat(), "amount": -42.99,
     "payee": "Amazon", "order_number": "111-222",
     "items": [{"title": "USB cable", "price": 42.99}]},
]}


class PushDoorTests(unittest.TestCase):
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
            "email": f"pd-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            write_config(conn)
        finally:
            conn.close()
        minted = cls.client.post("/api/tokens",
                                 json={"name": "cb-az", "password": "correct-horse-battery"}).json()
        cls.hdr = {"Authorization": f"Bearer {minted['token']}"}

    def _conn(self):
        return tenancy.tenant_connect(self.tid)

    def test_coinbase_door_with_script_token(self):
        bare = TestClient(self.client.app)
        r = bare.post("/api/import/coinbase", headers=self.hdr,
                      json=CB_PAYLOAD)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["pushed"], 2)
        self.assertEqual(out["accounts"]["coinbase-test"]["after"], 2)
        self.assertEqual(out["accounts"]["coinbase-test"]["balance"], 1234.56)
        # window-replace: a re-push without t2 retires it
        p2 = dict(CB_PAYLOAD, transactions=[CB_PAYLOAD["transactions"][0]])
        r = bare.post("/api/import/coinbase", headers=self.hdr, json=p2)
        out = r.json()
        self.assertEqual(out["stale_removed"], 1)
        self.assertEqual(out["accounts"]["coinbase-test"]["after"], 1)

    def test_amazon_door_with_script_token(self):
        bare = TestClient(self.client.app)
        r = bare.post("/api/import/amazon", headers=self.hdr,
                      json=AZ_PAYLOAD)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["pushed"], 1)
        self.assertEqual(out["new"], 1)
        self.assertIn("amazon_match", out)
        # idempotent: same push again creates nothing
        r = bare.post("/api/import/amazon", headers=self.hdr,
                      json=AZ_PAYLOAD)
        self.assertEqual(r.json()["new"], 0)
        conn = self._conn()
        try:
            row = conn.execute("SELECT payee, amount FROM amazon_orders "
                               "WHERE dedup_key='az-test-1'").fetchone()
            self.assertEqual(row["amount"], -42.99)
        finally:
            conn.close()

    def test_costco_door_with_script_token(self):
        bare = TestClient(self.client.app)
        payload = {"receipts": [{
            "dedup_key": "cs-test-1", "date": "2026-07-01", "amount": -88.20,
            "receipt_type": "warehouse", "warehouse": "TEST WHSE",
            "category": "Food & Drink", "summary": "groceries $88",
            "items": [{"description": "ORG BANANAS", "amount": 88.20,
                       "department": 65, "category": "Food & Drink"}]}],
            "kind": "scrape"}
        r = bare.post("/api/import/costco", headers=self.hdr, json=payload)
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        self.assertEqual(out["pushed"], 1)
        self.assertEqual(out["new"], 1)
        self.assertIn("costco_match", out)
        # idempotent: the same push again creates and refreshes nothing
        r = bare.post("/api/import/costco", headers=self.hdr, json=payload)
        self.assertEqual(r.json()["new"], 0)
        self.assertEqual(r.json()["refreshed"], 0)
        conn = self._conn()
        try:
            row = conn.execute("SELECT warehouse, amount FROM costco_receipts "
                               "WHERE dedup_key='cs-test-1'").fetchone()
            self.assertEqual(row["amount"], -88.20)
            self.assertEqual(row["warehouse"], "TEST WHSE")
        finally:
            conn.close()

    def test_bad_payloads_are_400(self):
        bare = TestClient(self.client.app)
        self.assertEqual(bare.post("/api/import/coinbase", headers=self.hdr,
                                   json={}).status_code, 400)
        self.assertEqual(bare.post("/api/import/amazon", headers=self.hdr,
                                   json={"orders": []}).status_code, 400)
        self.assertEqual(bare.post("/api/import/costco", headers=self.hdr,
                                   json={"receipts": []}).status_code, 400)

    def test_session_auth_works_too(self):
        # owner sessions can use the doors as well (not token-only)
        r = self.client.post("/api/import/amazon", json=AZ_PAYLOAD)
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()


class HeartbeatViaTests(unittest.TestCase):
    """Doctor shows HOW each collector pushes — heartbeats record the
    mechanism (token/app/exec) at stamp time."""

    def test_token_and_direct_stamps_record_their_via(self):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        client = TestClient(app)
        client.post("/api/signup", data={
            "email": f"hv-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            write_config(conn)
        finally:
            conn.close()
        minted = client.post("/api/tokens", json={"name": "via", "password": "correct-horse-battery"}).json()
        bare = TestClient(client.app)
        r = bare.post("/api/import/amazon",
                      headers={"Authorization": f"Bearer {minted['token']}"},
                      json=AZ_PAYLOAD)
        self.assertEqual(r.status_code, 200, r.text)
        conn = tenancy.tenant_connect(tid)
        try:
            row = conn.execute("SELECT via FROM script_heartbeats "
                               "WHERE source='amazon-orders'").fetchone()
            self.assertEqual(row["via"], "token")
            # a direct in-container stamp (the exec bridges) defaults exec
            from oikonome.sync import heartbeat
            heartbeat.stamp(conn, "bridge-test", rows=1, label="Bridge")
            row = conn.execute("SELECT via FROM script_heartbeats "
                               "WHERE source='bridge-test'").fetchone()
            self.assertEqual(row["via"], "exec")
        finally:
            conn.close()


class PushTokenCharsetTests(unittest.TestCase):
    """Registration enforces each platform's token alphabet — APNs tokens
    are hex, FCM tokens URL-safe — because the token is later spliced into
    vendor URL paths by the push relay, and a "token" carrying path or
    query syntax must never get that far. The relay independently enforces
    the same shape; this pins the instance-side fence."""

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
            "email": f"pc-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        # push registration is device-token-only — mint a device first
        minted = cls.client.post("/api/devices", json={
            "device_name": "charset phone", "platform": "android"}).json()
        cls.bare = TestClient(cls.client.app)
        cls.hdr = {"Authorization": f"Bearer {minted['token']}"}

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _register(self, token, platform):
        return self.bare.post("/api/devices/push", headers=self.hdr,
                              json={"token": token, "platform": platform})

    def test_apns_token_must_be_hex(self):
        r = self._register("not-hex-not-hex-not-hex", "apns")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("malformed token", r.text)
        r = self._register("0A1b2C3d" * 8, "apns")   # 64 hex chars
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["registered"])

    def test_fcm_token_rejects_spaces_control_chars_and_slashes(self):
        for bad in ("token with spaces here",
                    "token\nwith\nnewlines",
                    "token/../../v1/evil"):
            r = self._register(bad, "fcm")
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn("malformed token", r.text)
        # the real FCM alphabet still goes through
        r = self._register("fcm-token_OK:with.allowed~chars%20", "fcm")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["registered"])
