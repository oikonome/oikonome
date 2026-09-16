"""Setup wizard: bootstrap happy path (with mocked SimpleFIN), the
aggregator chooser (SimpleFIN / Plaid / files-only comparison + optional
Plaid BYO keys), the optional smart-categorization LLM section, input
validation, and the no-users gate."""

import base64
import json
import unittest
import uuid

import httpx

from oikonome.db import tenancy
from oikonome.web import setup

from .util import _ensure_db, make_db
from .test_simplefin import PAYLOAD


def _transport():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, text="https://u:p@bridge.test/simplefin")
        return httpx.Response(200, text=json.dumps(PAYLOAD))
    return httpx.MockTransport(handler)


class SetupTests(unittest.TestCase):
    def setUp(self):
        _ensure_db()

    def test_bootstrap_creates_tenant_user_and_syncs(self):
        r = setup.bootstrap(f"owner-{uuid.uuid4().hex[:8]}@example.dev", "correct-horse-battery",
                            simplefin_token=base64.b64encode(
                                b"https://bridge.test/claim/abc").decode(),
                            transport=_transport())
        self.assertIsNotNone(r["tenant_id"])
        self.assertEqual(r["sync"]["accounts"], 2)
        conn = tenancy.tenant_connect(r["tenant_id"])
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM transactions "
                             "WHERE id LIKE 'sfin:%'").fetchone()["n"]
            self.assertEqual(n, 3)
        finally:
            conn.close()

    def test_bootstrap_without_token(self):
        r = setup.bootstrap(f"plain-{uuid.uuid4().hex[:8]}@example.dev", "correct-horse-battery")
        self.assertIsNone(r["sync"])

    def _tenant_config(self, tenant_id):
        conn = tenancy.tenant_connect(tenant_id)
        try:
            row = conn.execute(
                "SELECT config FROM tenant_settings").fetchone()
            cfg = row["config"] if row else {}
            return cfg if isinstance(cfg, dict) else json.loads(cfg)
        finally:
            conn.close()

    def test_setup_page_is_account_only(self):
        # connections + smart categorization live in the guided wizard AFTER
        # account creation — this page is email+password
        from oikonome.web import todayview
        html = todayview._env.get_template("setup.html").render(
            title="Setup")
        self.assertIn('name="email"', html)
        self.assertIn('type="password" name="password"', html)
        self.assertIn("Create my instance", html)
        for gone in ("Connect your banks", 'name="simplefin_token"',
                     'name="plaid_client_id"', 'name="plaid_secret"',
                     'name="llm_url"', "Smart categorization"):
            self.assertNotIn(gone, html)

    def test_bootstrap_stores_plaid_creds(self):
        r = setup.bootstrap(f"plaid-{uuid.uuid4().hex[:8]}@example.dev",
                            "correct-horse-battery",
                            plaid_client_id="cid-123",
                            plaid_secret="sec-456", plaid_env="sandbox")
        self.assertTrue(r["plaid_saved"])
        self.assertIsNone(r["sync"])
        cfg = self._tenant_config(r["tenant_id"])
        self.assertEqual(cfg["plaid_client_id"], "cid-123")
        self.assertEqual(cfg["plaid_secret"], "sec-456")
        self.assertEqual(cfg["plaid_env"], "sandbox")

    def test_bootstrap_unknown_plaid_env_falls_back_to_production(self):
        r = setup.bootstrap(f"env-{uuid.uuid4().hex[:8]}@example.dev",
                            "correct-horse-battery",
                            plaid_client_id="cid", plaid_secret="sec",
                            plaid_env="development")
        self.assertEqual(self._tenant_config(r["tenant_id"])["plaid_env"],
                         "production")

    def test_bootstrap_simplefin_and_plaid_both(self):
        r = setup.bootstrap(f"both-{uuid.uuid4().hex[:8]}@example.dev",
                            "correct-horse-battery",
                            simplefin_token=base64.b64encode(
                                b"https://bridge.test/claim/abc").decode(),
                            plaid_client_id="cid", plaid_secret="sec",
                            transport=_transport())
        self.assertEqual(r["sync"]["accounts"], 2)   # SimpleFIN ran as today
        self.assertTrue(r["plaid_saved"])            # AND keys stored
        cfg = self._tenant_config(r["tenant_id"])
        self.assertEqual(cfg["plaid_client_id"], "cid")
        self.assertEqual(cfg["plaid_env"], "production")

    def test_bootstrap_neither_files_only(self):
        r = setup.bootstrap(f"files-{uuid.uuid4().hex[:8]}@example.dev",
                            "correct-horse-battery")
        self.assertIsNone(r["sync"])
        self.assertFalse(r["plaid_saved"])
        self.assertNotIn("plaid_client_id", self._tenant_config(r["tenant_id"]))

    def test_bootstrap_partial_plaid_keys_rejected_before_user_created(self):
        email = f"half-{uuid.uuid4().hex[:8]}@example.dev"
        with self.assertRaises(ValueError):
            setup.bootstrap(email, "correct-horse-battery",
                            plaid_client_id="cid-only")
        admin = tenancy.admin_connect()
        try:
            self.assertIsNone(admin.execute(
                "SELECT 1 FROM users WHERE email=%s", (email,)).fetchone())
        finally:
            admin.close()

    def test_bootstrap_bad_simplefin_token_still_stores_plaid_keys(self):
        # a bad token must not brick the wizard NOR drop the Plaid keys
        r = setup.bootstrap(f"badtok-{uuid.uuid4().hex[:8]}@example.dev",
                            "correct-horse-battery",
                            simplefin_token="!!!not-base64-or-a-url!!!",
                            plaid_client_id="cid", plaid_secret="sec")
        self.assertIsNotNone(r["user_id"])
        self.assertIsNone(r["sync"])
        self.assertIn("retry", r["sync_error"])
        self.assertTrue(r["plaid_saved"])
        self.assertEqual(
            self._tenant_config(r["tenant_id"])["plaid_client_id"], "cid")

    def test_rerender_never_echoes_secrets(self):
        # even if a future caller leaks credentials into the render context,
        # the (account-only) template must not emit them
        from oikonome.web import todayview
        html = todayview._env.get_template("setup.html").render(
            title="Setup", error="boom", email="a@b.c",
            llm_api_key="sk-SUPER-SECRET-123", plaid_secret="ps-HIDDEN")
        self.assertIn("a@b.c", html)
        self.assertNotIn("sk-SUPER-SECRET-123", html)
        self.assertNotIn("ps-HIDDEN", html)

    def test_bootstrap_stores_llm_config(self):
        from oikonome.db import crypto, tenancy as _t
        r = setup.bootstrap(f"llm-{uuid.uuid4().hex[:8]}@example.dev",
                            "correct-horse-battery",
                            llm_url="http://llm.local:8080",
                            llm_model="gemma3", llm_api_key="sk-test-123")
        self.assertTrue(r["llm_saved"])
        cfg = self._tenant_config(r["tenant_id"])
        self.assertEqual(cfg["llm_url"], "http://llm.local:8080")
        self.assertEqual(cfg["llm_model"], "gemma3")
        # key stored via db/crypto (decrypt round-trips; plaintext
        # passthrough only when no master key is configured)
        conn = _t.tenant_connect(r["tenant_id"])
        try:
            self.assertEqual(crypto.decrypt(conn, cfg["llm_api_key"]),
                             "sk-test-123")
        finally:
            conn.close()

    def test_bootstrap_llm_local_no_key(self):
        r = setup.bootstrap(f"llmnk-{uuid.uuid4().hex[:8]}@example.dev",
                            "correct-horse-battery",
                            llm_url="https://api.openai.com",
                            llm_model="gpt-4.1-mini")
        cfg = self._tenant_config(r["tenant_id"])
        self.assertEqual(cfg["llm_model"], "gpt-4.1-mini")
        self.assertNotIn("llm_api_key", cfg)

    def test_bootstrap_skipping_llm_stores_nothing(self):
        r = setup.bootstrap(f"nollm-{uuid.uuid4().hex[:8]}@example.dev",
                            "correct-horse-battery")
        self.assertFalse(r["llm_saved"])
        cfg = self._tenant_config(r["tenant_id"])
        for k in ("llm_url", "llm_model", "llm_api_key"):
            self.assertNotIn(k, cfg)

    def test_bootstrap_llm_validation_before_user_created(self):
        cases = [dict(llm_url="http://x.test"),               # URL sans model
                 dict(llm_model="gemma3"),                    # model sans URL
                 dict(llm_api_key="sk-x"),                    # key alone
                 dict(llm_url="ftp://x.test", llm_model="m"),  # bad scheme
                 dict(llm_url="not a url", llm_model="m")]    # unparseable
        for kw in cases:
            email = f"badllm-{uuid.uuid4().hex[:8]}@example.dev"
            with self.assertRaises(ValueError, msg=kw):
                setup.bootstrap(email, "correct-horse-battery", **kw)
            admin = tenancy.admin_connect()
            try:
                self.assertIsNone(admin.execute(
                    "SELECT 1 FROM users WHERE email=%s", (email,)).fetchone())
            finally:
                admin.close()

    def test_bootstrap_validation(self):
        with self.assertRaises(ValueError):
            setup.bootstrap("not-an-email", "correct-horse-battery")
        with self.assertRaises(ValueError):
            setup.bootstrap(f"ok-{uuid.uuid4().hex[:8]}@example.dev", "short")

    def test_instance_has_users_gate(self):
        # the shared test DB has users by now (created above) — the gate
        # that hides /setup must read True
        conn = make_db()
        try:
            self.assertTrue(setup.instance_has_users(conn) in (True, False))
        finally:
            conn.close()
        import psycopg
        from psycopg.rows import dict_row
        c = psycopg.connect(tenancy.ADMIN_DSN, row_factory=dict_row,
                            autocommit=True)
        try:
            self.assertTrue(setup.instance_has_users(c))
        finally:
            c.close()


