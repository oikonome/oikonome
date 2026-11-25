"""A connections bundle keeps a stored AI key or mail password only for
the destination it was saved for.

A bundle sets the endpoint and the relay host it carries and replaces only
the secrets it carries too; one naming another endpoint with no key would
otherwise send the household's stored key there, and one naming another
relay host would send it the stored password and login. So a stored
credential stays when the bundle brings its own, or when the destination
did not move — the same rule a restore follows. Named AI backends arrive
whole by id with the bundle's own credential, so no stored key is carried
onto them at all."""

import os
import unittest

from cryptography.fernet import Fernet

from oikonome.db import crypto
from oikonome.engine import budget
from oikonome.sync import connections_bundle as cb

from .util import make_db, write_config


class ABundleCarriesACredentialOnlyToItsDestination(unittest.TestCase):

    def setUp(self):
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        self.addCleanup(os.environ.pop, "OIKONOME_MASTER_KEY", None)
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(
            self.conn,
            llm_url="http://10.0.0.6:11434/v1", llm_model="m",
            llm_api_key=crypto.encrypt(self.conn, "sk-invented-legacy"),
            smtp_host="mail.example.test", smtp_port=587,
            smtp_user="relay-login-invented",
            smtp_from="books@example.test",
            smtp_password=crypto.encrypt(self.conn, "pw-invented"),
            llm_backends=[{"id": "b1", "name": "Box",
                           "url": "http://10.0.0.5:8000/v1", "model": "m",
                           "api_key": crypto.encrypt(self.conn,
                                                     "sk-invented-b1")}])

    def _apply(self, config=None, secrets=None, backends=None) -> dict:
        self.summary = cb.apply_payload(
            self.conn, self._payload(config, secrets, backends))
        return budget.load_config(self.conn)

    def _payload(self, config=None, secrets=None, backends=None) -> dict:
        payload = {"oikonome_config": cb.FORMAT, "config": config or {},
                   "secrets": secrets or {}, "items": []}
        if backends is not None:
            payload["llm_backends"] = backends
        return payload

    # ---- the reply names what was not carried ------------------------

    def test_the_reply_names_a_dropped_ai_key(self):
        self._apply({"llm_url": "http://10.0.0.88:11434/v1",
                     "llm_model": "m"})
        self.assertEqual(self.summary["credentials_dropped"], ["ai_key"])

    def test_the_reply_names_a_dropped_mail_password(self):
        self._apply({"smtp_host": "relay.elsewhere.test"})
        self.assertEqual(self.summary["credentials_dropped"],
                         ["mail_password"])

    def test_the_reply_names_both_in_order(self):
        self._apply({"smtp_host": "relay.elsewhere.test",
                     "llm_url": "http://10.0.0.88:11434/v1",
                     "llm_model": "m"})
        self.assertEqual(self.summary["credentials_dropped"],
                         ["ai_key", "mail_password"])

    def test_the_reply_names_nothing_when_nothing_moved(self):
        self._apply({"llm_url": "http://10.0.0.6:11434/v1",
                     "llm_model": "m", "smtp_host": "mail.example.test"})
        self.assertEqual(self.summary["credentials_dropped"], [])

    def test_a_moved_address_with_its_own_key_names_nothing(self):
        self._apply({"llm_url": "http://10.0.0.88:11434/v1",
                     "llm_model": "m"}, {"llm_api_key": "sk-invented-new"})
        self.assertEqual(self.summary["credentials_dropped"], [])

    def _plain(self, cfg, key):
        return crypto.decrypt(self.conn, cfg.get(key)) if cfg.get(key) \
            else None

    def test_a_moved_endpoint_without_a_key_keeps_no_stored_key(self):
        cfg = self._apply({"llm_url": "http://10.0.0.88:11434/v1",
                           "llm_model": "m"})
        self.assertEqual(cfg["llm_url"], "http://10.0.0.88:11434/v1")
        self.assertNotIn("llm_api_key", cfg)

    def test_a_moved_endpoint_with_its_own_key_takes_that_key(self):
        cfg = self._apply({"llm_url": "http://10.0.0.88:11434/v1",
                           "llm_model": "m"},
                          {"llm_api_key": "sk-invented-new"})
        self.assertEqual(self._plain(cfg, "llm_api_key"), "sk-invented-new")

    def test_an_unmoved_endpoint_keeps_the_stored_key(self):
        cfg = self._apply({"llm_url": "http://10.0.0.6:11434/v1",
                           "llm_model": "m"})
        self.assertEqual(self._plain(cfg, "llm_api_key"),
                         "sk-invented-legacy")

    def test_a_moved_relay_keeps_no_stored_password_or_login(self):
        cfg = self._apply({"smtp_host": "relay.elsewhere.test"})
        self.assertNotIn("smtp_password", cfg)
        self.assertNotIn("smtp_user", cfg)

    def test_a_moved_relay_with_its_own_password_and_login_takes_them(self):
        cfg = self._apply({"smtp_host": "relay.elsewhere.test",
                           "smtp_user": "other-login-invented"},
                          {"smtp_password": "pw-invented-new"})
        self.assertEqual(self._plain(cfg, "smtp_password"), "pw-invented-new")
        self.assertEqual(self._plain(cfg, "smtp_user"), "other-login-invented")

    def test_an_unmoved_relay_keeps_its_password_and_login(self):
        cfg = self._apply({"smtp_host": "MAIL.example.test"})
        self.assertEqual(self._plain(cfg, "smtp_password"), "pw-invented")
        self.assertEqual(cfg["smtp_user"], "relay-login-invented")

    def test_a_backend_arrives_whole_with_the_bundles_own_key(self):
        cfg = self._apply(backends=[
            {"id": "b1", "name": "Box", "url": "http://10.0.0.77:8000/v1",
             "model": "m"}])
        b = next(x for x in cfg["llm_backends"] if x["id"] == "b1")
        self.assertEqual(b["url"], "http://10.0.0.77:8000/v1")
        self.assertNotIn("api_key", b)


if __name__ == "__main__":
    unittest.main()
