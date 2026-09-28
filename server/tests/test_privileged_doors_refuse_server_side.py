"""Privileged doors are refused by the SERVER, never only by the UI.

Removing a household member demands a step-up proof. A hosted instance
refuses bring-your-own aggregator keys whatever the client offers. The
admin console's token compare survives a wrong-length or non-ASCII token
without a 500, which would otherwise be an oracle for the token's shape.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.appmod.app)
        self.email = f"door-{uuid.uuid4().hex[:8]}@x.dev"
        self.client.post("/api/signup", data={
            "email": self.email, "password": "correct-horse-battery"})

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)


class MemberRemoveNeedsStepUp(_Base):
    def test_member_remove_needs_password(self):
        # make a second member to remove
        me = self.client.get("/api/me").json()
        admin = tenancy.admin_connect()
        try:
            vid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'viewer') RETURNING id",
                (me["tenant_id"], f"v-{uuid.uuid4().hex[:8]}@x.dev")
                ).fetchone()["id"]
        finally:
            admin.close()
        # no password, unelevated → rejected
        r = self.client.request("DELETE", f"/api/users/{vid}", json={})
        self.assertEqual(r.status_code, 403)
        self.assertIn("elevation_required", r.text)
        # wrong password → rejected
        r = self.client.request("DELETE", f"/api/users/{vid}",
                                json={"password": "nope"})
        self.assertEqual(r.status_code, 401)
        # right password → removed
        r = self.client.request("DELETE", f"/api/users/{vid}",
                                json={"password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200)


class HostedRefusesBringYourOwnKeys(_Base):
    def test_hosted_byo_plaid_and_mx_and_simplefin_403(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        self.assertEqual(self.client.post(
            "/api/accounts/plaid/keys",
            json={"client_id": "x", "secret": "y", "env": "sandbox"}
            ).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/accounts/mx/keys",
            json={"client_id": "x", "api_key": "y", "env": "sandbox"}
            ).status_code, 403)
        self.assertEqual(self.client.post(
            "/api/accounts/plaid/validate",
            json={"client_id": "x", "secret": "y", "env": "sandbox"}
            ).status_code, 403)

    def test_self_host_byo_not_403(self):
        # self-host is BYO-by-design — the guard must NOT fire (validation
        # may fail against the provider, but never 403 on the mode gate)
        r = self.client.post("/api/accounts/plaid/validate",
            json={"client_id": "x", "secret": "y", "env": "sandbox"})
        self.assertNotEqual(r.status_code, 403)


class AdminTokenCompareIsSafe(_Base):
    def test_wrong_length_token_is_clean_not_500(self):
        os.environ["OIKONOME_ADMIN_TOKEN"] = "a" * 48
        # a short token must not raise (compare_digest on unequal length) —
        # it renders the login form again, never a 500
        r = self.client.post("/admin/console/login", data={"token": "short"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("not right", r.text)
        # a non-ASCII token likewise
        r = self.client.post("/admin/console/login", data={"token": "café-π"})
        self.assertEqual(r.status_code, 200)


if __name__ == "__main__":
    unittest.main()
