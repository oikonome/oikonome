"""A restore whose starter is signed out before it lands its settings must
not start sending through what it brought in.

A restore asks for a step-up at upload and then runs for minutes, applying
the household's settings last. A credential rotation in between deletes
every session — and when nothing outbound was set at the time, it writes no
pause record, so there is nothing for the restore to widen. Every session
that was live when the restore began being gone is the signal: the mail
relay, recipients and AI endpoint the archive brings in are held behind an
outbound pause until the owner presses Resume. A restore whose starter is
still signed in, or one nobody signed in started, pauses nothing new.

What is watched is the credential that STARTED the restore, not the
household's sessions: a password change keeps the changer's own session and
never touches another member's, so a thief's restore must still be held
when the owner's rotation ends only the thief's session.
"""
import csv
import datetime as dt
import io
import json
import secrets
import unittest
import uuid
import zipfile

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.notify import outbound_pause
from oikonome.sync import restore

from .util import make_db, write_config


def _settings_zip(cfg: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        s = io.StringIO()
        w = csv.DictWriter(s, fieldnames=["config"])
        w.writeheader()
        w.writerow({"config": json.dumps(cfg)})
        z.writestr("tenant_settings.csv", s.getvalue())
    return buf.getvalue()


HIJACK = {"smtp_host": "relay.attacker.example",
          "llm_url": "https://203.0.113.9/v1/chat/completions",
          "email_recipients": ["thief@example.com"]}


class RestoreOutlivingItsStarter(unittest.TestCase):

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
        self.admin.execute(
            "INSERT INTO sessions (token_hash, user_id, tenant_id, expires_at)"
            " VALUES (%s, %s, %s, %s)",
            (secrets.token_hex(32), self.uid, self.tid,
             dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)))
        write_config(self.conn)

    def _rotate(self, *_a):
        self.admin.execute("DELETE FROM sessions WHERE user_id=%s",
                           (self.uid,))

    def _paused(self):
        return outbound_pause.paused(budget.load_config(self.conn))

    def test_a_rotation_mid_restore_holds_what_the_archive_brought(self):
        restore.restore_zip(self.conn, _settings_zip(HIJACK),
                            progress=self._rotate)
        p = self._paused()
        self.assertIsNotNone(p)
        self.assertTrue(p["smtp"])
        self.assertTrue(p["llm"])
        self.assertEqual(p["recipients"], ["thief@example.com"])

    def test_a_starter_still_signed_in_pauses_nothing(self):
        restore.restore_zip(self.conn, _settings_zip(HIJACK))
        self.assertIsNone(self._paused())

    def test_a_rotation_mid_restore_that_brings_nothing_outbound_pauses_nothing(self):
        restore.restore_zip(self.conn, _settings_zip({"food_monthly": 900}),
                            progress=self._rotate)
        self.assertIsNone(self._paused())


class RestoreWatchesTheSessionThatStartedIt(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.admin = tenancy.admin_connect()
        self.addCleanup(self.admin.close)

        def user(role):
            return self.admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', %s) RETURNING id",
                (self.tid, f"{role}-{uuid.uuid4().hex[:8]}@example.com",
                 role)).fetchone()["id"]

        def session(uid):
            h = secrets.token_hex(32)
            self.admin.execute(
                "INSERT INTO sessions (token_hash, user_id, tenant_id,"
                " expires_at) VALUES (%s, %s, %s, %s)",
                (h, uid, self.tid,
                 dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)))
            return h

        self.owner = user("owner")
        member = user("member")
        self.owner_session = session(self.owner)
        self.thief_session = session(self.owner)     # a stolen owner session
        session(member)                               # the spouse, signed in
        write_config(self.conn)

    def _password_change_from_owner_browser(self, *_a):
        # what the password door does: every other session of the user
        # goes, the changer's own is kept, other members are untouched
        self.admin.execute(
            "DELETE FROM sessions WHERE user_id=%s AND token_hash != %s",
            (self.owner, self.owner_session))

    def test_a_rotation_that_keeps_the_owners_session_holds_the_thiefs_restore(self):
        restore.restore_zip(self.conn, _settings_zip(HIJACK),
                            progress=self._password_change_from_owner_browser,
                            starter=self.thief_session[:32])
        p = outbound_pause.paused(budget.load_config(self.conn))
        self.assertIsNotNone(p)
        self.assertTrue(p["smtp"])
        self.assertEqual(p["recipients"], ["thief@example.com"])

    def test_the_owners_own_restore_survives_the_owners_rotation(self):
        restore.restore_zip(self.conn, _settings_zip(HIJACK),
                            progress=self._password_change_from_owner_browser,
                            starter=self.owner_session[:32])
        self.assertIsNone(outbound_pause.paused(budget.load_config(self.conn)))


if __name__ == "__main__":
    unittest.main()
