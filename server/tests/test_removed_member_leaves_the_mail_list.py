"""Removing a household member takes their address off the household's mail
list. Membership was their only reason to receive the daily balances and
spending mail; left on the list, a removed person keeps getting it (on
self-host no membership filter stands between the list and the send).
Leaving on one's own already did this; removal by the owner must too."""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db, seed_accounts, write_config

PW = "correct-horse-battery"
MEMBER_PW = "member-horse-battery"


class RemovedMemberLeavesTheMailList(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.owner = TestClient(cls.app)
        cls.owner.post("/api/signup", data={
            "email": f"own-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _member(self):
        r = self.owner.post("/api/invites", json={
            "label": "partner", "role": "member", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["url"].rsplit("token=", 1)[1]
        email = f"member-{uuid.uuid4().hex[:8]}@example.dev"
        got = TestClient(self.app).post("/api/invite/claim", json={
            "token": token, "email": email, "password": MEMBER_PW})
        self.assertEqual(got.status_code, 200, got.text)
        uid = next(u["id"] for u in self.owner.get("/api/users").json()["users"]
                   if u["email"] == email)
        return uid, email

    def _config(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    def test_removal_takes_the_address_off_recipients_and_mutes(self):
        uid, email = self._member()
        keep = "someone-else@example.dev"
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["email_recipients"] = [email.upper(), keep]
                cfg["email_muted"] = [email]
        finally:
            conn.close()
        r = self.owner.request("DELETE", f"/api/users/{uid}",
                               json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        cfg = self._config()
        self.assertEqual(cfg.get("email_recipients"), [keep])
        self.assertEqual(cfg.get("email_muted"), [])


if __name__ == "__main__":
    unittest.main()