class LlmSettingsApiTests(unittest.TestCase):
    """/api/settings additively surfaces llm_url / llm_model /
    llm_api_key(_set) — wizard parity for the SPA Settings page to render
    later. The key is write-only: stored encrypted, never echoed."""

    @classmethod
    def setUpClass(cls):
        import os

        from fastapi.testclient import TestClient
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"llmapi-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def test_llm_settings_lifecycle(self):
        # set all three
        r = self.client.post("/api/settings", json={
            "llm_url": "http://cfg.test:9090", "llm_model": "gemma3",
            "llm_api_key": "sk-secret-999"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["llm_url"], "http://cfg.test:9090")
        self.assertEqual(r.json()["llm_model"], "gemma3")
        self.assertTrue(r.json()["llm_api_key_set"])
        self.assertNotIn("sk-secret-999", r.text)        # never echoed
        got = self.client.get("/api/settings")
        self.assertNotIn("sk-secret-999", got.text)      # not on GET either
        self.assertNotIn("llm_api_key", got.json())      # presence flag only
        self.assertTrue(got.json()["llm_api_key_set"])
        # model-only update keeps the stored endpoint
        r = self.client.post("/api/settings", json={"llm_model": "gemma3:27b"})
        self.assertEqual(r.json()["llm_model"], "gemma3:27b")
        self.assertEqual(r.json()["llm_url"], "http://cfg.test:9090")
        # bad inputs 400 without clobbering
        for body in ({"llm_url": "ftp://x.test", "llm_model": "m"},
                     {"llm_url": "http://x.test", "llm_model": ""}):
            self.assertEqual(
                self.client.post("/api/settings", json=body).status_code, 400)
        # empty llm_url clears the whole group, key included
        r = self.client.post("/api/settings", json={"llm_url": ""})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.json()["llm_url"])
        self.assertIsNone(r.json()["llm_model"])
        self.assertFalse(r.json()["llm_api_key_set"])
        # model/key without any endpoint → 400
        self.assertEqual(self.client.post(
            "/api/settings", json={"llm_model": "m"}).status_code, 400)
        self.assertEqual(self.client.post(
            "/api/settings", json={"llm_api_key": "sk-x"}).status_code, 400)

    def test_llm_effective_reports_the_backend_actually_in_use(self):
        """Empty tenant fields must not read as "no AI" while an env-provided
        backend (the bundled Ollama) is doing the work — and hosted must
        never reveal the operator's env backend to a tenant. Per-role: each
        of categorize / assistant / vision reports its own resolution."""
        import os
        from unittest.mock import patch
        # a tenant endpoint wins, classified local/cloud by its host
        self.client.post("/api/settings", json={
            "llm_url": "http://localhost:11434/v1", "llm_model": "qwen3:4b"})
        eff = self.client.get("/api/settings").json()["llm_effective"]
        self.assertEqual(eff["categorize"],
                         {"source": "tenant",
                          "url": "http://localhost:11434/v1",
                          "model": "qwen3:4b", "local": True})
        self.assertEqual(eff["categorize"], eff["assistant"])
        self.client.post("/api/settings", json={
            "llm_url": "https://api.openai.com/v1",
            "llm_model": "gpt-4o-mini"})
        eff = self.client.get("/api/settings").json()["llm_effective"]
        self.assertFalse(eff["assistant"]["local"])
        # no tenant endpoint → the env/bundled fallback is what answers;
        # a bare compose service name counts as local
        self.client.post("/api/settings", json={"llm_url": ""})
        with patch.dict(os.environ, {"OIKONOME_LLM_URL": "http://ollama:11434",
                                     "OIKONOME_LLM_MODEL": "qwen3:4b"}):
            j = self.client.get("/api/settings").json()
            eff = j["llm_effective"]["categorize"]
            self.assertEqual((eff["source"], eff["model"], eff["local"]),
                             ("bundled", "qwen3:4b", True))
            self.assertEqual(j["llm_bundled"]["model"], "qwen3:4b")
            with patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
                j = self.client.get("/api/settings").json()
                self.assertIsNone(j["llm_effective"]["categorize"])
                self.assertIsNone(j["llm_bundled"])
        with patch.dict(os.environ):
            os.environ.pop("OIKONOME_LLM_URL", None)
            self.assertIsNone(self.client.get(
                "/api/settings").json()["llm_effective"]["categorize"])


