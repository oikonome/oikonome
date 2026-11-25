"""A restore keeps the household's stored AI keys and mail password only
for the destination they were saved for.

An export never carries a credential, so a restore puts the stored one
back beside the setting that names where it is sent: each AI backend's key
and extra body onto the archive's entry of the same id, the single
endpoint's key beside its URL, the relay's password and login beside its
host. That is what lets an instance restore its own export without being
unconfigured. But an archive is a file anyone holding it can edit: one
naming another URL under the same backend id, or another relay host, would
otherwise hand the stored key to that destination. So a credential is
carried only when the archive's destination is the stored one; moved, the
entry lands with no credential, as a new backend from an archive does, and
the owner types one in for the new destination."""

import csv
import io
import json
import unittest
import zipfile

from oikonome.engine import budget
from oikonome.sync import restore

from .util import make_db, write_config

STORED = {
    "llm_backends": [{"id": "b1", "name": "Box",
                      "url": "http://10.0.0.5:8000/v1", "model": "m",
                      "api_key": "sk-invented-stored",
                      "extra_body": '{"org": "invented"}'}],
    "llm_roles": {"assistant": "b1"},
    "llm_url": "http://10.0.0.6:11434/v1", "llm_model": "m",
    "llm_api_key": "sk-invented-legacy",
    "llm_extra_body": '{"team": "invented"}',
    "smtp_host": "mail.example.test", "smtp_port": 587,
    "smtp_user": "relay-login-invented", "smtp_password": "pw-invented",
    "smtp_from": "books@example.test",
}


def _zip(config: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps(config)})
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


class ACredentialFollowsOnlyItsOwnDestination(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, **json.loads(json.dumps(STORED)))
        self.addCleanup(self.conn.close)

    def _restore(self, **archive) -> dict:
        restore.restore_zip(self.conn, _zip(archive))
        return budget.load_config(self.conn)

    def _backend(self, url: str) -> dict:
        return {"id": "b1", "name": "Box", "url": url, "model": "m"}

    # ---- an AI backend, matched by id ----------------------------------

    def test_a_backend_moved_under_the_same_id_lands_without_its_key(self):
        cfg = self._restore(llm_backends=[
            self._backend("http://10.0.0.77:8000/v1")],
            llm_roles={"assistant": "b1"})
        b = cfg["llm_backends"][0]
        self.assertEqual(b["url"], "http://10.0.0.77:8000/v1")
        self.assertNotIn("api_key", b)
        self.assertNotIn("extra_body", b)

    def test_a_backend_at_its_stored_url_keeps_its_key(self):
        cfg = self._restore(llm_backends=[
            self._backend("http://10.0.0.5:8000/v1")],
            llm_roles={"assistant": "b1"})
        b = cfg["llm_backends"][0]
        self.assertEqual(b["api_key"], "sk-invented-stored")
        self.assertEqual(b["extra_body"], '{"org": "invented"}')

    def test_a_host_spelled_in_another_case_is_the_same_destination(self):
        cfg = self._restore(llm_backends=[
            self._backend("http://10.0.0.5:8000/v1 ")],
            llm_roles={"assistant": "b1"})
        self.assertEqual(cfg["llm_backends"][0]["api_key"],
                         "sk-invented-stored")

    # ---- the single endpoint -------------------------------------------

    def test_the_single_endpoint_moved_drops_its_key(self):
        cfg = self._restore(llm_url="http://10.0.0.88:11434/v1",
                            llm_model="m")
        self.assertEqual(cfg["llm_url"], "http://10.0.0.88:11434/v1")
        self.assertNotIn("llm_api_key", cfg)
        self.assertNotIn("llm_extra_body", cfg)

    def test_the_single_endpoint_unchanged_keeps_its_key(self):
        cfg = self._restore(llm_url="http://10.0.0.6:11434/v1",
                            llm_model="m")
        self.assertEqual(cfg["llm_api_key"], "sk-invented-legacy")
        self.assertEqual(cfg["llm_extra_body"], '{"team": "invented"}')

    # ---- the mail relay ------------------------------------------------

    def test_a_relay_on_another_host_drops_its_password_and_login(self):
        cfg = self._restore(smtp_host="relay.elsewhere.test", smtp_port=587,
                            smtp_from="books@example.test")
        self.assertEqual(cfg["smtp_host"], "relay.elsewhere.test")
        self.assertNotIn("smtp_password", cfg)
        self.assertNotIn("smtp_user", cfg)

    def test_the_same_relay_keeps_its_password_and_login(self):
        cfg = self._restore(smtp_host="MAIL.example.test", smtp_port=587,
                            smtp_from="books@example.test")
        self.assertEqual(cfg["smtp_password"], "pw-invented")
        self.assertEqual(cfg["smtp_user"], "relay-login-invented")

    def test_an_archive_naming_no_destination_keeps_every_credential(self):
        cfg = self._restore(food_monthly=444)
        self.assertEqual(cfg["llm_api_key"], "sk-invented-legacy")
        self.assertEqual(cfg["smtp_password"], "pw-invented")
        self.assertEqual(cfg["llm_backends"][0]["api_key"],
                         "sk-invented-stored")


