"""`oikonome keycheck` — master-key continuity probe.

A database dump from another install restores fine, but every ciphertext
in it was written under THAT install's OIKONOME_MASTER_KEY: syncs then die
with crypto errors long after the restore looked successful. The manager
runs `oikonome keycheck` inside the app container right after a restore;
these tests pin the CLI helper's verdicts: clean DB, matching key, wrong
key (the restore-from-elsewhere case), and missing key.
"""

import os
import unittest
import uuid

from cryptography.fernet import Fernet

from oikonome import cli
from oikonome.db import crypto, tenancy

from .util import _ensure_db, make_db


class _KeyFixture(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("OIKONOME_MASTER_KEY")
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        _ensure_db()
        # keycheck scans EVERY tenant's wrapped key — earlier suites in the
        # shared test database leave keys wrapped under their own throwaway
        # master keys (their tenants are finished; nothing re-reads them).
        # Clear them so each test only sees secrets written under the key
        # set above.
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM tenant_keys")
            admin.execute(
                "UPDATE users SET totp_secret = NULL "
                "WHERE totp_secret LIKE %s", (crypto.CP_PREFIX + "%",))
            # the instance web-push key is a CP ciphertext too (probed and
            # re-wrapped like TOTP); an earlier suite may have minted one
            admin.execute("DELETE FROM push_vapid")
        finally:
            admin.close()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("OIKONOME_MASTER_KEY", None)
        else:
            os.environ["OIKONOME_MASTER_KEY"] = self._prev

    def _store_tenant_secret(self):
        conn = make_db()
        try:
            stored = crypto.encrypt(conn, "tok-abc123")
            # sanity: the key is set, so this must be real ciphertext (a
            # plaintext passthrough would make keycheck vacuously green)
            self.assertTrue(stored.startswith(crypto.PREFIX))
        finally:
            conn.close()


class KeycheckTests(_KeyFixture):
    def test_no_secrets_is_ok(self):
        self.assertTrue(cli.keycheck())

    def test_matching_key_is_ok(self):
        self._store_tenant_secret()
        self.assertTrue(cli.keycheck())

    def test_restored_under_other_key_fails(self):
        self._store_tenant_secret()
        # the restore-from-another-install case: data written under one
        # master key, instance running with a different one
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        self.assertFalse(cli.keycheck())

    def test_missing_key_with_secrets_fails(self):
        self._store_tenant_secret()
        del os.environ["OIKONOME_MASTER_KEY"]
        self.assertFalse(cli.keycheck())

    def test_control_plane_totp_probed_too(self):
        # users.totp_secret encrypts directly under the master key (no
        # tenant envelope) — keycheck must cover it even with no tenant_keys
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"kc-{uuid.uuid4().hex[:8]}")
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "totp_secret) VALUES (%s, %s, %s, %s)",
                (tid, f"kc-{uuid.uuid4().hex[:8]}@example.dev", "x",
                 crypto.encrypt_cp("JBSWY3DPEHPK3PXP")))
        finally:
            admin.close()
        self.assertTrue(cli.keycheck())
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        self.assertFalse(cli.keycheck())


class RotateMasterKeyTests(_KeyFixture):
    """`oikonome rotate-master-key` — same fixture as keycheck
    (fresh master key per test, cross-suite rows cleared)."""

    def _store_cp_totp(self):
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"rk-{uuid.uuid4().hex[:8]}")
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "totp_secret) VALUES (%s, %s, %s, %s)",
                (tid, f"rk-{uuid.uuid4().hex[:8]}@example.dev", "x",
                 crypto.encrypt_cp("JBSWY3DPEHPK3PXP")))
        finally:
            admin.close()

    def test_rotate_then_keycheck_passes_under_new_key(self):
        conn = make_db()
        try:
            stored = crypto.encrypt(conn, "tok-rotate-me")
            self.assertTrue(stored.startswith(crypto.PREFIX))
            self._store_cp_totp()
            old = os.environ["OIKONOME_MASTER_KEY"]
            os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
            os.environ["OIKONOME_MASTER_KEY_OLD"] = old
            try:
                self.assertTrue(cli.rotate_master_key())
            finally:
                os.environ.pop("OIKONOME_MASTER_KEY_OLD")
            self.assertTrue(cli.keycheck())
            # the secret itself still round-trips (data key unchanged,
            # only its wrapping moved)
            self.assertEqual(crypto.decrypt(conn, stored), "tok-rotate-me")
        finally:
            conn.close()

    def test_web_push_key_is_rewrapped_and_probed(self):
        """push_vapid.private_key encrypts directly under the master key.
        A rotation that skipped it would leave web push undeliverable the
        moment the old key is dropped — and keycheck, which is what tells
        the operator the old key CAN be dropped, must count it too."""
        from oikonome.notify import push
        admin = tenancy.admin_connect()
        try:
            priv, _pub = push.vapid_keys(admin)
            self.assertTrue(priv.startswith("-----BEGIN"))
            stored_old = admin.execute(
                "SELECT private_key FROM push_vapid WHERE id=1"
            ).fetchone()["private_key"]
            self.assertTrue(stored_old.startswith(crypto.CP_PREFIX))
        finally:
            admin.close()
        old = os.environ["OIKONOME_MASTER_KEY"]
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        # under the new key alone the stored ciphertext is unreadable —
        # keycheck must say so rather than pass on TOTP/tenant rows alone
        self.assertFalse(cli.keycheck())
        os.environ["OIKONOME_MASTER_KEY_OLD"] = old
        try:
            self.assertTrue(cli.rotate_master_key())
        finally:
            os.environ.pop("OIKONOME_MASTER_KEY_OLD")
        self.assertTrue(cli.keycheck())
        admin = tenancy.admin_connect()
        try:
            stored_new = admin.execute(
                "SELECT private_key FROM push_vapid WHERE id=1"
            ).fetchone()["private_key"]
            self.assertNotEqual(stored_old, stored_new)
            # the same PEM comes back under the new key — push keeps
            # signing with the key browsers already subscribed to
            self.assertEqual(priv, push.vapid_keys(admin)[0])
        finally:
            admin.close()

    def test_wrong_old_key_changes_nothing(self):
        self._store_tenant_secret()
        good = os.environ["OIKONOME_MASTER_KEY"]
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        os.environ["OIKONOME_MASTER_KEY_OLD"] = Fernet.generate_key().decode()
        try:
            self.assertFalse(cli.rotate_master_key())
        finally:
            os.environ.pop("OIKONOME_MASTER_KEY_OLD")
        # rolled back: everything still decrypts under the original key
        os.environ["OIKONOME_MASTER_KEY"] = good
        self.assertTrue(cli.keycheck())

    def test_rerun_is_idempotent(self):
        self._store_tenant_secret()
        old = os.environ["OIKONOME_MASTER_KEY"]
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        os.environ["OIKONOME_MASTER_KEY_OLD"] = old
        try:
            self.assertTrue(cli.rotate_master_key())
            # interrupted-rotation replay: already-rotated rows are skipped
            self.assertTrue(cli.rotate_master_key())
        finally:
            os.environ.pop("OIKONOME_MASTER_KEY_OLD")
        self.assertTrue(cli.keycheck())

    def test_missing_envs_refused(self):
        self.assertFalse(cli.rotate_master_key())   # no OLD set


if __name__ == "__main__":
    unittest.main()
