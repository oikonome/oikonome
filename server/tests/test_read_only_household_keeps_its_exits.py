"""A household held read-only keeps every way OUT.

Read-only stops writes to the household's money; it must never trap the
people in it. Deleting the account, a member leaving, the owner removing a
member or making them view-only, revoking a script token or support
access, unlinking a bank, deleting a webhook and muting an alert all only
take something away, so they stay open while ordinary writes answer 402.
A change that GRANTS something (a member promoted to editor) is still
refused.

The exits are one declared table of exact (method, path) pairs
(permissions.READ_ONLY_EXITS). The inventory half below pins that every
mutating route the old path-family allowlist used to wave through is now
either in that table or deliberately left gated, so a new door under a
family can never skip the read-only gate without somebody deciding it
should.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.web import permissions

from .util import _ensure_db, all_routes

PW = "correct-horse-battery"
MEMBER_PW = "family-member-pass"

# Mutating doors under the families the read-only gate used to admit
# wholesale that are NOT exits, each with the reason. Empty today: every
# door under those families is listed in the table on purpose. A new door
# lands in neither and fails the inventory below.
DELIBERATELY_GATED: dict[str, str] = {}

# the path families the read-only gate matched by prefix before the table
OLD_FAMILIES = ("/api/logout", "/api/export", "/api/auth", "/api/stepup",
                "/api/recovery/cancel-pending", *permissions.SELF_WRITE_OK)


class ReadOnlyHouseholdExitsTests(unittest.TestCase):
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
        self.email = f"exit-{uuid.uuid4().hex[:8]}@example.dev"
        self.owner.post("/api/signup", data={"email": self.email,
                                             "password": PW})
        self.owner.post("/api/auth/elevate", json={"password": PW})
        r = self.owner.post("/api/invites", json={
            "label": "partner", "role": "member", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["url"].rsplit("token=", 1)[1]
        self.member = TestClient(self.app)
        member_email = f"exit-m-{uuid.uuid4().hex[:8]}@example.dev"
        got = self.member.post("/api/invite/claim", json={
            "token": token, "email": member_email, "password": MEMBER_PW})
        self.assertEqual(got.status_code, 200, got.text)
        users = self.owner.get("/api/users").json()["users"]
        self.member_id = next(u["id"] for u in users
                              if u["email"] == member_email)
        minted = self.owner.post("/api/tokens", json={
            "name": "collector", "scope": "push", "password": PW})
        self.assertEqual(minted.status_code, 200, minted.text)
        self.token_id = minted.json()["id"]
        blocked = mock.patch.object(self.appmod, "_billing_write_blocked",
                                    return_value=True)
        blocked.start()
        self.addCleanup(blocked.stop)

    def test_the_owner_deletes_the_account_while_read_only(self):
        r = self.owner.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.owner.post("/api/account/delete", data={})
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_owner_removes_a_member_while_read_only(self):
        r = self.owner.request("DELETE", f"/api/users/{self.member_id}",
                               json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_owner_limits_a_member_but_cannot_promote_one(self):
        up = self.owner.post(f"/api/users/{self.member_id}/role",
                             json={"role": "member", "password": PW})
        self.assertEqual(up.status_code, 402, up.text)
        down = self.owner.post(f"/api/users/{self.member_id}/role",
                               json={"role": "viewer", "password": PW})
        self.assertEqual(down.status_code, 200, down.text)

    def test_the_owner_revokes_a_token_and_support_access_while_read_only(
            self):
        r = self.owner.post("/api/tokens/revoke",
                            json={"id": self.token_id, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.owner.post("/api/support-access/revoke", json={})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_member_leaves_while_read_only(self):
        r = self.member.post("/api/auth/elevate",
                             json={"password": MEMBER_PW})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.member.post("/api/account/leave", data={})
        self.assertNotEqual(r.status_code, 402, r.text)
        self.assertLess(r.status_code, 300, r.text)

    def test_an_ordinary_write_is_still_refused(self):
        r = self.owner.post("/api/accounts/classify", json={})
        self.assertEqual(r.status_code, 402, r.text)
        r = self.owner.post("/api/invites", json={
            "label": "x", "role": "viewer", "password": PW})
        self.assertEqual(r.status_code, 402, r.text)
        r = self.owner.post("/api/support-access", json={
            "hours": 1, "password": PW})
        self.assertEqual(r.status_code, 402, r.text)

    def test_an_emailed_mute_works_while_read_only_and_a_category_does_not(
            self):
        from oikonome.notify import mailact
        tid = self.owner.get("/api/me").json()["tenant_id"]
        base = {"tenant_id": tid, "email": self.email, "target": "x",
                "value": ""}
        self.assertIsNone(mailact._standing({**base, "action": "mute"}))
        refused = mailact._standing({**base, "action": "cat"})
        self.assertEqual((refused or {}).get("state"), "blocked")


class ReadOnlyExitInventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod
        cls.routes = {}
        for r in all_routes(appmod.app):
            for m in set(r.methods) - {"GET", "HEAD", "OPTIONS"}:
                cls.routes[(m, r.path)] = r

    def test_every_exit_is_a_real_route(self):
        for method, path in permissions.READ_ONLY_EXITS:
            self.assertIn((method, path), self.routes, (method, path))

    def test_no_door_under_an_old_family_skips_the_gate_undecided(self):
        for (method, path) in self.routes:
            if not permissions.path_within(path, OLD_FAMILIES):
                continue
            listed = (method, path) in permissions.READ_ONLY_EXITS
            self.assertTrue(listed or f"{method} {path}" in DELIBERATELY_GATED,
                            f"{method} {path} is under a family the "
                            "read-only gate used to admit; list it in "
                            "permissions.READ_ONLY_EXITS or gate it here")

    def test_matching_is_exact(self):
        ok = self.appmod.billing_write_ok
        self.assertTrue(ok("POST", "/api/account/delete"))
        self.assertTrue(ok("DELETE", f"/api/users/{uuid.uuid4()}"))
        self.assertTrue(ok("POST", f"/api/users/{uuid.uuid4()}/notify"))
        for method, path in (("POST", "/api/account/delete/x"),
                             ("GET", "/api/account/delete"),
                             ("POST", "/api/auth/elevate/x"),
                             ("POST", "/api/auth/other"),
                             ("POST", "/api/testing/other"),
                             ("POST", "/api/export/other"),
                             ("DELETE", "/api/users/x/y"),
                             ("POST", "/api/users/x/notify/more"),
                             ("POST", "/api/users"),
                             ("POST", "/api/accounts/classify")):
            self.assertFalse(ok(method, path), (method, path))


if __name__ == "__main__":
    unittest.main()
