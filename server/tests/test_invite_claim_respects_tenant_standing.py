"""Claiming a family invite must meet the same standing gates as every
other write — a claim is pre-auth, so it never passes through them.

A share link outlives the household's good standing: the owner is
suspended, scheduled for deletion, frozen by an add-on, and an
unguarded leftover link plants a verified viewer on the live ledger anyway.
Device mint and script tokens refuse this class; the claim door does too,
and refuses BEFORE burning the invite so the link works again once the
household does.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class InviteClaimStandingTests(unittest.TestCase):
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
        self.owner = TestClient(self.app)
        self.email = f"own-{uuid.uuid4().hex[:8]}@x.dev"
        r = self.owner.post("/api/signup", data={"email": self.email,
                                                 "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.owner.get("/api/me").json()["tenant_id"]
        r = self.owner.post("/api/invites",
                            json={"label": "fam", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.token = r.json()["url"].rsplit("token=", 1)[1]

    def _set_status(self, status):
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenants SET status=%s WHERE id=%s",
                          (status, self.tid))
        finally:
            admin.close()

    def _claim(self):
        return TestClient(self.app).post("/api/invite/claim", json={
            "token": self.token,
            "email": f"fam-{uuid.uuid4().hex[:8]}@x.dev",
            "password": "family-member-pass"})

    def _peek(self):
        return TestClient(self.app).get(
            f"/api/invite/peek?token={self.token}").status_code

    def test_suspended_household_cannot_grow_a_member(self):
        self._set_status("suspended")
        r = self._claim()
        self.assertEqual(r.status_code, 403, r.text)
        # refused, not spent: the link still stands for when standing returns
        self.assertEqual(self._peek(), 200)

    def test_pending_delete_refuses_too(self):
        self._set_status("pending_delete")
        self.assertEqual(self._claim().status_code, 403)

    def test_a_household_in_good_standing_still_claims(self):
        self._set_status("active")
        r = self._claim()
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_link_refused_while_suspended_works_after_reinstatement(self):
        self._set_status("suspended")
        self.assertEqual(self._claim().status_code, 403)
        self._set_status("active")
        self.assertEqual(self._claim().status_code, 200)


if __name__ == "__main__":
    unittest.main()
