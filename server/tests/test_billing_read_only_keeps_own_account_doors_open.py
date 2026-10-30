"""A household that is read-only for billing keeps every person's own
account doors open.

A lapsed subscription stops writes to the household's money. It is no
reason to refuse a password change, keep a stolen session or device token
live, or go on sending notices to someone who switched them off: the
worker keeps sending to the household through its wind-down, so the
switches that stop it have to keep working too. The doors that touch only
the caller's own credentials and delivery (permissions.SELF_WRITE_OK),
and the owner's switches for one member's notices, stay open; a write to
the household's data still answers 402.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.web import permissions

from .util import _ensure_db

PW = "correct-horse-battery"
MEMBER_PW = "family-member-pass"


class BillingReadOnlyOwnDoorsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.owner = TestClient(self.app)
        email = f"own-{uuid.uuid4().hex[:8]}@example.dev"
        self.owner.post("/api/signup", data={"email": email, "password": PW})
        self.owner.post("/api/auth/elevate", json={"password": PW})
        r = self.owner.post("/api/invites", json={
            "label": "partner", "role": "member", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["url"].rsplit("token=", 1)[1]
        self.member = TestClient(self.app)
        self.member_email = f"member-{uuid.uuid4().hex[:8]}@example.dev"
        got = self.member.post("/api/invite/claim", json={
            "token": token, "email": self.member_email,
            "password": MEMBER_PW})
        self.assertEqual(got.status_code, 200, got.text)
        users = self.owner.get("/api/users").json()["users"]
        self.member_id = next(u["id"] for u in users
                              if u["email"] == self.member_email)
        blocked = mock.patch.object(self.appmod, "_billing_write_blocked",
                                    return_value=True)
        blocked.start()
        self.addCleanup(blocked.stop)

    def test_a_member_turns_household_notices_off_while_read_only(self):
        r = self.member.post("/api/me/push", json={"muted": True})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.member.post("/api/me/email", json={"muted": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.member.get("/api/me").json()["push_muted"])

    def test_the_owner_sets_a_members_notices_while_read_only(self):
        r = self.owner.post(f"/api/users/{self.member_id}/notify",
                            json={"push_muted": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["notify"]["push_muted"])

    def test_sessions_and_password_stay_in_the_persons_hands(self):
        r = self.member.post("/api/sessions/revoke",
                             json={"all_others": True,
                                   "password": MEMBER_PW})
        self.assertNotEqual(r.status_code, 402, r.text)
        r = self.member.post("/api/password/change", json={
            "current_password": MEMBER_PW,
            "new_password": "another-long-passphrase"})
        self.assertNotEqual(r.status_code, 402, r.text)
        r = self.member.post("/api/devices/revoke", json={})
        self.assertNotEqual(r.status_code, 402, r.text)

    def test_a_write_to_the_household_is_still_refused(self):
        r = self.owner.post("/api/accounts/classify", json={})
        self.assertEqual(r.status_code, 402, r.text)
        r = self.owner.post("/api/users/x/role", json={"role": "viewer"})
        self.assertEqual(r.status_code, 402, r.text)

    def test_every_own_account_door_is_open_while_read_only(self):
        for door in permissions.SELF_WRITE_OK:
            self.assertTrue(self.appmod.billing_write_ok(door), door)
            self.assertTrue(self.appmod.billing_write_ok(door + "/x"), door)
        self.assertTrue(self.appmod.billing_write_ok(
            f"/api/users/{uuid.uuid4()}/notify"))
        for shut in ("/api/users/x/notify/more", "/api/users/x/role",
                     "/api/users", "/api/accounts/classify"):
            self.assertFalse(self.appmod.billing_write_ok(shut), shut)


if __name__ == "__main__":
    unittest.main()