class TheRestoreSaysWhichCredentialItDidNotCarry(unittest.TestCase):
    """A credential left behind is only found out when the AI or the mail
    later fails, so the restore's result names what it did not carry, from
    a fixed vocabulary the clients word: "ai_key" then "mail_password"."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn, **json.loads(json.dumps(STORED)))
        self.addCleanup(self.conn.close)

    def _dropped(self, **archive) -> list:
        counts = restore.restore_zip(self.conn, _zip(archive))
        return counts.credentials_dropped

    def test_a_moved_endpoint_says_ai_key(self):
        self.assertEqual(self._dropped(llm_url="http://10.0.0.88:11434/v1",
                                       llm_model="m"), ["ai_key"])

    def test_a_moved_backend_says_ai_key(self):
        self.assertEqual(self._dropped(llm_backends=[
            {"id": "b1", "name": "Box", "url": "http://10.0.0.77:8000/v1",
             "model": "m"}], llm_roles={"assistant": "b1"}), ["ai_key"])

    def test_a_moved_relay_says_mail_password(self):
        self.assertEqual(self._dropped(smtp_host="relay.elsewhere.test",
                                       smtp_from="books@example.test"),
                         ["mail_password"])

    def test_both_moved_say_both_in_order(self):
        self.assertEqual(self._dropped(
            smtp_host="relay.elsewhere.test", smtp_from="books@example.test",
            llm_url="http://10.0.0.88:11434/v1", llm_model="m"),
            ["ai_key", "mail_password"])

    def test_nothing_moved_says_nothing(self):
        self.assertEqual(self._dropped(food_monthly=444), [])
        self.assertEqual(self._dropped(
            llm_url="http://10.0.0.6:11434/v1", llm_model="m",
            smtp_host="mail.example.test"), [])

    def test_the_restore_note_tells_the_person(self):
        counts = restore.restore_zip(self.conn, _zip(
            {"llm_url": "http://10.0.0.88:11434/v1", "llm_model": "m"}))
        self.assertTrue(any("AI" in n and "key" in n
                            for n in counts.get("_notes") or []),
                        counts.get("_notes"))


class ADefaultPortIsTheSameAddress(unittest.TestCase):
    def test_the_schemes_default_port_written_out_is_the_same(self):
        same = restore._destination
        self.assertEqual(same("https://host.test:443/v1"),
                         same("https://host.test/v1"))
        self.assertEqual(same("http://host.test:80/v1"),
                         same("http://HOST.test/v1/"))
        self.assertNotEqual(same("https://host.test:8443/v1"),
                            same("https://host.test/v1"))
        self.assertNotEqual(same("http://host.test:443/v1"),
                            same("http://host.test/v1"))


if __name__ == "__main__":
    unittest.main()
