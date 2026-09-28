"""A script token must not outlive the standing that justified it.

current_user()'s Bearer branch enforces every lockout the cookie path
enforces. Otherwise a suspended, pending-delete or frozen tenant keeps a
fully working WRITE path through the collector doors indefinitely, and
cannot even revoke the token, because the UI that revokes it is locked out
too.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db


class TokenLockoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"tl-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        minted = cls.client.post(
            "/api/tokens",
            json={"name": "collector", "password": "correct-horse-battery"}
        ).json()
        cls.hdr = {"Authorization": f"Bearer {minted['token']}"}

    def _status(self, status):
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenants SET status = %s WHERE id = %s",
                          (status, self.tid))
        finally:
            admin.close()

    def tearDown(self):
        self._status("active")

    def _push(self):
        """A real collector door — balance update, one of SCRIPT_TOKEN_ALLOW."""
        return TestClient(self.appmod.app).post(
            "/api/accounts/balance", headers=self.hdr,
            json={"account_id": "nope", "balance": 1})

    def test_suspended_tenant_token_is_refused(self):
        self._status("suspended")
        r = self._push()
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("suspended", r.text)

    def test_pending_delete_tenant_token_is_refused(self):
        self._status("pending_delete")
        r = self._push()
        self.assertEqual(r.status_code, 403, r.text)

    def test_active_tenant_token_still_works(self):
        """The guard must not break the collectors it is wrapped around: an
        active tenant's token reaches the handler (404 for the unknown
        account, NOT 401/402/403 from the gate)."""
        r = self._push()
        self.assertNotIn(r.status_code, (401, 402, 403))


if __name__ == "__main__":
    unittest.main()
