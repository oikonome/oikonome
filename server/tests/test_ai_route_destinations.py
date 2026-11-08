"""Where each AI task's data goes, and a "bundled" route with no bundled
backend behind it.

- A task routed to "bundled" lands on the operator's env backend only
  where one exists for tenants (an env endpoint, not hosted). Once that
  backend is gone (`llm off`, a restore, a mirror carrying the route in),
  the route reads as unset everywhere: the resolver falls back to the
  household's own endpoint, the settings view stops reporting the pin,
  and a save that carries the stale route back is accepted with the route
  dropped. A NEW pin to a bundled backend that does not exist is still
  refused.
- GET /api/assistant names, per task, whether its data stays on this
  server or its network, goes to a remote AI service, or goes nowhere.
  It is the one answer every "stays on this instance" line reads.
"""

import os
import unittest
import uuid
from unittest.mock import patch

from oikonome.engine import llm_categorize

from .util import _ensure_db


def _tenant_conn_of(client):
    from oikonome.db import tenancy
    from oikonome.web.app import app  # noqa: F401  (app import wires DB env)
    r = client.get("/api/me")
    return tenancy.tenant_connect(r.json()["tenant_id"])


def _env_without_llm(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("OIKONOME_LLM") and k != "OIKONOME_HOSTED"}
    env.update(extra)
    return env


class StaleBundledRouteResolves(unittest.TestCase):
    CFG = {"llm_url": "http://10.0.0.5:11434/v1", "llm_model": "m",
           "llm_roles": {"vision": "bundled"}}

    def test_no_env_backend_falls_back_to_the_household_endpoint(self):
        with patch.dict(os.environ, _env_without_llm(), clear=True):
            self.assertEqual(llm_categorize.resolve_role(self.CFG, "vision"),
                             ("legacy", None))

    def test_hosted_never_honours_a_bundled_route(self):
        with patch.dict(os.environ, _env_without_llm(
                OIKONOME_LLM_URL="http://ollama:11434",
                OIKONOME_HOSTED="1"), clear=True):
            self.assertEqual(llm_categorize.resolve_role(self.CFG, "vision"),
                             ("legacy", None))

    def test_a_live_bundled_backend_still_answers_its_route(self):
        with patch.dict(os.environ, _env_without_llm(
                OIKONOME_LLM_URL="http://ollama:11434"), clear=True):
            self.assertEqual(llm_categorize.resolve_role(self.CFG, "vision"),
                             ("env", None))


class StaleBundledRouteApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"airoute-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.client.post("/api/auth/elevate",
                        json={"password": "correct-horse-battery"})

    def _save(self, body, code=200):
        r = self.client.post("/api/settings", json=body)
        self.assertEqual(r.status_code, code, r.text)
        return r

    def tearDown(self):
        with patch.dict(os.environ, _env_without_llm(), clear=True):
            self._save({"llm_url": "", "llm_backends": [], "llm_roles": {}})

    def test_a_stale_bundled_route_reads_unset_and_saves_through(self):
        with patch.dict(os.environ, _env_without_llm(
                OIKONOME_LLM_URL="http://ollama:11434"), clear=True):
            self._save({"llm_url": "http://10.0.0.5:11434/v1",
                        "llm_model": "m"})
            self._save({"llm_roles": {"vision": "bundled"}})
        # the operator turns the bundled backend off
        with patch.dict(os.environ, _env_without_llm(), clear=True):
            got = self.client.get("/api/settings").json()
            self.assertNotIn("vision", got["llm_roles"])
            self.assertEqual(got["llm_effective"]["vision"]["source"],
                             "tenant")
            conn = _tenant_conn_of(self.client)
            try:
                self.assertEqual(
                    llm_categorize._backend(conn, role="vision")["url"],
                    "http://10.0.0.5:11434/v1")
            finally:
                conn.close()
            # an AI-card save carrying the stale route back is not refused
            j = self._save({"llm_roles": {"vision": "bundled"},
                            "taxdocs_allow_remote_llm": False}).json()
            self.assertNotIn("vision", j["llm_roles"])
            # a NEW pin to a backend that is not there still is
            self._save({"llm_roles": {"assistant": "bundled"}}, code=400)

    def test_assistant_status_names_where_each_task_goes(self):
        with patch.dict(os.environ, _env_without_llm(), clear=True):
            roles = self.client.get("/api/assistant").json()["roles"]
            self.assertEqual({r: v["where"] for r, v in roles.items()},
                             {r: "off" for r in llm_categorize.ROLES})
            self._save({"llm_backends": [
                {"id": "loc", "name": "Box", "url": "http://10.0.0.5:11434/v1",
                 "model": "m"},
                {"id": "far", "name": "Far", "url": "https://93.184.216.34/v1",
                 "model": "m"},
            ], "llm_roles": {"categorize": "loc", "vision": "far"}})
            roles = self.client.get("/api/assistant").json()["roles"]
            self.assertEqual(roles["categorize"], {"where": "local", "host": ""})
            self.assertEqual(roles["vision"],
                             {"where": "remote", "host": "93.184.216.34"})
            self.assertEqual(roles["assistant"]["where"], "off")
        # the operator's own remote backend is remote, its host never named
        with patch.dict(os.environ, _env_without_llm(
                OIKONOME_LLM_URL="https://93.184.216.34/v1"), clear=True):
            roles = self.client.get("/api/assistant").json()["roles"]
            self.assertEqual(roles["assistant"], {"where": "remote", "host": ""})


class DestinationsNeverTouchCredentials(unittest.TestCase):
    """Judging where a task's data goes needs only the endpoint URL, which
    is stored in the clear. The status read must neither decrypt a stored
    credential (one that cannot be decrypted — a restore onto another
    master key — would otherwise fail the whole read) nor load the config
    more than once."""

    def setUp(self):
        from .util import make_db, write_config
        self.conn = make_db()
        write_config(self.conn, llm_backends=[
            {"id": "loc", "name": "Box", "url": "http://10.0.0.5:11434/v1",
             "model": "m", "api_key": "undecryptable", "extra_body": "x"},
            {"id": "far", "name": "Far", "url": "https://93.184.216.34/v1",
             "model": "m", "api_key": "undecryptable"}],
            llm_roles={"categorize": "loc", "vision": "far",
                       "assistant": "far"})

    def tearDown(self):
        self.conn.close()

    def test_status_view_reads_config_once_and_decrypts_nothing(self):
        from oikonome.engine import assistant, budget
        real = budget.load_config
        loads = []

        def counting(*a, **k):
            loads.append(1)
            return real(*a, **k)

        with patch.dict(os.environ, _env_without_llm(), clear=True), \
                patch("oikonome.db.crypto.decrypt",
                      side_effect=RuntimeError("no master key")), \
                patch.object(budget, "load_config", side_effect=counting):
            got = assistant.status_view(self.conn)
        self.assertEqual(len(loads), 1)
        self.assertEqual(got["available"], True)
        self.assertEqual(got["local"], False)
        self.assertEqual(got["host"], "93.184.216.34")
        self.assertEqual(got["roles"]["categorize"],
                         {"where": "local", "host": ""})
        self.assertEqual(got["roles"]["vision"],
                         {"where": "remote", "host": "93.184.216.34"})

    def test_role_destinations_decrypt_nothing(self):
        with patch.dict(os.environ, _env_without_llm(), clear=True), \
                patch("oikonome.db.crypto.decrypt",
                      side_effect=RuntimeError("no master key")):
            roles = llm_categorize.role_destinations(self.conn)
        self.assertEqual(roles["assistant"]["where"], "remote")


if __name__ == "__main__":
    unittest.main()
