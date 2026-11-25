"""The mail relay's login is stored encrypted, like its password.

Postmark takes the server API token as both the SMTP username and the
password, so smtp_user is a credential whatever its name says. It is
stored under the household's key, decrypted only where mail is sent and
for the owner's own settings view, never frozen into a month's budget
snapshot, carried by the connections bundle as a secret (re-encrypted
under the destination's key), and swept into ciphertext where an older
build stored it in the clear.
"""

import datetime as dt
import os
import unittest
from pathlib import Path
from unittest import mock

from cryptography.fernet import Fernet

from oikonome.db import crypto, migrate
from oikonome.engine import budget
from oikonome.sync import connections_bundle as cb
from oikonome.web import report, settings_exposure

from .util import make_db, write_config

LOGIN = "relay-token-invented"
MIGRATION = (Path(__file__).resolve().parents[1] / "oikonome" / "db"
             / "migrations" / "224_the_mail_relay_user_is_encrypted.sql")


class RelayLoginAtRest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {
            "OIKONOME_MASTER_KEY": Fernet.generate_key().decode(),
            "OIKONOME_HOSTED": ""})
        env.start()
        self.addCleanup(env.stop)
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _relay(self, user):
        write_config(self.conn, smtp_host="mail.example.test",
                     smtp_port=587, smtp_user=user,
                     smtp_from="books@example.test",
                     smtp_password=crypto.encrypt(self.conn, "pw-invented"))

    def _stored(self):
        return budget.load_config(self.conn).get("smtp_user")

    # ---- reading it back -------------------------------------------------

    def test_the_sender_logs_in_with_the_decrypted_login(self):
        self._relay(crypto.encrypt(self.conn, LOGIN))
        with mock.patch("oikonome.web.netguard.check_host"):
            smtp = report.resolve_smtp(self.conn)
        self.assertEqual(smtp["user"], LOGIN)

    def test_an_empty_from_sends_as_the_decrypted_login_address(self):
        write_config(self.conn, smtp_host="mail.example.test",
                     smtp_user=crypto.encrypt(self.conn,
                                              "books@example.test"))
        with mock.patch("oikonome.web.netguard.check_host"):
            smtp = report.resolve_smtp(self.conn)
        self.assertEqual(smtp["sender"], "books@example.test")

    def test_the_owner_reads_the_login_and_a_viewer_learns_one_is_set(self):
        self._relay(crypto.encrypt(self.conn, LOGIN))
        view = settings_exposure.readable(self.conn,
                                          budget.load_config(self.conn))
        self.assertEqual(view["smtp_user"], LOGIN)
        self.assertEqual(settings_exposure.view_for(
            "owner", {"smtp_user": view["smtp_user"]})["smtp_user"], LOGIN)
        self.assertEqual(settings_exposure.view_for(
            "viewer", {"smtp_user": view["smtp_user"]}),
            {"smtp_user_set": True})

    def test_a_login_that_will_not_decrypt_is_left_out_of_the_view(self):
        self._relay(crypto.PREFIX + "not-a-token")
        view = settings_exposure.readable(self.conn,
                                          budget.load_config(self.conn))
        self.assertNotIn("smtp_user", view)
        self.assertEqual(report.relay_user(self.conn,
                                           budget.load_config(self.conn)), "")

    # ---- writing it ------------------------------------------------------

    def test_a_posted_login_is_stored_encrypted(self):
        sealed = report.seal_relay_user(self.conn, None, LOGIN)
        self.assertTrue(sealed.startswith(crypto.PREFIX))
        self.assertEqual(crypto.decrypt(self.conn, sealed), LOGIN)

    def test_the_same_login_posted_again_keeps_the_stored_ciphertext(self):
        stored = crypto.encrypt(self.conn, LOGIN)
        self.assertEqual(report.seal_relay_user(self.conn, stored, LOGIN),
                         stored)
        self.assertEqual(report.seal_relay_user(self.conn, stored, ""), "")

    # ---- the month snapshot ---------------------------------------------

    def test_a_month_snapshot_freezes_no_login(self):
        self._relay(crypto.encrypt(self.conn, LOGIN))
        self.assertTrue(budget.snapshot_month(self.conn, dt.date(2026, 3, 9)))
        row = self.conn.execute(
            "SELECT config FROM budget_snapshots WHERE year=2026 AND "
            "month=3").fetchone()
        self.assertNotIn("smtp_user", row["config"])
        self.assertIn("smtp_host", row["config"])

    def test_a_snapshot_stored_with_a_login_reads_back_without_it(self):
        self.conn.execute(
            "INSERT INTO budget_snapshots (year, month, config) VALUES "
            "(2026, 2, %s::jsonb)",
            ('{"food_monthly": 100, "other_monthly": 100, '
             '"smtp_user": "relay-token-invented"}',))
        self.assertNotIn("smtp_user",
                         budget.snapshot_config(self.conn, 2026, 2))

    # ---- the connections bundle -----------------------------------------

    def test_the_bundle_carries_the_login_as_a_secret_in_plaintext(self):
        self._relay(crypto.encrypt(self.conn, LOGIN))
        payload = cb.build_payload(self.conn)
        self.assertEqual(payload["secrets"]["smtp_user"], LOGIN)
        self.assertNotIn("smtp_user", payload["config"])

    def test_an_imported_login_is_stored_encrypted(self):
        for where in ("secrets", "config"):      # config: an older bundle
            with self.subTest(where=where):
                write_config(self.conn)
                payload = {"oikonome_config": cb.FORMAT, "items": [],
                           "config": {"smtp_host": "mail.example.test"},
                           "secrets": {}}
                payload[where]["smtp_user"] = LOGIN
                cb.apply_payload(self.conn, payload)
                stored = self._stored()
                self.assertTrue(stored.startswith(crypto.PREFIX))
                self.assertEqual(crypto.decrypt(self.conn, stored), LOGIN)

    def test_a_bundle_bringing_the_same_login_leaves_it_as_stored(self):
        self._relay(crypto.encrypt(self.conn, LOGIN))
        before = self._stored()
        cb.apply_payload(self.conn, cb.build_payload(self.conn))
        self.assertEqual(self._stored(), before)

    # ---- an older build's plaintext --------------------------------------

    def test_the_secret_sweep_encrypts_a_plaintext_login_once(self):
        self._relay(LOGIN)
        self.assertGreaterEqual(migrate.plaintext_tenant_secrets(self.conn), 1)
        migrate._reencrypt_tenant(self.conn)
        sealed = self._stored()
        self.assertTrue(sealed.startswith(crypto.PREFIX))
        self.assertEqual(crypto.decrypt(self.conn, sealed), LOGIN)
        migrate._reencrypt_tenant(self.conn)
        self.assertEqual(self._stored(), sealed)
        self.assertEqual(migrate.plaintext_tenant_secrets(self.conn), 0)

    def test_the_migration_strips_the_login_from_stored_snapshots(self):
        self.conn.execute(
            "INSERT INTO budget_snapshots (year, month, config) VALUES "
            "(2026, 1, %s::jsonb)",
            ('{"food_monthly": 100, "smtp_user": "relay-token-invented", '
             '"smtp_host": "mail.example.test"}',))
        sql = MIGRATION.read_text()
        for _ in range(2):                       # idempotent
            self.conn.execute(sql)
        cfg = self.conn.execute(
            "SELECT config FROM budget_snapshots WHERE year=2026 AND "
            "month=1").fetchone()["config"]
        self.assertEqual(cfg, {"food_monthly": 100,
                               "smtp_host": "mail.example.test"})


if __name__ == "__main__":
    unittest.main()
