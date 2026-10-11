"""Scheduled household mail honours an outbound pause committed while the
report was being built.

The recipients and the relay are read before the build, which can take
minutes over a whole ledger. An owner who closes a hijack in that window
(a credential rotation pausing the extra recipients and the Settings relay)
must stop this send too: no paused address is mailed, and the paused relay
carries nothing.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.jobs import worker
from oikonome.notify import outbound_pause
from oikonome.web import report

from .util import _ensure_db, accept_recipient_invite, write_config

PW = "correct-horse-battery"


class MailHonoursMidBuildPause(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        client = TestClient(self.app)
        self.email = f"midb-{uuid.uuid4().hex[:10]}@example.dev"
        r = client.post("/api/signup", data={"email": self.email,
                                            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = client.get("/api/me").json()["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            self.uid = admin.execute(
                "SELECT id FROM users WHERE lower(email)=lower(%s)",
                (self.email,)).fetchone()["id"]
        finally:
            admin.close()
        self.partner = f"partner-{uuid.uuid4().hex[:6]}@example.dev"
        conn = tenancy.tenant_connect(self.tid)
        try:
            write_config(conn, smtp_host="relay.example.dev", smtp_port=2525,
                         smtp_starttls=False,
                         email_recipients=[self.email, self.partner])
        finally:
            conn.close()
        accept_recipient_invite(self.tid, self.partner)

    def test_a_pause_landing_after_the_recipient_read_holds_the_mail(self):
        real = worker._recipients_detail
        calls = []

        def read_then_pause(conn, tid, kind=None):
            out = real(conn, tid, kind)
            calls.append(out)
            if len(calls) == 1:
                # the owner's rotation commits while the report is built
                admin = tenancy.admin_connect()
                try:
                    outbound_pause.pause_for_rotation(admin, self.uid)
                finally:
                    admin.close()
            return out

        sent = {}

        def capture(subject, plain, html, recipients, *, smtp, **kw):
            sent["to"], sent["smtp"] = list(recipients), smtp
            return [{"email": r, "ok": True} for r in recipients]

        env = {"OIKONOME_HOSTED": "", "OIKONOME_SMTP_HOST": "env.example.dev",
               "OIKONOME_SMTP_SENDER": "no-reply@example.dev"}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(worker, "_recipients_detail",
                                  side_effect=read_then_pause), \
                mock.patch.object(report, "send_each", side_effect=capture):
            worker.email_tenant(self.tid, cadence="daily", force_email=True)
        self.assertIn(self.partner, calls[0][0],
                      "the stale read still listed the partner")
        self.assertEqual(sent["to"], [self.email])
        self.assertNotEqual(sent["smtp"].get("host"), "relay.example.dev")


if __name__ == "__main__":
    unittest.main()
