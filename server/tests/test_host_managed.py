"""On the hosted platform the operator owns mail and the aggregator, so a
tenant can neither set them nor read them back.

The aggregator half is enforced by `_no_byo_on_hosted`. The mail half needs
its own guard: a tenant relay that *beats* the operator's env in
`resolve_smtp` means the daily verdict can leave through a server the
operator does not run and cannot debug when it silently stops.

Self-host is BYO by design. Every hosted assertion below has a self-host
twin, because the bug this guards against is a fix that also disables the
self-hoster.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.web import report

from .util import _ensure_db, clear_totp_burn, make_db

SMTP_BODY = {"smtp_host": "relay.tenant.example", "smtp_port": "2525",
             "smtp_user": "u@tenant.example", "smtp_password": "hunter2-x",
             "smtp_from": "money@tenant.example"}


def _fresh_code(secret):
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)


class HostedMailResolveTests(unittest.TestCase):
    """resolve_smtp is the load-bearing half: the API can refuse new writes,
    but a row saved before the guard existed would otherwise keep winning."""

    def setUp(self):
        self.conn = make_db()
        from oikonome.db import crypto
        from oikonome.engine import budget
        cfg = budget.load_config(self.conn)
        cfg.update({"smtp_host": "relay.tenant.example", "smtp_port": 2525,
                    "smtp_user": "u@tenant.example",
                    "smtp_password": crypto.encrypt(self.conn, "s3cret"),
                    "smtp_from": "money@tenant.example"})
        budget.save_config(self.conn, cfg)

    def tearDown(self):
        self.conn.close()

    def test_hosted_ignores_a_tenant_relay_saved_earlier(self):
        with mock.patch.dict(os.environ,
                             {"OIKONOME_HOSTED": "1",
                              "OIKONOME_SMTP_HOST": "postmark.example",
                              "OIKONOME_SMTP_FROM": "no-reply@oikonome.com"}):
            s = report.resolve_smtp(self.conn)
        self.assertEqual(s["host"], "postmark.example")
        self.assertEqual(s["sender"], "no-reply@oikonome.com")
        self.assertTrue(s["configured"])

    def test_self_host_still_prefers_the_tenant_relay(self):
        env = dict(os.environ)
        env.pop("OIKONOME_HOSTED", None)
        env["OIKONOME_SMTP_HOST"] = "postmark.example"
        with mock.patch.dict(os.environ, env, clear=True):
            s = report.resolve_smtp(self.conn)
        self.assertEqual((s["host"], s["port"]), ("relay.tenant.example", 2525))
        self.assertEqual(s["password"], "s3cret")


class HostedSettingsApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"hostmail-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        # Hosted gates every write behind an enrolled second factor, and
        # that check runs BEFORE the guard under test. Without enrolling,
        # each assertion below would pass on the wrong 403 and prove
        # nothing.
        from oikonome.auth import totp as totp_mod
        secret = totp_mod.new_secret()
        r = cls.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _fresh_code(secret),
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text

    def test_hosted_refuses_the_smtp_group(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            r = self.client.post("/api/settings", json=SMTP_BODY)
        self.assertEqual(r.status_code, 403)
        self.assertIn("mail server sends your email", r.json()["detail"])

    def test_hosted_refuses_even_one_smtp_key(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            r = self.client.post("/api/settings", json={"smtp_host": "x.test"})
        self.assertEqual(r.status_code, 403)

    def test_hosted_leaves_the_rest_of_settings_writable(self):
        """The guard is scoped to the smtp_* group — a settings write that
        does not touch mail must still go through, or every unrelated save
        on hosted breaks."""
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            r = self.client.post("/api/settings", json={"theme": "dark"})
        self.assertEqual(r.status_code, 200)

    def test_hosted_withholds_what_it_will_not_accept(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            v = self.client.get("/api/settings").json()
        for k in ("smtp_host", "smtp_port", "smtp_user", "smtp_from",
                  "smtp_starttls", "smtp_password_set",
                  "plaid_client_id", "plaid_env", "plaid_secret_set",
                  "mx_client_id", "mx_env"):
            self.assertNotIn(k, v, f"{k} is the host's, not the tenant's")
        # ...while what the tenant does own is untouched
        self.assertIn("email_recipients", v)
        self.assertIn("email_schedule", v)
        self.assertIn("llm_model", v)

    def test_self_host_keeps_both_halves(self):
        env = dict(os.environ)
        env.pop("OIKONOME_HOSTED", None)
        with mock.patch.dict(os.environ, env, clear=True):
            r = self.client.post("/api/settings", json=SMTP_BODY)
            self.assertEqual(r.status_code, 200)
            v = self.client.get("/api/settings").json()
        self.assertEqual(v["smtp_host"], "relay.tenant.example")
        self.assertTrue(v["smtp_password_set"])
        self.assertNotIn("smtp_password", v)      # write-only, still

    def test_hosted_still_refuses_byo_aggregator_keys(self):
        """The aggregator-key guard, pinned here too: it is the other half
        of the same policy, and this file is where someone will come
        looking."""
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            r = self.client.post("/api/accounts/plaid/keys",
                                 json={"client_id": "abc", "secret": "def"})
        self.assertEqual(r.status_code, 403)


if __name__ == "__main__":
    unittest.main()
