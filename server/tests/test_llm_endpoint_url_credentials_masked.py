"""An LLM endpoint URL can carry its own credential — a password in the
userinfo (sent as Basic auth) or a key in the query string. GET
/api/settings is readable by every household member, viewers included, so
those parts leave masked everywhere the URL is echoed. The masked form a
client sends back on a round-trip save must KEEP the stored credential, and
a mask that no longer matches the stored URL is refused rather than saved
literally (which would silently break the backend)."""

import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db

SECRET = "pw-invented-7731"
QKEY = "qk-invented-4410"


class LlmEndpointUrlCredentialsMaskedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.email = f"llmurl-{uuid.uuid4().hex[:8]}@example.dev"
        cls.client.post("/api/signup", data={
            "email": cls.email, "password": "correct-horse-battery"})
        # repointing mail, the AI endpoint or the daily report is a step-up act
        cls.client.post("/api/auth/elevate",
                        json={"password": "correct-horse-battery"})
        from oikonome.db import tenancy
        admin = tenancy.admin_connect()
        try:
            cls.tid = str(admin.execute(
                "SELECT tenant_id FROM users WHERE email=%s",
                (cls.email,)).fetchone()["tenant_id"])
        finally:
            admin.close()

    def _stored(self):
        from oikonome.db import tenancy
        from oikonome.engine import budget
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn)
        finally:
            conn.close()

    def test_legacy_url_is_masked_and_round_trips(self):
        url = f"http://someone:{SECRET}@cfg.test:9090/v1"
        r = self.client.post("/api/settings", json={
            "llm_url": url, "llm_model": "gemma3"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn(SECRET, r.text)
        r = self.client.get("/api/settings")
        self.assertNotIn(SECRET, r.text)
        masked = r.json()["llm_url"]
        self.assertIn("cfg.test:9090", masked)
        self.assertEqual("http://someone:***@cfg.test:9090/v1",
                         r.json()["llm_effective"]["categorize"]["url"])
        # the client sends back exactly what it was shown
        r = self.client.post("/api/settings", json={
            "llm_url": masked, "llm_model": "gemma3"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(url, self._stored()["llm_url"])
        # a mask on a different host has nothing to restore from
        r = self.client.post("/api/settings", json={
            "llm_url": "http://someone:***@other.test:9090/v1",
            "llm_model": "gemma3"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(url, self._stored()["llm_url"])

    def test_backend_url_query_key_is_masked_and_round_trips(self):
        url = f"http://cfg.test:9091/v1?api_key={QKEY}&tier=a"
        r = self.client.post("/api/settings", json={"llm_backends": [
            {"id": "q1", "name": "Q", "url": url, "model": "m1"}],
            "llm_roles": {"assistant": "q1"}})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.get("/api/settings")
        self.assertNotIn(QKEY, r.text)
        bk = next(b for b in r.json()["llm_backends"] if b["id"] == "q1")
        self.assertIn("tier=a", bk["url"])
        r = self.client.post("/api/settings", json={"llm_backends": [
            {"id": "q1", "name": "Q", "url": bk["url"], "model": "m1"}]})
        self.assertEqual(r.status_code, 200, r.text)
        stored = next(b for b in self._stored()["llm_backends"]
                      if b["id"] == "q1")
        self.assertEqual(url, stored["url"])


if __name__ == "__main__":
    unittest.main()
