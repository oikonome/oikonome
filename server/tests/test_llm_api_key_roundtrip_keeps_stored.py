"""The legacy single-endpoint llm_api_key follows the masked-secret save
contract its per-backend twin and llm_extra_body already keep: the key is
never echoed on GET, so "" — all a client that didn't retype it can send
back — KEEPS the stored value, and only an explicit JSON null clears it.
Were "" a clear, every round-trip save of the AI card would silently wipe
the credential."""

import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db


class LlmApiKeyRoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"llmkey-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def test_empty_string_keeps_stored_key_null_clears(self):
        r = self.client.post("/api/settings", json={
            "llm_url": "http://cfg.test:9090", "llm_model": "gemma3",
            "llm_api_key": "sk-keep-me-999"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["llm_api_key_set"])
        # a full round-trip save (url + model + the masked "" the client
        # got back) must keep the stored key
        r = self.client.post("/api/settings", json={
            "llm_url": "http://cfg.test:9090", "llm_model": "gemma3",
            "llm_api_key": ""})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["llm_api_key_set"])
        # a key-only save with the masked "" (stored url fills in) too
        r = self.client.post("/api/settings", json={"llm_api_key": ""})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["llm_api_key_set"])
        self.assertNotIn("sk-keep-me-999", r.text)      # still never echoed
        # explicit JSON null is the deliberate clear
        r = self.client.post("/api/settings", json={
            "llm_url": "http://cfg.test:9090", "llm_model": "gemma3",
            "llm_api_key": None})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["llm_api_key_set"])

    def test_clearing_the_endpoint_still_drops_the_key(self):
        self.client.post("/api/settings", json={
            "llm_url": "http://cfg.test:9090", "llm_model": "gemma3",
            "llm_api_key": "sk-group-clear"})
        r = self.client.post("/api/settings", json={"llm_url": ""})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["llm_api_key_set"])


if __name__ == "__main__":
    unittest.main()
