"""/api/me `bank_link` — the hosted one-door bank-link flag.

True only when BOTH hosted mode and the PLATFORM Plaid credentials
(env-resolved, never tenant config) are present. The SPA's hosted
"Connect your accounts" card keys off it; while false it shows an honest
not-yet-live note instead of a dead button. Self-host is always false —
it keeps the provider grid regardless of any env keys.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db

_ENV = ("OIKONOME_HOSTED", "OIKONOME_PLAID_CLIENT_ID",
        "OIKONOME_PLAID_SECRET")


class BankLinkFlagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"bl-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in _ENV}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _flag(self):
        return self.client.get("/api/me").json()["bank_link"]

    def test_self_host_false_even_with_env_keys(self):
        os.environ["OIKONOME_PLAID_CLIENT_ID"] = "cid"
        os.environ["OIKONOME_PLAID_SECRET"] = "sec"
        self.assertFalse(self._flag())

    def test_hosted_without_platform_keys_false(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        self.assertFalse(self._flag())

    def test_hosted_with_partial_keys_false(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        os.environ["OIKONOME_PLAID_CLIENT_ID"] = "cid"
        self.assertFalse(self._flag())

    def test_hosted_with_platform_keys_true(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        os.environ["OIKONOME_PLAID_CLIENT_ID"] = "cid"
        os.environ["OIKONOME_PLAID_SECRET"] = "sec"
        self.assertTrue(self._flag())


if __name__ == "__main__":
    unittest.main()
