"""Adding a master key encrypts every secret written before it existed.

Without a master key, secrets are stored as written. When one is added,
the startup sweep has to reach every column and settings key that holds a
secret — a business's EIN, a webhook's signing secret, an AI backend's key
or request extras — not only bank tokens, or those stay in the clear in
every dump and backup from then on.
"""
import os
import unittest
import uuid
from unittest import mock

from cryptography.fernet import Fernet

from oikonome.db import crypto, migrate, tenancy
from oikonome.engine import budget

from .util import _ensure_db


class SecretSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("OIKONOME_MASTER_KEY", None)
        admin = tenancy.admin_connect()
        self.tid = str(tenancy.create_tenant(admin, "sweep"))
        admin.close()
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                "INSERT INTO business_entity (name, structure, ein_enc)"
                " VALUES ('Shop', 'sole_prop', '123456789')")
            conn.execute(
                "INSERT INTO webhooks (url, secret)"
                " VALUES ('https://hooks.example.test/x', 'signing-plain')")
            budget.save_config(conn, {
                "food_monthly": 100,
                "llm_extra_body": '{"org": "plain-org"}',
                "llm_backends": [{"id": "b1", "name": "B", "model": "m",
                                  "url": "https://llm.example.test",
                                  "api_key": "plain-key",
                                  "extra_body": '{"t": "plain"}'}]})
        finally:
            conn.close()
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()

    def test_every_secret_is_encrypted_and_still_reads_back(self):
        migrate.reencrypt_tenant_secrets()
        conn = tenancy.tenant_connect(self.tid)
        try:
            ein = conn.execute(
                "SELECT ein_enc FROM business_entity").fetchone()["ein_enc"]
            hook = conn.execute(
                "SELECT secret FROM webhooks").fetchone()["secret"]
            cfg = budget.load_config(conn)
            backend = cfg["llm_backends"][0]
            stored = {"ein": ein, "webhook": hook,
                      "extra": cfg["llm_extra_body"],
                      "backend_key": backend["api_key"],
                      "backend_extra": backend["extra_body"]}
            for name, v in stored.items():
                self.assertTrue(v.startswith(crypto.PREFIX), name)
            self.assertEqual(crypto.decrypt(conn, ein), "123456789")
            self.assertEqual(crypto.decrypt(conn, hook), "signing-plain")
            self.assertEqual(crypto.decrypt(conn, backend["api_key"]),
                             "plain-key")
            self.assertEqual(migrate.plaintext_tenant_secrets(conn), 0)
        finally:
            conn.close()
        self.assertEqual(migrate.reencrypt_tenant_secrets(), 0)

    def test_the_plaintext_count_sees_every_kind(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.assertEqual(migrate.plaintext_tenant_secrets(conn), 5)
        finally:
            conn.close()


    def test_a_secret_changed_mid_sweep_is_not_put_back(self):
        """The sweep reads, then writes while the app serves. A webhook
        secret rotated between the two keeps the new value."""
        real = crypto.encrypt
        mine = f"signing-{uuid.uuid4().hex}"
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE webhooks SET secret = %s"
                          " WHERE tenant_id = %s", (mine, self.tid))
        finally:
            admin.close()

        def rotate_then_encrypt(conn, v):
            if v == mine:
                admin = tenancy.admin_connect()
                try:
                    admin.execute("UPDATE webhooks SET secret = 'rotated'"
                                  " WHERE tenant_id = %s", (self.tid,))
                finally:
                    admin.close()
            return real(conn, v)

        with mock.patch.object(crypto, "encrypt",
                               side_effect=rotate_then_encrypt):
            migrate.reencrypt_tenant_secrets()
        conn = tenancy.tenant_connect(self.tid)
        try:
            hook = conn.execute(
                "SELECT secret FROM webhooks").fetchone()["secret"]
            # left for the next sweep, never overwritten with the old one
            self.assertEqual(hook, "rotated")
            self.assertEqual(migrate.plaintext_tenant_secrets(conn), 1)
        finally:
            conn.close()


class ControlPlaneSweepTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_a_factor_turned_off_mid_sweep_stays_off(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("OIKONOME_MASTER_KEY", None)
        seed = f"PLAINSEED{uuid.uuid4().hex[:12].upper()}"
        admin = tenancy.admin_connect()
        try:
            tid = str(tenancy.create_tenant(admin, "cp-sweep"))
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash,"
                " totp_secret) VALUES (%s, %s, 'x', %s) RETURNING id",
                (tid, f"cp-sweep-{uuid.uuid4().hex[:8]}@example.test", seed)
            ).fetchone()["id"]
        finally:
            admin.close()
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        real = crypto.encrypt_cp

        def disable_then_encrypt(v):
            if v == seed:
                a = tenancy.admin_connect()
                try:
                    a.execute("UPDATE users SET totp_secret = NULL"
                              " WHERE id = %s", (uid,))
                finally:
                    a.close()
            return real(v)

        admin = tenancy.admin_connect()
        try:
            with mock.patch.object(crypto, "encrypt_cp",
                                   side_effect=disable_then_encrypt):
                migrate._encrypt_cp_secrets(admin)
            self.assertIsNone(admin.execute(
                "SELECT totp_secret FROM users WHERE id = %s",
                (uid,)).fetchone()["totp_secret"])
        finally:
            admin.close()


if __name__ == "__main__":
    unittest.main()
