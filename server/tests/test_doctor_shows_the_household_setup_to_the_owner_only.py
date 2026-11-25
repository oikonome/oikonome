"""The household's setup — its mail relay, its AI endpoint and the models
it runs — is the owner's to read. GET
/api/settings withholds those values from members and viewers; the System
health page must not hand them back.

What breaks if this fails: a view-only member opens System health and
reads the owner's relay host, the address of the owner's own AI server
and its model names, all of which Settings refuses them.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import doctor

from .util import _ensure_db, write_config

RELAY = "relay.setup-only.example"
AI_HOST = "ai.setup-only.example"
MODEL = "setup-only-model-x"


class DoctorOwnerOnlySetupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.owner = TestClient(app)
        cls.owner.post("/api/signup", data={
            "email": f"dso-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            write_config(conn, smtp_host=RELAY, smtp_port=587,
                         smtp_from="home@example.dev",
                         llm_url=f"http://{AI_HOST}:8443/v1",
                         llm_model=MODEL)
        finally:
            conn.close()
        inv = cls.owner.post("/api/invites", json={
            "label": "spouse", "password": "correct-horse-battery"}).json()
        token = inv["url"].rsplit("token=", 1)[1]
        cls.viewer = TestClient(app)
        r = cls.viewer.post("/api/invite/claim", json={
            "token": token,
            "email": f"dsv-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        assert r.status_code == 200, r.text

    def _doctor(self, client, models=("other-model",), path="/api/doctor"):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("OIKONOME_LLM", "OIKONOME_SMTP"))
               and k not in ("OIKONOME_HOSTED",)}
        listing = {"data": [{"id": m} for m in models]}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(doctor, "_probe_json",
                                  lambda *a, **k: listing), \
                mock.patch("oikonome.web.netguard.check_url",
                           lambda *a, **k: None):
            r = client.get(path)
        self.assertEqual(r.status_code, 200, r.text)
        return r.text

    def test_a_viewer_reads_no_relay_host_endpoint_or_model(self):
        self.assertEqual(self.viewer.get("/api/me").json()["role"], "viewer")
        body = self._doctor(self.viewer)
        for secret in (RELAY, AI_HOST, MODEL, "other-model"):
            self.assertNotIn(secret, body)

    def test_a_viewers_diagnostic_bundle_names_no_model_either(self):
        """The bundle withholds addresses from everyone; the model names
        it carries are the owner's to read, like the page's."""
        body = self._doctor(self.viewer, path="/api/doctor/bundle")
        for secret in (RELAY, AI_HOST, MODEL, "other-model"):
            self.assertNotIn(secret, body)

    def test_the_owner_still_reads_the_setup_in_full(self):
        body = self._doctor(self.owner)
        for shown in (RELAY, AI_HOST, MODEL, "other-model"):
            self.assertIn(shown, body)


if __name__ == "__main__":
    unittest.main()
