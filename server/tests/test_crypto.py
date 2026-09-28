"""Envelope encryption: round-trip, per-tenant key isolation, at-rest
ciphertext, dev-mode passthrough."""

import os
import unittest

from cryptography.fernet import Fernet, InvalidToken

from oikonome.db import crypto
from oikonome.sync import base

from .util import make_db, write_config


class CryptoTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("OIKONOME_MASTER_KEY")
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("OIKONOME_MASTER_KEY", None)
        else:
            os.environ["OIKONOME_MASTER_KEY"] = self._prev
        self.conn.close()

    def test_round_trip_and_tagging(self):
        ct = crypto.encrypt(self.conn, "https://u:p@bridge/simplefin")
        self.assertTrue(ct.startswith("enc:v1:"))
        self.assertNotIn("bridge", ct)
        self.assertEqual(crypto.decrypt(self.conn, ct),
                         "https://u:p@bridge/simplefin")

    def test_item_token_encrypted_at_rest(self):
        base.upsert_item(self.conn, "it-x", "simplefin", "Demo",
                         "secret-token-123")
        raw = self.conn.execute(
            "SELECT access_token FROM items WHERE id='it-x'").fetchone()
        self.assertTrue(raw["access_token"].startswith("enc:v1:"))
        self.assertNotIn("secret-token-123", raw["access_token"])
        self.assertEqual(base.get_access_token(self.conn, "it-x"),
                         "secret-token-123")

    def test_per_tenant_key_isolation(self):
        """Tenant B's key cannot decrypt tenant A's ciphertext, even with
        the same master key."""
        ct = crypto.encrypt(self.conn, "tenant-a-secret")
        other = make_db()
        try:
            with self.assertRaises(InvalidToken):
                crypto.decrypt(other, ct)
        finally:
            other.close()

    def test_dev_mode_passthrough(self):
        os.environ.pop("OIKONOME_MASTER_KEY", None)
        self.assertEqual(crypto.encrypt(self.conn, "plain"), "plain")
        self.assertEqual(crypto.decrypt(self.conn, "plain"), "plain")
        # but an encrypted value with no key must fail LOUDLY, not silently
        with self.assertRaises(RuntimeError):
            crypto.decrypt(self.conn, "enc:v1:abc")

    def test_legacy_plaintext_passthrough(self):
        self.assertEqual(crypto.decrypt(self.conn, "access-legacy"),
                         "access-legacy")


if __name__ == "__main__":
    unittest.main()