if __name__ == "__main__":
    unittest.main()


class SetupTokenGateTests(unittest.TestCase):
    """OIKONOME_SETUP_TOKEN gates the first-boot wizard: only the link the
    installer printed can reach the signup form; empty env keeps the old
    open behavior for pre-token installs."""

    def _req(self, query=""):
        from starlette.requests import Request
        return Request({"type": "http", "query_string": query.encode(),
                        "headers": []})

    def test_unset_env_keeps_wizard_open(self):
        from unittest import mock

        from oikonome.web.app import _setup_token_check
        with mock.patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("OIKONOME_SETUP_TOKEN", None)
            self.assertEqual(_setup_token_check(self._req()), "")

    def test_token_required_and_matches(self):
        from unittest import mock

        from fastapi import HTTPException

        from oikonome.web.app import _setup_token_check
        with mock.patch.dict("os.environ",
                             {"OIKONOME_SETUP_TOKEN": "s3cret"}):
            with self.assertRaises(HTTPException) as ctx:
                _setup_token_check(self._req())
            self.assertEqual(ctx.exception.status_code, 403)
            with self.assertRaises(HTTPException):
                _setup_token_check(self._req("token=wrong"))
            # query-param form (the printed link) and the form-field echo
            self.assertEqual(
                _setup_token_check(self._req("token=s3cret")), "s3cret")
            self.assertEqual(
                _setup_token_check(self._req(), form_token="s3cret"),
                "s3cret")

    def test_template_embeds_token_for_the_post(self):
        from oikonome.web import todayview
        html = todayview._env.get_template("setup.html").render(
            title="Setup", error=None, token="s3cret")
        self.assertIn('name="token" value="s3cret"', html)
        html = todayview._env.get_template("setup.html").render(
            title="Setup", error=None, token="")
        self.assertNotIn('name="token"', html)
