"""A household member whose session has no strong factor can still leave.

Hosted instances block every write from a factorless session until a
second factor is enrolled. Leaving the household re-authenticates with the
password on its own, so that gate is redundant there — and without the
exemption a member invited by mistake (or holding a session that pre-dates
the requirement) is trapped: cannot leave, must enroll on an account that
is not theirs. The leave door must reach its own password check, not the
enrollment refusal.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


class ViewerLeaveTests(unittest.TestCase):
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
            "password": "correct-horse-battery"})
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
        inv = self.owner.post("/api/invites", json={
            "label": "spouse", "password": "correct-horse-battery"}).json()
        token = inv["url"].rsplit("token=", 1)[1]
        self.viewer = TestClient(self.app)
        self.email = f"fam-{uuid.uuid4().hex[:8]}@example.dev"
        r = self.viewer.post("/api/invite/claim", json={
            "token": token, "email": self.email,
            "password": "family-member-pass"})
        assert r.status_code == 200, r.text
        os.environ["OIKONOME_HOSTED"] = "1"

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def test_factorless_viewer_is_gated_but_can_leave(self):
        me = self.viewer.get("/api/me").json()
        self.assertTrue(me["needs_2fa"])
        # ordinary writes stay blocked by the enrollment gate
        r = self.viewer.post("/api/me/email", json={"muted": True})
        self.assertEqual(r.status_code, 403)
        self.assertIn("second factor", r.text)
        # a wrong password reaches leave's own check, not the gate
        r = self.viewer.post("/api/account/leave",
                             data={"password": "not-it"})
        self.assertNotEqual(r.status_code, 403, r.text)
        self.assertNotIn("second factor", r.text)
        # the right password leaves
        r = self.viewer.post("/api/account/leave",
                             data={"password": "family-member-pass"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotEqual(self.viewer.get("/api/me").status_code, 200)
