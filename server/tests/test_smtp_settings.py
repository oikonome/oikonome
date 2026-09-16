"""Web-configured SMTP: settings round-trip (write-only
password, clear-by-emptying-host), resolve precedence (tenant config
over env), and the test-send endpoint."""

import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import report

from .util import _ensure_db, make_db


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def test_env_fallback_and_config_precedence(self):
        with mock.patch.dict("os.environ",
                             {"OIKONOME_SMTP_HOST": "env.example"}):
            self.assertEqual(report.resolve_smtp(self.conn)["host"],
                             "env.example")
        from oikonome.db import crypto
        from oikonome.engine import budget
        cfg = budget.load_config(self.conn)
        cfg.update({"smtp_host": "cfg.example", "smtp_port": 2525,
                    "smtp_user": "u@cfg.example",
                    "smtp_password": crypto.encrypt(self.conn, "s3cret"),
                    "smtp_from": "money@cfg.example"})
        budget.save_config(self.conn, cfg)
        with mock.patch.dict("os.environ",
                             {"OIKONOME_SMTP_HOST": "env.example"}):
            s = report.resolve_smtp(self.conn)
        self.assertEqual((s["host"], s["port"], s["user"], s["password"],
                          s["sender"], s["configured"]),
                         ("cfg.example", 2525, "u@cfg.example", "s3cret",
                          "money@cfg.example", True))

    def test_unconfigured_everywhere(self):
        with mock.patch.dict("os.environ", {"OIKONOME_SMTP_HOST": ""}):
            self.assertFalse(report.resolve_smtp(self.conn)["configured"])


class SmtpSettingsApiTests(unittest.TestCase):
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
            "email": f"smtp-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def test_roundtrip_password_writeonly_and_clear(self):
        r = self.client.post("/api/settings", json={
            "smtp_host": "smtp.test", "smtp_port": "465",
            "smtp_user": "u@test", "smtp_password": "hunter2-hunter2",
            "smtp_from": "money@test"})
        self.assertEqual(r.status_code, 200)
        v = r.json()
        self.assertEqual((v["smtp_host"], v["smtp_port"], v["smtp_user"],
                          v["smtp_from"]),
                         ("smtp.test", 465, "u@test", "money@test"))
        self.assertTrue(v["smtp_password_set"])
        self.assertNotIn("smtp_password", v)     # write-only
        # stored encrypted, resolves back to plaintext
        conn = tenancy.tenant_connect(self.tid)
        try:
            s = report.resolve_smtp(conn)
        finally:
            conn.close()
        self.assertEqual(s["password"], "hunter2-hunter2")
        # empty password on re-save keeps the stored one
        r = self.client.post("/api/settings", json={
            "smtp_host": "smtp.test", "smtp_password": ""})
        self.assertTrue(r.json()["smtp_password_set"])
        # bad port rejected
        self.assertEqual(self.client.post("/api/settings", json={
            "smtp_host": "smtp.test", "smtp_port": "no"}).status_code, 400)
        # clearing the host clears the whole group
        r = self.client.post("/api/settings", json={"smtp_host": ""})
        v = r.json()
        self.assertIsNone(v["smtp_host"])
        self.assertFalse(v["smtp_password_set"])

    def test_smtp_test_endpoint(self):
        with mock.patch.dict("os.environ", {"OIKONOME_SMTP_HOST": ""}):
            r = self.client.post("/api/settings/smtp-test")
            self.assertEqual(r.status_code, 400)   # nothing configured
        self.client.post("/api/settings", json={
            "smtp_host": "smtp.test", "smtp_from": "money@test"})
        with mock.patch("oikonome.web.report.send") as send:
            r = self.client.post("/api/settings/smtp-test")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["via"], "smtp.test")
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["smtp"]["host"], "smtp.test")
        # leave the tenant clean for other suites
        self.client.post("/api/settings", json={"smtp_host": ""})


if __name__ == "__main__":
    unittest.main()


class ConnectionSettingsTests(unittest.TestCase):
    """Provider key presence in the settings view, the clear endpoints,
and the tenant llm_extra_body round-trip + backend use."""

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
            "email": f"conn-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def test_key_presence_and_clear(self):
        v = self.client.get("/api/settings").json()
        self.assertFalse(v["plaid_secret_set"])
        self.assertFalse(v["mx_api_key_set"])
        with mock.patch("oikonome.sync.mx.validate", return_value=True):
            self.client.post("/api/accounts/mx/keys", json={
                "client_id": "mx-cid", "api_key": "k", "env": "sandbox"})
        v = self.client.get("/api/settings").json()
        self.assertEqual(v["mx_client_id"], "mx-cid")
        self.assertTrue(v["mx_api_key_set"])
        self.assertNotIn("mx_api_key", v)          # write-only
        self.client.delete("/api/accounts/mx/keys")
        v = self.client.get("/api/settings").json()
        self.assertIsNone(v["mx_client_id"])
        self.assertFalse(v["mx_api_key_set"])

    def test_llm_extra_body_roundtrip_and_backend(self):
        r = self.client.post("/api/settings", json={
            "llm_url": "http://llm.test", "llm_model": "m",
            "llm_extra_body": '{"chat_template_kwargs": {"enable_thinking": false}}'})
        self.assertEqual(r.status_code, 200)
        # write-only, like the api_key beside it: presence, never the value
        self.assertEqual(r.json()["llm_extra_body"], "")
        self.assertTrue(r.json()["llm_extra_body_set"])
        # invalid JSON rejected
        self.assertEqual(self.client.post("/api/settings", json={
            "llm_url": "http://llm.test", "llm_model": "m",
            "llm_extra_body": "not json"}).status_code, 400)
        # the backend layer carries it into the request body
        from oikonome.engine import llm_categorize as llm
        conn = tenancy.tenant_connect(self.tid)
        try:
            be = llm._backend(conn)
        finally:
            conn.close()
        self.assertIn("enable_thinking", be["extra_body"])
        # clearing the url drops the whole group
        self.client.post("/api/settings", json={"llm_url": ""})
        v = self.client.get("/api/settings").json()
        self.assertFalse(v["llm_extra_body_set"])
