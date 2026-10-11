"""A relay saved in Settings never sends from an empty From.

An empty From used to become oikonome@localhost, which an authenticated
relay refuses (the account does not own it) and a recipient's DMARC check
drops, so the daily email silently stopped. The setup wizard already filled
an empty From from the relay username; the settings door now does the
same, and refuses the save when the username is not an address to fill it
with. A relay stored empty before that still sends as its own account.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.engine import budget
from oikonome.web import report

from .util import _ensure_db, make_db


class SettingsDoorFillsTheFrom(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"smtpf-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.client.post("/api/auth/elevate",
                        json={"password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def tearDown(self):
        self.client.post("/api/settings", json={"smtp_host": ""})

    def test_an_empty_from_takes_the_relay_account(self):
        r = self.client.post("/api/settings", json={
            "smtp_host": "smtp.test", "smtp_user": "me@mail.test",
            "smtp_from": ""})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("me@mail.test", r.json()["smtp_from"])

    def test_an_empty_from_with_no_address_to_take_is_refused(self):
        r = self.client.post("/api/settings", json={
            "smtp_host": "smtp.test", "smtp_user": "apikey",
            "smtp_from": ""})
        self.assertEqual(400, r.status_code, r.text)
        self.assertIn("smtp_from", r.json()["detail"])
        r = self.client.get("/api/settings")
        self.assertNotEqual("smtp.test", r.json().get("smtp_host"))

    def test_a_host_only_lan_relay_is_saved_with_a_no_reply_from(self):
        """An unauthenticated LAN relay needs only its host, which is what
        the setup wizard asks for. With no username and no From it accepts
        any sender, so the save goes through with a no-reply address on
        the instance's own name, or the relay's when there is none."""
        from unittest import mock
        with mock.patch.dict(os.environ,
                             {"OIKONOME_BASE_URL": "https://money.home.test"}):
            r = self.client.post("/api/settings", json={
                "smtp_host": "relay.lan", "smtp_user": "", "smtp_from": ""})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("no-reply@money.home.test", r.json()["smtp_from"])
        self.client.post("/api/settings", json={"smtp_host": ""})
        with mock.patch.dict(os.environ, {"OIKONOME_BASE_URL": ""}):
            r = self.client.post("/api/settings", json={
                "smtp_host": "relay.lan", "smtp_user": "", "smtp_from": ""})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("no-reply@relay.lan", r.json()["smtp_from"])

    def test_a_from_given_is_kept(self):
        r = self.client.post("/api/settings", json={
            "smtp_host": "smtp.test", "smtp_user": "apikey",
            "smtp_from": "money@mail.test"})
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual("money@mail.test", r.json()["smtp_from"])


class AStoredEmptyFromSendsAsTheAccount(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_resolve(self):
        conn = make_db()
        try:
            cfg = budget.load_config(conn)
            cfg.update({"smtp_host": "cfg.example", "smtp_user": "u@cfg.example",
                        "smtp_from": ""})
            budget.save_config(conn, cfg)
            self.assertEqual("u@cfg.example",
                             report.resolve_smtp(conn)["sender"])
            cfg["smtp_user"] = "apikey"
            budget.save_config(conn, cfg)
            self.assertEqual("oikonome@localhost",
                             report.resolve_smtp(conn)["sender"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
