"""A script token dies with the person who made it.

`api_tokens.created_by` is ON DELETE SET NULL, so deleting a user row
leaves their tokens in place — and a push token acts as the owner on its
doors. The invariant: once a person is removed from the household (by an
owner, a second owner included) or leaves it, every script token they
minted is refused. The failure mode it guards is a removed co-owner's
collector token that keeps importing into the household indefinitely.
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import api_tokens
from oikonome.db import tenancy

from .util import _admin_dsn, _ensure_db, TEST_DB

PASSWORD = "correct-horse-battery"


class RemovedPersonScriptTokenTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        self.owner = TestClient(self.app)
        self.owner.post("/api/signup", data={
            "email": f"own-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PASSWORD})
        self.tid = self.owner.get("/api/me").json()["tenant_id"]

    def _mint_for(self, user_id) -> str:
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            tok, _row = api_tokens.mint(admin, self.tid, user_id, "collector",
                                        scope="read")
        finally:
            admin.close()
        return tok

    def _read(self, tok):
        return TestClient(self.app).get(
            "/api/integrations/summary",
            headers={"Authorization": f"Bearer {tok}"}).status_code

    def test_removing_a_second_owner_ends_their_script_tokens(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            b = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,'h','owner',now()) RETURNING id",
                (self.tid, f"own2-{uuid.uuid4().hex[:6]}@example.dev")
            ).fetchone()["id"]
        finally:
            admin.close()
        tok = self._mint_for(b)
        self.assertNotEqual(self._read(tok), 401)
        r = self.owner.request("DELETE", f"/api/users/{b}",
                               json={"password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._read(tok), 401,
                         "a removed owner's script token still works")

    def test_leaving_the_household_ends_your_script_tokens(self):
        r = self.owner.post("/api/invites", json={"label": "fam",
                                                  "password": PASSWORD})
        invite = r.json()["url"].rsplit("token=", 1)[1]
        person = TestClient(self.app)
        email = f"fam-{uuid.uuid4().hex[:8]}@example.dev"
        r = person.post("/api/invite/claim", json={
            "token": invite, "email": email,
            "password": "family-member-pass"})
        self.assertEqual(r.status_code, 200, r.text)
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            uid = admin.execute("SELECT id FROM users WHERE email=%s",
                                (email,)).fetchone()["id"]
        finally:
            admin.close()
        # a token made while this person held a role that could mint one
        tok = self._mint_for(uid)
        self.assertNotEqual(self._read(tok), 401)
        r = person.post("/api/account/leave",
                        data={"password": "family-member-pass"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._read(tok), 401,
                         "a departed person's script token still works")


if __name__ == "__main__":
    unittest.main()
