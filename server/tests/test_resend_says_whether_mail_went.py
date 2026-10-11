"""Resend answers with whether an invitation actually went out.

The per-address cooldown and the per-tenant hourly cap both stop a send
without any error. Answered with a bare `ok`, the clients said "re-sent",
the owner told the person to look again, and nothing ever arrived. The
door keeps its 200 and `ok: true` (the request was valid and nothing went
wrong) and adds `sent`, plus a `reason` word when nothing went out, so
each client can say what really happened.
"""

import datetime as dt
import os
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.notify import delivery, recipient_invites

from .test_recipient_invites import _addr, _signed_in_client
from .util import TEST_DB, _admin_dsn, _ensure_db, make_db


def _age_last_send(tid, email, minutes):
    admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
    try:
        admin.execute(
            "UPDATE recipient_invites SET last_sent_at=%s "
            "WHERE tenant_id=%s AND email=%s",
            (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes),
             tid, email.lower()))
        admin.commit()
    finally:
        admin.close()


class ResendReportsTheSend(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()

    def setUp(self):
        self.client, self.tid, self.owner = _signed_in_client()
        self.guest = _addr()
        r = self.client.post("/api/settings",
                             json={"email_recipients_added": [self.guest]})
        self.assertEqual(r.status_code, 200, r.text)

    def _resend(self):
        r = self.client.post("/api/settings/recipients/resend",
                             json={"email": self.guest})
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertTrue(j["ok"])
        self.assertIn("email_recipient_status", j)
        return j

    def test_resend_inside_the_cooldown_says_nothing_was_sent(self):
        j = self._resend()
        self.assertIs(j["sent"], False)
        self.assertEqual(j["reason"], "cooldown")

    def test_resend_past_the_hourly_cap_says_nothing_was_sent(self):
        _age_last_send(self.tid, self.guest, 11)
        with mock.patch.object(recipient_invites, "_HOURLY_TENANT_CAP", 1):
            j = self._resend()
        self.assertIs(j["sent"], False)
        self.assertEqual(j["reason"], "cap")

    def test_resend_after_the_cooldown_says_it_was_sent(self):
        _age_last_send(self.tid, self.guest, 11)
        j = self._resend()
        self.assertIs(j["sent"], True)
        self.assertNotIn("reason", j)



class ResendReportsTheDelivery(unittest.TestCase):
    """`sent` follows the mail, not the token. Minting a fresh link is not
    the same as delivering it: the recipient can be blocked, the link can
    be unbuildable because OIKONOME_BASE_URL is unset, or the relay can
    refuse. In each of those the owner must be told nothing went out."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        self.email = _addr()
        self.addCleanup(delivery.clear, self.email, reactivate=False)
        p = mock.patch("oikonome.web.app._tenant_smtp",
                       return_value={"host": "relay.example.dev",
                                     "configured": True})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("oikonome.web.app._log_link")
        p.start()
        self.addCleanup(p.stop)
        self.sent: list[list[str]] = []
        p = mock.patch("oikonome.web.report.send",
                       side_effect=lambda *a, **k: self.sent.append(a[3]))
        self.send = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ,
                            {"OIKONOME_BASE_URL": "https://example.dev"})
        p.start()
        self.addCleanup(p.stop)
        self.first = recipient_invites.invite(self.tid, self.email)
        _age_last_send(self.tid, self.email, 11)

    def test_a_relayed_invite_says_it_was_sent(self):
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": True, "reason": "invited"})
        self.assertEqual(self.sent, [[self.email]])

    def test_a_refused_relay_says_undelivered(self):
        self.send.side_effect = RuntimeError("relay down")
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "undelivered"})

    def test_a_blocked_recipient_says_blocked(self):
        with mock.patch("oikonome.web.spamdefense.recipient_blocked",
                        return_value=True):
            got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "blocked"})
        self.assertEqual(self.sent, [])

    def test_an_unbuildable_link_says_no_mail_and_keeps_the_old_link(self):
        """With SMTP configured but no base URL no invitation can be mailed
        at all, so rotating the token would only kill the link the person
        may already hold."""
        with mock.patch.dict(os.environ, {"OIKONOME_BASE_URL": ""}):
            got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "no_mail"})
        self.assertEqual(self.sent, [])
        self.assertIsNotNone(recipient_invites.accept(self.first),
                             "the earlier link must still work")

if __name__ == "__main__":
    unittest.main()
