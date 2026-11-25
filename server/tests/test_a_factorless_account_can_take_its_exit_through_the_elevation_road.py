"""An account standing at the second-factor gate can take the way out the
gate offers it, by the road both clients take.

The gate offers Delete account to the owner and Leave household to anyone
else. Both doors, called with no body (as the web and the app call them),
answer `elevation_required`; the client's confirm-it's-you sheet then reads
GET /api/auth/elevation for the proofs to collect and posts them to
/api/auth/elevate, and the door is retried. An account at the gate has, by
definition, no authenticator and no passkey — so the sheet must be told to
collect a password alone, the elevate door must open the window on a
password alone, and the retried exit must succeed. If any step refused a
factorless account, the gate would be offering a button that fails on
every press. Driven for a cookie session (web) and a device token (app),
for the owner, a member and a viewer. The role each client keys the button
on is checked against the door too: the owner's leave and a member's
delete are refused, which is why neither is offered.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import device_tokens
from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config

PW = "correct-horse-battery"


class FactorlessExitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        os.environ.pop("OIKONOME_HOSTED", None)
        self.owner = TestClient(self.app)
        self.owner_email = f"gx-{uuid.uuid4().hex[:8]}@example.dev"
        r = self.owner.post("/api/signup", data={
            "email": self.owner_email, "password": PW})
        assert r.status_code in (200, 303), r.text
        me = self.owner.get("/api/me").json()
        self.tid = me["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            self.owner_uid = admin.execute(
                "SELECT id FROM users WHERE email=%s",
                (self.owner_email,)).fetchone()["id"]
        finally:
            admin.close()
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def _joined(self, role: str) -> TestClient:
        inv = self.owner.post("/api/invites", json={
            "label": role, "role": role, "password": PW}).json()
        token = inv["url"].rsplit("token=", 1)[1]
        c = TestClient(self.app)
        r = c.post("/api/invite/claim", json={
            "token": token, "email": f"gm-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        assert r.status_code == 200, r.text
        return c

    def _gate(self):
        # the requirement switches on after the household is set up, the
        # way a session that pre-dates it meets the gate
        os.environ["OIKONOME_HOSTED"] = "1"

    def _exit_by_the_client_road(self, c: TestClient, door: str,
                                 password: str, headers=None):
        h = headers or {}
        me = c.get("/api/me", headers=h).json()
        self.assertTrue(me["needs_2fa"], me)
        # the client's first call carries no proof
        r = c.post(door, data={}, headers=h)
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"]["error"], "elevation_required")
        # the sheet asks what to collect: a password, and no code
        st = c.get("/api/auth/elevation", headers=h)
        self.assertEqual(st.status_code, 200, st.text)
        self.assertEqual(st.json()["methods"], ["password"])
        self.assertFalse(st.json()["totp"])
        # a password alone opens the window on a factorless account
        e = c.post("/api/auth/elevate", json={"password": password},
                   headers=h)
        self.assertEqual(e.status_code, 200, e.text)
        self.assertTrue(e.json()["elevated"])
        # and the retried exit goes through
        r = c.post(door, data={}, headers=h)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotEqual(c.get("/api/me", headers=h).status_code, 200)

    def test_a_factorless_owner_deletes_the_account_from_the_web(self):
        self._gate()
        self._exit_by_the_client_road(self.owner, "/api/account/delete", PW)

    def test_a_factorless_owner_deletes_the_account_from_the_app(self):
        admin = tenancy.admin_connect()
        try:
            tok, _row = device_tokens.mint(admin, self.owner_uid, self.tid,
                                           "phone", "android")
        finally:
            admin.close()
        self._gate()
        self._exit_by_the_client_road(
            TestClient(self.app), "/api/account/delete", PW,
            headers={"Authorization": f"Bearer {tok}"})

    def test_a_factorless_member_leaves_the_household(self):
        member = self._joined("member")
        self._gate()
        self._exit_by_the_client_road(member, "/api/account/leave",
                                      "family-member-pass")
        # the household is untouched
        self.assertEqual(self.owner.get("/api/me").status_code, 200)

    def test_a_factorless_viewer_leaves_the_household(self):
        viewer = self._joined("viewer")
        self._gate()
        self._exit_by_the_client_road(viewer, "/api/account/leave",
                                      "family-member-pass")

    def test_each_door_refuses_the_role_its_button_is_hidden_from(self):
        member = self._joined("member")
        self._gate()
        for c, pw in ((member, "family-member-pass"), (self.owner, PW)):
            e = c.post("/api/auth/elevate", json={"password": pw})
            self.assertEqual(e.status_code, 200, e.text)
        r = member.post("/api/account/delete", data={})
        self.assertEqual(r.status_code, 403, r.text)
        r = self.owner.post("/api/account/leave", data={})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self.owner.get("/api/me").status_code, 200)


if __name__ == "__main__":
    unittest.main()
