"""The doctor's "secrets encrypted at rest" line covers every stored secret.

With a master key set, the worker's startup sweep re-encrypts whatever was
stored in plaintext before the key existed: aggregator tokens, EINs,
webhook signing secrets and the keys in settings. Until it has run, the
doctor must not say the household's secrets are encrypted — and it must
count the same things the sweep does, not aggregator tokens alone.
"""
import os
import unittest
import uuid

from cryptography.fernet import Fernet

from oikonome.db import tenancy

from .util import TEST_DB, _admin_dsn, _ensure_db, seed_accounts


class DoctorCountsEveryStoredSecret(unittest.TestCase):
    def setUp(self):
        _ensure_db()
        self._prev = os.environ.get("OIKONOME_MASTER_KEY")
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"doc-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        # no aggregator token at all, so only the row below can be plaintext
        self.conn.execute("UPDATE items SET access_token=NULL")

    def tearDown(self):
        self.conn.close()
        self.admin.close()
        if self._prev is None:
            os.environ.pop("OIKONOME_MASTER_KEY", None)
        else:
            os.environ["OIKONOME_MASTER_KEY"] = self._prev

    def _encryption_row(self):
        from oikonome.web import doctor
        rows = [c for c in doctor.checks(self.tid)
                if c["name"] == "token encryption"]
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_nothing_plaintext_reads_encrypted(self):
        self.assertTrue(self._encryption_row()["ok"])

    def test_a_plaintext_webhook_secret_is_not_called_encrypted(self):
        self.conn.execute(
            "INSERT INTO webhooks (url, name, secret) "
            "VALUES ('https://hooks.example.dev/in', 'test', 'plain-secret')")
        row = self._encryption_row()
        self.assertFalse(row["ok"])
        self.assertIn("PLAINTEXT", row["detail"])
        self.assertNotIn("plain-secret", row["detail"])


if __name__ == "__main__":
    unittest.main()
