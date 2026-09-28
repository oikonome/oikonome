"""Repointing where the household's data or sign-in mail goes is a
step-up act.

A stolen session cookie is not the password. If a cookie alone could swap
the SMTP relay, the owner's next password-reset link would be handed to
the thief's server and the account taken over; if it could swap the AI
endpoint or its key, every transaction the categorize pass reads would go
to the thief; a new daily-mail recipient is a copy of the household's money
every morning. So each of those changes needs the elevation window (or,
from an older app build, the password in the body), exactly like adding a
webhook or changing the account email.

What must NOT prompt: the SPA re-posting the whole settings document with
those fields unchanged, and taking a destination away (clearing the relay,
removing a recipient) — neither sends anything anywhere new.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"
RELAY = "8.8.8.8"          # a literal public address: no DNS in the test
OTHER_RELAY = "8.8.4.4"
LLM = "http://8.8.8.8:11434/v1"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        os.environ.pop("OIKONOME_HOSTED", None)
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.c = TestClient(self.app)
        self.email = f"outb-{uuid.uuid4().hex[:10]}@example.dev"
        r = self.c.post("/api/signup",
                        data={"email": self.email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def _elevate(self):
        r = self.c.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def _unelevate(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE sessions SET elevated_at = NULL WHERE user_id = "
                "(SELECT id FROM users WHERE email = %s)", (self.email,))
        finally:
            admin.close()

    def _stored(self) -> dict:
        admin = tenancy.admin_connect()
        try:
            row = admin.execute(
                "SELECT s.config FROM tenant_settings s JOIN users u "
                "ON u.tenant_id = s.tenant_id WHERE u.email = %s",
                (self.email,)).fetchone()
        finally:
            admin.close()
        return dict(row["config"] or {}) if row else {}

    def _save(self, body):
        return self.c.post("/api/settings", json=body)

    def assertRefused(self, r):
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"]["error"], "elevation_required")


class SmtpRelayTests(_Base):

    def test_unelevated_session_cannot_point_mail_at_a_new_relay(self):
        """The reset link rides this relay: a cookie alone must not move
        it, and the refusal must save nothing else from the same body."""
        r = self._save({"smtp_host": RELAY, "smtp_port": 587,
                        "smtp_starttls": False, "food_monthly": 123})
        self.assertRefused(r)
        cfg = self._stored()
        self.assertNotIn("smtp_host", cfg)
        self.assertNotEqual(cfg.get("food_monthly"), 123)

    def test_each_smtp_field_is_part_of_the_destination(self):
        """Same host, someone else's login is someone else's Sent folder."""
        self._elevate()
        self.assertEqual(self._save({"smtp_host": RELAY, "smtp_user": "me"})
                         .status_code, 200)
        self._unelevate()
        for body in ({"smtp_host": OTHER_RELAY}, {"smtp_user": "thief"},
                     {"smtp_password": "hunter2"}, {"smtp_port": 25},
                     {"smtp_from": "x@example.dev"},
                     {"smtp_starttls": False}):
            with self.subTest(body=body):
                self.assertRefused(self._save({"smtp_host": RELAY, **body}))
        self.assertEqual(self._stored().get("smtp_user"), "me")

    def test_resaving_the_unchanged_relay_does_not_ask(self):
        self._elevate()
        self.assertEqual(self._save({"smtp_host": RELAY, "smtp_port": 587,
                                     "smtp_user": "me"}).status_code, 200)
        self._unelevate()
        r = self._save({"smtp_host": RELAY, "smtp_port": 587,
                        "smtp_user": "me", "food_monthly": 50})
        self.assertEqual(r.status_code, 200, r.text)

    def test_filling_in_the_default_port_and_starttls_does_not_ask(self):
        """A client that re-posts the defaults the sender already uses
        (587, STARTTLS on) for a relay stored without them repoints
        nothing."""
        self._elevate()
        self.assertEqual(self._save({"smtp_host": RELAY}).status_code, 200)
        self._unelevate()
        r = self._save({"smtp_host": RELAY, "smtp_port": 587,
                        "smtp_starttls": True})
        self.assertEqual(r.status_code, 200, r.text)

    def test_clearing_the_relay_does_not_ask(self):
        self._elevate()
        self._save({"smtp_host": RELAY})
        self._unelevate()
        r = self._save({"smtp_host": ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("smtp_host", self._stored())

    def test_elevated_session_repoints_the_relay(self):
        self._elevate()
        r = self._save({"smtp_host": RELAY})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._stored()["smtp_host"], RELAY)

    def test_older_app_build_proves_with_the_password_in_the_body(self):
        r = self._save({"smtp_host": RELAY, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        r = self._save({"smtp_host": OTHER_RELAY, "password": "wrong-one"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._stored()["smtp_host"], RELAY)


class AiEndpointTests(_Base):

    def test_unelevated_session_cannot_set_a_new_llm_endpoint(self):
        self.assertRefused(self._save({"llm_url": LLM, "llm_model": "m"}))
        self.assertNotIn("llm_url", self._stored())

    def test_unelevated_session_cannot_swap_the_llm_key(self):
        """On a proxy the key picks whose account logs the prompts."""
        self._elevate()
        self.assertEqual(self._save({"llm_url": LLM, "llm_model": "m",
                                     "llm_api_key": "sk-owner"})
                         .status_code, 200)
        self._unelevate()
        self.assertRefused(self._save({"llm_url": LLM, "llm_model": "m",
                                       "llm_api_key": "sk-thief"}))
        # the whole document re-posted, key left masked: no prompt
        r = self._save({"llm_url": LLM, "llm_model": "m"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_unelevated_session_cannot_add_a_backend_url(self):
        self.assertRefused(self._save({"llm_backends": [
            {"id": "b1", "name": "x", "url": LLM, "model": "m"}]}))
        self.assertNotIn("llm_backends", self._stored())

    def test_backend_list_resave_and_removal_do_not_ask(self):
        self._elevate()
        bk = [{"id": "b1", "name": "x", "url": LLM, "model": "m",
               "api_key": "sk-owner"}]
        self.assertEqual(self._save({"llm_backends": bk}).status_code, 200)
        self._unelevate()
        resave = [{"id": "b1", "name": "renamed", "url": LLM, "model": "m2"}]
        r = self._save({"llm_backends": resave})
        self.assertEqual(r.status_code, 200, r.text)
        r = self._save({"llm_backends": []})
        self.assertEqual(r.status_code, 200, r.text)

    def test_unelevated_session_cannot_consent_to_remote_tax_documents(self):
        self.assertRefused(self._save({"taxdocs_allow_remote_llm": True}))
        self.assertNotIn("taxdocs_allow_remote_llm", self._stored())


class RecipientTests(_Base):

    def test_unelevated_session_cannot_add_a_daily_mail_recipient(self):
        self.assertRefused(self._save(
            {"email_recipients": [self.email, "thief@example.dev"]}))
        self.assertNotIn("thief@example.dev",
                         str(self._stored().get("email_recipients")))

    def test_removing_or_resaving_recipients_does_not_ask(self):
        self._elevate()
        both = [self.email, "partner@example.dev"]
        self.assertEqual(self._save({"email_recipients": both})
                         .status_code, 200)
        self._unelevate()
        self.assertEqual(self._save({"email_recipients": both})
                         .status_code, 200)
        self.assertEqual(self._save({"email_recipients": [self.email]})
                         .status_code, 200)

    def test_form_door_cannot_add_a_recipient_unelevated(self):
        r = self.c.post("/settings/email",
                        data={"email_recipients": "thief@example.dev"},
                        follow_redirects=False)
        self.assertRefused(r)
        self.assertNotIn("thief@example.dev",
                         str(self._stored().get("email_recipients")))


if __name__ == "__main__":
    unittest.main()
