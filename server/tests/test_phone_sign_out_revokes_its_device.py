"""A phone signing out takes its device token (and the widget token under
it) off the server, whatever the account's standing.

The phone holds a 90-day bearer, not a cookie. A sign-out that only
forgets the token locally leaves it live server-side, and the screens an
account in bad standing sees can reach nothing but logout — so logout must
accept the device token and revoke it.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PASSWORD = "correct-horse-battery"


class PhoneSignOutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app

    def _phone(self):
        email = f"signout-{uuid.uuid4().hex[:8]}@example.dev"
        c = TestClient(self.app)
        c.post("/api/signup", data={"email": email, "password": PASSWORD})
        tid = c.get("/api/me").json()["tenant_id"]
        bare = TestClient(self.app)
        r = bare.post("/api/login", data={"email": email,
                                          "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        naked = TestClient(self.app)
        naked.cookies.clear()
        m = naked.post("/api/devices", json={
            "mint_ticket": r.json()["mint_ticket"],
            "device_name": "test phone", "platform": "android"})
        self.assertEqual(m.status_code, 200, m.text)
        return naked, tid, {"Authorization": f"Bearer {m.json()['token']}"}

    def _set_status(self, tenant_id, status):
        conn = tenancy.admin_connect()
        try:
            conn.execute("UPDATE tenants SET status=%s WHERE id=%s",
                         (status, tenant_id))
            conn.commit()
        finally:
            conn.close()

    def test_logout_with_the_device_token_revokes_it(self):
        naked, _tid, bearer = self._phone()
        w = naked.post("/api/devices/widget", headers=bearer)
        self.assertEqual(w.status_code, 200, w.text)
        widget = {"Authorization": f"Bearer {w.json()['token']}"}
        self.assertEqual(naked.get("/api/today/glance",
                                   headers=widget).status_code, 200)
        out = naked.post("/api/logout", headers=bearer)
        self.assertEqual(out.status_code, 200, out.text)
        self.assertEqual(naked.get("/api/me", headers=bearer).status_code,
                         401, "the device token outlived the sign-out")
        self.assertEqual(naked.get("/api/today/glance",
                                   headers=widget).status_code, 401,
                         "the widget token outlived its device")

    def test_a_locked_out_account_can_still_revoke_its_phone(self):
        naked, tid, bearer = self._phone()
        self._set_status(tid, "suspended")
        try:
            self.assertEqual(naked.post("/api/logout",
                                        headers=bearer).status_code, 200)
            self._set_status(tid, "active")
            self.assertEqual(naked.get("/api/me",
                                       headers=bearer).status_code, 401)
        finally:
            self._set_status(tid, "active")


if __name__ == "__main__":
    unittest.main()
