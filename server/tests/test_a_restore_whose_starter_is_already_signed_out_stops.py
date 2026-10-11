"""A restore whose starting credential is gone before it begins stops,
and applies nothing.

The upload is authenticated, and the restore itself starts a moment later
on a worker thread, after it has waited for its connections and the
household's sync lock. A credential rotation can land in that gap: it ends
the starter's session and keeps the rotating owner's own. The restore must
then not look for "some other session of the household" to stand in for
its starter — that survivor is exactly the session the rotation was made
from, and taking it as proof would let an archive the starter brought
(a mail relay, an extra recipient, an AI endpoint) go live with nothing
held. Bound to its own starter only, the restore stops with a reason the
person can read, and the household is left as it was.

A restore with no starting credential at all (a command-line restore, a
test) still watches the household's sessions, as before.
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


BROUGHT = {"smtp_host": "relay.invalid.example",
           "email_recipients": ["outsider@example.com"]}


class RestoreStarterAlreadySignedOut(unittest.TestCase):

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
        self.member = user("member")
        self.starter_session = session(self.owner)
        self.member_session = session(self.member)
        write_config(self.conn)
        self.before = budget.load_config(self.conn)

    def _evict_starter(self):
        self.admin.execute("DELETE FROM sessions WHERE token_hash=%s",
                           (self.starter_session,))

    def test_starter_evicted_with_only_another_members_session_alive_stops(self):
        self._evict_starter()
        with self.assertRaises(ValueError) as cm:
            restore.restore_zip(self.conn, _settings_zip(BROUGHT),
                                starter=self.starter_session[:32])
        self.assertIn("signed out", str(cm.exception))
        after = budget.load_config(self.conn)
        self.assertNotEqual(after.get("smtp_host"), BROUGHT["smtp_host"])
        self.assertEqual(after.get("email_recipients"),
                         self.before.get("email_recipients"))

    def test_starter_still_signed_in_restores(self):
        restore.restore_zip(self.conn, _settings_zip(BROUGHT),
                            starter=self.starter_session[:32])
        self.assertEqual(budget.load_config(self.conn).get("smtp_host"),
                         BROUGHT["smtp_host"])

    def test_a_restore_with_no_starter_still_runs(self):
        restore.restore_zip(self.conn, _settings_zip(BROUGHT), starter="")
        self.assertEqual(budget.load_config(self.conn).get("smtp_host"),
                         BROUGHT["smtp_host"])


if __name__ == "__main__":
    unittest.main()
