"""A restore started from the phone app is held once that device is revoked.

A restore watches the credential that started it and, if that credential
is gone when the settings land, holds the mail relay, recipients and AI
endpoint the archive brought in behind an outbound pause. A rotation
DELETEs browser sessions but only marks a device token revoked, leaving
its row and its expiry in place. If a revoked device token still counted
as signed in, a restore started from a stolen phone login would put the
archive's relay and recipients live after the owner had already changed
the password.
"""
import hashlib
import unittest
import uuid

from oikonome.auth import device_tokens
from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.notify import outbound_pause
from oikonome.sync import restore

from .test_a_restore_outliving_its_starters_session_is_paused import (
    HIJACK, _settings_zip)
from .util import make_db, write_config


class RestoreStartedFromADevice(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.admin = tenancy.admin_connect()
        self.addCleanup(self.admin.close)
        self.uid = self.admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role) "
            "VALUES (%s, %s, 'x', 'owner') RETURNING id",
            (self.tid, f"own-{uuid.uuid4().hex[:8]}@example.com"),
        ).fetchone()["id"]
        token, _row = device_tokens.mint(self.admin, self.uid, self.tid,
                                         "phone")
        # the request's credential fingerprint (web.security)
        self.starter = hashlib.sha256(token.encode()).hexdigest()[:32]
        write_config(self.conn)

    def _password_change(self, *_a):
        device_tokens.revoke_all_for_user(self.admin, self.uid)

    def _paused(self):
        return outbound_pause.paused(budget.load_config(self.conn))

    def test_revoking_the_starting_device_mid_restore_holds_the_archive(self):
        restore.restore_zip(self.conn, _settings_zip(HIJACK),
                            progress=self._password_change,
                            starter=self.starter)
        p = self._paused()
        self.assertIsNotNone(p)
        self.assertTrue(p["smtp"])
        self.assertTrue(p["llm"])
        self.assertEqual(p["recipients"], ["thief@example.com"])

    def test_a_starting_device_still_signed_in_pauses_nothing(self):
        restore.restore_zip(self.conn, _settings_zip(HIJACK),
                            starter=self.starter)
        self.assertIsNone(self._paused())


if __name__ == "__main__":
    unittest.main()
