"""Several AI backends live at once, routed per task: llm_backends is a
named list, llm_roles maps categorize / assistant / vision onto an entry
(or "bundled", the operator's env backend). Guarantees under test:

- routing picks the named backend per role; unrouted roles keep the legacy
  resolution (tenant single endpoint, else env);
- per-backend api keys are write-only: encrypted at rest, preserved when an
  update omits them, cleared by api_key:"", never echoed;
- the legacy single endpoint folds into the list as id "legacy" WITH its
  stored key, and the legacy keys retire in the same save;
- roles must name a real backend ("bundled" only where a bundled backend
  exists), and deleting a backend unroutes anything pointing at it;
- the vision role uses the entry's own vision_model, never another
  backend's.
"""

import os
import unittest
import uuid
from unittest.mock import patch

from oikonome.engine import llm_categorize



def _tenant_conn_of(client):
    """The TestClient signs up a tenant; tests that call the engine
    directly need a connection scoped to that same tenant."""
    from oikonome.db import tenancy
    from oikonome.web.app import app  # noqa: F401  (app import wires DB env)
    r = client.get("/api/me")
    return tenancy.tenant_connect(r.json()["tenant_id"])


class BackendRoutingApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        os.environ["OIKONOME_DEV"] = "1"
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"llmroute-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def _save(self, body, ok=True):
        r = self.client.post("/api/settings", json=body)
        if ok:
            self.assertEqual(r.status_code, 200, r.text)
        return r

    def test_backends_and_roles_lifecycle(self):
        j = self._save({"llm_backends": [
            {"id": "loc", "name": "Ollama box", "url": "http://localhost:11434/v1",
             "model": "qwen3:4b", "vision_model": "qwen2.5vl:7b"},
            {"id": "cloud", "name": "OpenAI", "url": "https://api.openai.com/v1",
             "model": "gpt-4o-mini", "api_key": "sk-cloud-secret"},
        ], "llm_roles": {"assistant": "cloud", "categorize": "loc",
                         "vision": "loc"}}).json()
        self.assertEqual([b["id"] for b in j["llm_backends"]],
                         ["loc", "cloud"])
        self.assertTrue(j["llm_backends"][1]["api_key_set"])
        self.assertFalse(j["llm_backends"][0]["api_key_set"])
        self.assertNotIn("sk-cloud-secret", self.client.get(
            "/api/settings").text)
        self.assertEqual(j["llm_roles"]["assistant"], "cloud")
        eff = j["llm_effective"]
        self.assertEqual(eff["assistant"]["model"], "gpt-4o-mini")
        self.assertFalse(eff["assistant"]["local"])
        self.assertEqual(eff["categorize"]["model"], "qwen3:4b")
        # the vision role reports the entry's own vision model
        self.assertEqual(eff["vision"]["model"], "qwen2.5vl:7b")

        # the resolver routes the same way, and decrypts the stored key
        conn = _tenant_conn_of(self.client)
        try:
            be = llm_categorize._backend(conn, role="assistant")
            self.assertEqual((be["url"], be["model"], be["api_key"]),
                             ("https://api.openai.com/v1", "gpt-4o-mini",
                              "sk-cloud-secret"))
            self.assertEqual(
                llm_categorize._backend(conn, role="vision")["vision_model"],
                "qwen2.5vl:7b")
        finally:
            conn.close()

        # update that omits api_key keeps the stored key; "" clears it
        j = self._save({"llm_backends": [
            {"id": "loc", "name": "Ollama box", "url": "http://localhost:11434/v1",
             "model": "qwen3:8b"},
            {"id": "cloud", "name": "OpenAI", "url": "https://api.openai.com/v1",
             "model": "gpt-4o-mini"},
        ]}).json()
        self.assertTrue(j["llm_backends"][1]["api_key_set"])
        j = self._save({"llm_backends": [
            {"id": "cloud", "name": "OpenAI", "url": "https://api.openai.com/v1",
             "model": "gpt-4o-mini", "api_key": ""},
        ]}).json()
        self.assertFalse(j["llm_backends"][0]["api_key_set"])
        # deleting "loc" unrouted the roles that pointed at it
        self.assertNotIn("categorize", j["llm_roles"])
        self.assertNotIn("vision", j["llm_roles"])
        self.assertEqual(j["llm_roles"].get("assistant"), "cloud")

    def test_roles_must_name_a_real_backend(self):
        r = self._save({"llm_roles": {"assistant": "nope"}}, ok=False)
        self.assertEqual(r.status_code, 400)
        r = self._save({"llm_roles": {"pets": "x"}}, ok=False)
        self.assertEqual(r.status_code, 400)
        # "bundled" only exists where an env backend does (none in tests)
        with patch.dict(os.environ):
            os.environ.pop("OIKONOME_LLM_URL", None)
            r = self._save({"llm_roles": {"assistant": "bundled"}}, ok=False)
            self.assertEqual(r.status_code, 400)
        with patch.dict(os.environ,
                        {"OIKONOME_LLM_URL": "http://ollama:11434"}):
            self._save({"llm_roles": {"assistant": "bundled"}})
            self._save({"llm_roles": {}})            # unroute again

    def test_backend_validation(self):
        for bad in ([{"id": "x", "name": "n", "model": "m"}],       # no url
                    [{"id": "x", "url": "http://h.test", "name": "n"}],  # no model
                    [{"id": "bundled", "url": "http://h.test",
                      "model": "m"}],                               # reserved
                    [{"id": "d", "url": "http://h.test", "model": "m"},
                     {"id": "d", "url": "http://h2.test", "model": "m"}],
                    [{"id": "x", "url": "http://h.test", "model": "m",
                      "extra_body": "not json"}]):
            r = self._save({"llm_backends": bad}, ok=False)
            self.assertEqual(r.status_code, 400, str(bad))

    def test_extra_body_must_be_a_json_object(self):
        """extra_body is merged into the request body with dict.update at
        call time, so JSON that parses to anything but an object (42,
        "hi", null, [1,2]) would pass a loads-only check and then
        crash the call with an uncaught TypeError — a 500 on every
        upload routed through that backend."""
        for bad in ("42", '"hi"', "null", "[1, 2]"):
            r = self._save({"llm_backends": [
                {"id": "eb", "url": "http://localhost:11434/v1",
                 "model": "m", "extra_body": bad}]}, ok=False)
            self.assertEqual(r.status_code, 400, bad)
            r = self._save({"llm_url": "http://localhost:11434/v1",
                            "llm_model": "m", "llm_extra_body": bad},
                           ok=False)
            self.assertEqual(r.status_code, 400, bad)
        # a real object still saves on both paths
        self._save({"llm_backends": [
            {"id": "eb", "url": "http://localhost:11434/v1", "model": "m",
             "extra_body": '{"a": 1}'}]})
        self._save({"llm_url": "http://localhost:11434/v1",
                    "llm_model": "m", "llm_extra_body": '{"a": 1}'})
        self._save({"llm_url": "", "llm_backends": []})     # clean up

    def test_extra_body_is_masked_like_the_api_key_beside_it(self):
        """extra_body is arbitrary JSON merged into every request to the
        backend, so it can carry credentials (auth-in-body vendors, org
        tokens) — GET must report presence only, exactly like api_key.
        And because clients round-trip what GET gave them, the masked ""
        (or an omitted key) must KEEP the stored value; only an explicit
        JSON null clears it — otherwise every save from a client that
        didn't retype the JSON would silently wipe it."""
        secret = '{"organization": "org-secret-77"}'
        entry = {"id": "ebm", "url": "http://localhost:11434/v1",
                 "model": "m"}
        j = self._save({"llm_backends": [
            {**entry, "extra_body": secret}]}).json()
        b = j["llm_backends"][0]
        self.assertEqual(b["extra_body"], "")
        self.assertTrue(b["extra_body_set"])
        self.assertNotIn("org-secret-77",
                         self.client.get("/api/settings").text)
        # the stored value is intact for the execution path
        conn = _tenant_conn_of(self.client)
        try:
            from oikonome.engine import budget
            cfg = budget.load_config(conn)
            self.assertEqual(cfg["llm_backends"][0]["extra_body"], secret)
        finally:
            conn.close()
        # round trip of the masked "" keeps it; omitting the key keeps it
        j = self._save({"llm_backends": [
            {**entry, "extra_body": ""}]}).json()
        self.assertTrue(j["llm_backends"][0]["extra_body_set"])
        j = self._save({"llm_backends": [entry]}).json()
        self.assertTrue(j["llm_backends"][0]["extra_body_set"])
        # explicit null is the deliberate clear
        j = self._save({"llm_backends": [
            {**entry, "extra_body": None}]}).json()
        self.assertFalse(j["llm_backends"][0]["extra_body_set"])
        self._save({"llm_backends": []})                    # clean up

    def test_extra_body_is_encrypted_at_rest_like_the_api_key(self):
        """Masking (GET) and scrubbing (exports) are two thirds of the
        extra_body-carries-a-credential story — at rest it must be an
        envelope token like api_key beside it, so a DB dump never hands
        it out in the clear. Untagged legacy rows still pass through
        decrypt unchanged, so pre-existing plaintext values keep working."""
        from cryptography.fernet import Fernet
        from fastapi.testclient import TestClient

        from oikonome.db import crypto as _crypto
        prev = os.environ.get("OIKONOME_MASTER_KEY")
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        try:
            from oikonome.web.app import app
            client = TestClient(app)
            client.post("/api/signup", data={
                "email": f"llmenc-{uuid.uuid4().hex[:8]}@example.dev",
                "password": "correct-horse-battery"})
            secret = '{"organization": "org-secret-77"}'
            r = client.post("/api/settings", json={
                "llm_backends": [
                    {"id": "enc", "url": "http://localhost:11434/v1",
                     "model": "m", "extra_body": secret}],
                "llm_roles": {"categorize": "enc"}})
            self.assertEqual(r.status_code, 200, r.text)
            conn = _tenant_conn_of(client)
            try:
                from oikonome.engine import budget
                stored = budget.load_config(
                    conn)["llm_backends"][0]["extra_body"]
                self.assertTrue(stored.startswith(_crypto.PREFIX), stored)
                self.assertNotIn("org-secret-77", stored)
                # the execution path gets the plaintext back
                be = llm_categorize._backend(conn, role="categorize")
                self.assertEqual(be["extra_body"], secret)
            finally:
                conn.close()
        finally:
            if prev is None:
                os.environ.pop("OIKONOME_MASTER_KEY", None)
            else:
                os.environ["OIKONOME_MASTER_KEY"] = prev

    def test_legacy_extra_body_is_masked_and_kept_on_a_round_trip(self):
        """The single-endpoint llm_extra_body is the same credential as the
        per-backend one and gets the same contract: GET reports presence
        only, so a client that echoes back the masked "" must KEEP the
        stored value and only an explicit JSON null clears it. Without the
        keep rule, masking alone would wipe the value the first time any
        client saved the Settings page."""
        secret = '{"organization": "org-legacy-77"}'
        legacy = {"llm_url": "http://cfg-mask.test:9090", "llm_model": "m"}
        j = self._save({**legacy, "llm_extra_body": secret}).json()
        self.assertEqual(j["llm_extra_body"], "")
        self.assertTrue(j["llm_extra_body_set"])
        self.assertNotIn("org-legacy-77", self.client.get("/api/settings").text)
        # the round trip of the masked "" keeps it; omitting it keeps it
        j = self._save({**legacy, "llm_extra_body": ""}).json()
        self.assertTrue(j["llm_extra_body_set"])
        j = self._save(legacy).json()
        self.assertTrue(j["llm_extra_body_set"])
        # ...and the execution path still has the real value
        conn = _tenant_conn_of(self.client)
        try:
            self.assertEqual(
                llm_categorize._backend(conn, role="categorize")["extra_body"],
                secret)
        finally:
            conn.close()
        # explicit null is the deliberate clear
        j = self._save({**legacy, "llm_extra_body": None}).json()
        self.assertFalse(j["llm_extra_body_set"])
        self._save({"llm_url": ""})                         # clean up

    def test_taxdocs_consent_saves_without_the_legacy_url_group(self):
        """The tax-document parser refuses a remote model until this
        consent is on, and names the setting in the error it raises. Both
        clients send it with the AI card's payload and never with the
        legacy llm_url group, so a write path living inside that group left
        the checkbox unwritable — an instruction pointing at a knob with no
        write path is a dead end, not a fix."""
        self._save({"llm_url": "", "llm_backends": [], "llm_roles": {}})
        remote = {"llm_backends": [
            {"id": "cloud", "name": "Cloud", "url": "https://api.example.test",
             "model": "m"}], "llm_roles": {"vision": "cloud"}}
        r = self._save({**remote, "taxdocs_allow_remote_llm": "1"})
        self.assertEqual(r.json().get("taxdocs_allow_remote_llm"), "1")
        self.assertEqual(self.client.get("/api/settings").json()
                         .get("taxdocs_allow_remote_llm"), "1")
        # ...and the gate that names the setting now opens
        from oikonome.engine import taxdocs
        conn = _tenant_conn_of(self.client)
        try:
            taxdocs._require_offbox_consent(conn, "W-2")   # no raise
            # unchecking it withdraws consent again
            r = self._save({**remote, "taxdocs_allow_remote_llm": ""})
            self.assertFalse(r.json().get("taxdocs_allow_remote_llm"))
            with self.assertRaises(ValueError):
                taxdocs._require_offbox_consent(conn, "W-2")
        finally:
            conn.close()
        self._save({"llm_backends": [], "llm_roles": {}})    # clean up

    def test_legacy_extra_body_is_encrypted_at_rest_like_the_api_key(self):
        """The single-endpoint config is the one most self-host installs
        use, and its extra_body carries the same auth-in-body credentials
        as the per-backend twin — so it owes the same envelope at rest as
        the llm_api_key stored beside it. A DB dump must not hand it out in
        the clear."""
        from cryptography.fernet import Fernet
        from fastapi.testclient import TestClient

        from oikonome.db import crypto as _crypto
        prev = os.environ.get("OIKONOME_MASTER_KEY")
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        try:
            from oikonome.web.app import app
            client = TestClient(app)
            client.post("/api/signup", data={
                "email": f"llmlegenc-{uuid.uuid4().hex[:8]}@example.dev",
                "password": "correct-horse-battery"})
            secret = '{"organization": "org-legacy-77"}'
            r = client.post("/api/settings", json={
                "llm_url": "http://cfg-enc.test:9090", "llm_model": "m",
                "llm_extra_body": secret})
            self.assertEqual(r.status_code, 200, r.text)
            conn = _tenant_conn_of(client)
            try:
                from oikonome.engine import budget
                stored = budget.load_config(conn)["llm_extra_body"]
                self.assertTrue(stored.startswith(_crypto.PREFIX), stored)
                self.assertNotIn("org-legacy-77", stored)
                # the execution path gets the plaintext back
                self.assertEqual(
                    llm_categorize._backend(conn)["extra_body"], secret)

                # a value written before this was encrypted carries no tag;
                # it must keep working, and must be encrypted as it folds
                # into the backend list rather than riding forward in the
                # clear forever
                cfg = budget.load_config(conn)
                cfg["llm_extra_body"] = secret          # pre-encryption row
                budget.save_config(conn, cfg)
                self.assertEqual(
                    llm_categorize._backend(conn)["extra_body"], secret)
            finally:
                conn.close()
            r = client.post("/api/settings", json={"llm_backends": [
                {"id": "legacy", "name": "Custom endpoint",
                 "url": "http://cfg-enc.test:9090", "model": "m"}],
                "llm_roles": {"categorize": "legacy"}})
            self.assertEqual(r.status_code, 200, r.text)
            conn = _tenant_conn_of(client)
            try:
                from oikonome.engine import budget
                folded = budget.load_config(
                    conn)["llm_backends"][0]["extra_body"]
                self.assertTrue(folded.startswith(_crypto.PREFIX), folded)
                self.assertEqual(
                    llm_categorize._backend(conn, role="categorize"
                                            )["extra_body"], secret)
            finally:
                conn.close()
        finally:
            if prev is None:
                os.environ.pop("OIKONOME_MASTER_KEY", None)
            else:
                os.environ["OIKONOME_MASTER_KEY"] = prev

    def test_multi_backend_save_unwraps_the_tenant_key_once(self):
        """Each crypto.encrypt round-trips the DB for the tenant key, and
        the backends save runs inside the settings row lock — three
        backends with keys must not mean three key fetches under the
        lock; the save resolves the key once for the whole list."""
        from oikonome.db import crypto as _crypto
        calls = []
        real = _crypto._tenant_fernet

        def counting(conn):
            calls.append(1)
            return real(conn)
        with patch.object(_crypto, "_tenant_fernet", side_effect=counting):
            self._save({"llm_backends": [
                {"id": f"k{i}", "url": "http://localhost:11434/v1",
                 "model": "m", "api_key": f"sk-{i}"} for i in range(3)]})
        self.assertEqual(len(calls), 1,
                         "the tenant key must be resolved once per save, "
                         f"not per backend (got {len(calls)} fetches)")
        # the keys are individually stored and still round-trip
        j = self.client.get("/api/settings").json()
        self.assertTrue(all(b["api_key_set"] for b in j["llm_backends"]))
        self._save({"llm_backends": []})                    # clean up

    def test_vision_model_saves_without_the_legacy_url_group(self):
        """A bundled-Ollama install has no llm_url, and the AI settings
        card sends llm_vision_model on its own — a write path living only
        inside the legacy llm_url block would leave such an install no way
        to set a vision model at all."""
        self._save({"llm_url": "", "llm_backends": [], "llm_roles": {}})
        r = self._save({"llm_vision_model": "qwen2.5vl:3b"})
        self.assertEqual(r.json().get("llm_vision_model"), "qwen2.5vl:3b")
        self.assertEqual(self.client.get("/api/settings").json()
                         .get("llm_vision_model"), "qwen2.5vl:3b")
        # ...and the bundled/env path picks it up for the vision role
        conn = _tenant_conn_of(self.client)
        try:
            with patch.dict(os.environ,
                            {"OIKONOME_LLM_URL": "http://ollama:11434",
                             "OIKONOME_LLM_MODEL": "qwen2.5:1.5b"}):
                be = llm_categorize._backend(conn, role="vision")
            self.assertEqual(be["vision_model"], "qwen2.5vl:3b")
        finally:
            conn.close()
        # empty string clears it, still without the legacy group
        r = self._save({"llm_vision_model": ""})
        self.assertFalse(r.json().get("llm_vision_model"))

    def test_display_resolution_matches_execution_resolution(self):
        """llm_effective (the Settings 'what uses what' card) and
        _backend (the execution path) share one resolver — for every
        role the displayed url/model must be what a call would hit."""
        self._save({"llm_url": "", "llm_backends": [
            {"id": "one", "name": "One", "url": "http://localhost:11434/v1",
             "model": "m1", "vision_model": "v1"},
            {"id": "two", "name": "Two", "url": "http://127.0.0.1:8080/v1",
             "model": "m2"},
        ], "llm_roles": {"categorize": "one", "vision": "two"}})
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")}
        conn = _tenant_conn_of(self.client)
        try:
            with patch.dict(os.environ, env, clear=True):
                eff = self.client.get("/api/settings").json()["llm_effective"]
                for role in llm_categorize.ROLES:
                    be = llm_categorize._backend(conn, role=role)
                    view = eff[role]
                    if not be["url"]:
                        self.assertIsNone(view, role)
                        continue
                    self.assertEqual(view["url"], be["url"], role)
                    expect = (be["vision_model"] or be["model"]
                              if role == "vision" else be["model"])
                    self.assertEqual(view["model"], expect, role)
        finally:
            conn.close()
        self._save({"llm_backends": [], "llm_roles": {}})

    def test_legacy_endpoint_folds_in_with_its_key(self):
        self._save({"llm_url": "http://cfg.test:9090", "llm_model": "gemma3",
                    "llm_api_key": "sk-legacy-secret"})
        # the SPA writes the list including the legacy entry (no key —
        # it never saw one) and the stored key must ride along
        j = self._save({"llm_backends": [
            {"id": "legacy", "name": "Custom endpoint",
             "url": "http://cfg.test:9090", "model": "gemma3"},
        ], "llm_roles": {"categorize": "legacy"}}).json()
        self.assertTrue(j["llm_backends"][0]["api_key_set"])
        self.assertIsNone(j["llm_url"])          # legacy keys retired
        conn = _tenant_conn_of(self.client)
        try:
            be = llm_categorize._backend(conn, role="categorize")
            self.assertEqual(be["api_key"], "sk-legacy-secret")
        finally:
            conn.close()
        self._save({"llm_backends": [], "llm_roles": {}})

    def test_unrouted_roles_keep_legacy_then_env_resolution(self):
        self._save({"llm_backends": [
            {"id": "a", "name": "A", "url": "http://a.test", "model": "m-a"},
        ], "llm_roles": {"assistant": "a"}})
        conn = _tenant_conn_of(self.client)
        try:
            # no legacy endpoint, no env → unrouted role resolves to nothing
            with patch.dict(os.environ):
                os.environ.pop("OIKONOME_LLM_URL", None)
                self.assertEqual(
                    llm_categorize._backend(conn, role="categorize")["url"], "")
                self.assertEqual(
                    llm_categorize._backend(conn, role="assistant")["url"],
                    "http://a.test")
            with patch.dict(os.environ,
                            {"OIKONOME_LLM_URL": "http://ollama:11434",
                             "OIKONOME_LLM_MODEL": "envm"}):
                be = llm_categorize._backend(conn, role="categorize")
                self.assertEqual((be["url"], be["model"], be["source"]),
                                 ("http://ollama:11434", "envm", "env"))
        finally:
            conn.close()
        self._save({"llm_backends": [], "llm_roles": {}})


if __name__ == "__main__":
    unittest.main()
