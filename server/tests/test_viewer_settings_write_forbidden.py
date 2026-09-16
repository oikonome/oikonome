"""A view-only household member must never rewrite tenant settings.

The Settings endpoint itself carries no role check — the refusal lives in
the one gate every protected route passes through (the non-GET viewer
block in current_user, with /api/settings absent from VIEWER_WRITE_OK).
This pins that a viewer's POST /api/settings is 403 and persists nothing,
so the gate's coverage of the endpoint can never silently regress."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


class ViewerSettingsWriteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.owner = TestClient(app)
        cls.owner.post("/api/signup", data={
            "email": f"vso-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn, food_monthly=1234)
        finally:
            conn.close()
        inv = cls.owner.post("/api/invites", json={
            "label": "spouse", "password": "correct-horse-battery"}).json()
        token = inv["url"].rsplit("token=", 1)[1]
        cls.viewer = TestClient(app)
        r = cls.viewer.post("/api/invite/claim", json={
            "token": token,
            "email": f"vsv-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        assert r.status_code == 200, r.text

    def test_viewer_post_settings_is_403_and_persists_nothing(self):
        self.assertEqual(self.viewer.get("/api/me").json()["role"], "viewer")
        # a plain budget write and a credential/egress write both refuse
        r = self.viewer.post("/api/settings", json={"food_monthly": 1})
        self.assertEqual(r.status_code, 403)
        r = self.viewer.post("/api/settings", json={
            "email_recipients": ["attacker@example.dev"],
            "llm_url": "http://cfg.test:9090", "llm_model": "m",
            "llm_api_key": "sk-viewer-planted"})
        self.assertEqual(r.status_code, 403)
        got = self.owner.get("/api/settings").json()
        self.assertEqual(got["food_monthly"], 1234)
        self.assertFalse(got["llm_api_key_set"])
        self.assertFalse(got.get("email_recipients"))
        # reads stay open — view-only, not locked out
        self.assertEqual(self.viewer.get("/api/settings").status_code, 200)


if __name__ == "__main__":
    unittest.main()
