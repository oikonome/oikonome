"""The manual "Send today's email now" button must never report "Sent" when
nobody was mailed. On hosted, household mail goes only to verified
addresses; unguarded, an owner who has not yet clicked the confirmation
link gets `{ok: true, subject}` and no email — a silent failure they cannot
tell from a slow inbox."""
import os
import unittest
import uuid
from unittest import mock

from fastapi import HTTPException

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.jobs import worker
from oikonome.web import api, report
from .util import TEST_DB, _admin_dsn, make_db, write_config


class SendNowNeedsARecipientTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        write_config(self.conn)
        self.owner = f"owner-{uuid.uuid4().hex[:8]}@example.com"
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s,%s,'x','owner')", (self.tid, self.owner))
        finally:
            admin.close()
        self._hosted = os.environ.get("OIKONOME_HOSTED")
        os.environ["OIKONOME_HOSTED"] = "1"

    def tearDown(self):
        if self._hosted is None:
            os.environ.pop("OIKONOME_HOSTED", None)
        else:
            os.environ["OIKONOME_HOSTED"] = self._hosted
        self.conn.close()

    def _verify_owner(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute("UPDATE users SET verified_at=now() WHERE email=%s",
                          (self.owner,))
        finally:
            admin.close()

    def _send(self, **kw):
        with mock.patch.object(
                report, "send_each",
                return_value=[{"email": self.owner, "ok": True,
                               "error": None}]) as m:
            out = worker.email_tenant(self.tid, send_it=True, **kw)
        return out, m

    def test_unverified_owner_is_told_nothing_was_sent(self):
        with mock.patch.object(report, "send_each") as m:
            with self.assertRaises(worker.NoRecipients) as cm:
                worker.email_tenant(self.tid, send_it=True, force_email=True)
        m.assert_not_called()
        self.assertIn("confirmation link", str(cm.exception))

    def test_verified_owner_gets_the_mail(self):
        self._verify_owner()
        subject, m = self._send(force_email=True)
        self.assertTrue(m.called)
        self.assertEqual(m.call_args.args[3], [self.owner])
        self.assertTrue(subject)

    def test_scheduled_send_stays_silent_without_recipients(self):
        # the hourly sweep must not raise on a household nobody can be
        # mailed — that is a decision already made, not an error
        with mock.patch.object(report, "send_each") as m:
            worker.email_tenant(self.tid, send_it=True)
        m.assert_not_called()

    def test_configured_recipients_do_not_smuggle_an_unverified_owner(self):
        # the configured-list branch must not put the owner on the list
        # regardless of verification — the other branch refuses too
        budget.save_config(self.conn, {"email_recipients": ["guest@example.com"]})
        with mock.patch.object(report, "send_each") as m:
            with self.assertRaises(worker.NoRecipients):
                worker.email_tenant(self.tid, send_it=True, force_email=True)
        m.assert_not_called()
        self._verify_owner()
        _, m = self._send(force_email=True)
        self.assertEqual(m.call_args.args[3], [self.owner])

    def test_the_button_answers_409_not_ok(self):
        user = {"tenant_id": self.tid, "user_id": "u", "email": self.owner}
        with mock.patch.object(api.demoguard, "deny"):
            with self.assertRaises(HTTPException) as cm:
                api.jobs_email_api(user=user)
        self.assertEqual(cm.exception.status_code, 409)
        self.assertIn("Nothing sent", cm.exception.detail)


if __name__ == "__main__":
    unittest.main()
