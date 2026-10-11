"""A Resend that gets no mail out must leave the recipient no worse off.

The Resend button rotates the invitation link and mails the new one. Four
ways that can fail without the owner being told the truth, each pinned
here:

* the relay refuses the new mail — the link the person already holds
  must keep working, or the only live link is in the server log;
* the relay SETTINGS cannot be read — that is an error, not an install
  without mail where the logged link counts as delivered;
* a relay exists but no OIKONOME_BASE_URL — the row records the same
  "not arriving, set the base URL" hold the settings save records, so
  the owner sees why beside the "invite sent" badge;
* the delivery check for a held address must never lift the hold on
  speculation: a check that times out after a scheduled send to the same
  address was accepted must not put the hold back.
"""

import os
import unittest
import uuid
from unittest import mock

from oikonome.db import tenancy
from oikonome.notify import delivery, recipient_invites
from oikonome.web import app as webapp

from .util import TEST_DB, _admin_dsn, _ensure_db, make_db

_SMTP = {"host": "relay.example.dev", "configured": True}


def _addr():
    return f"noworse-{uuid.uuid4().hex[:8]}@example.dev"


class _Base(unittest.TestCase):
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
        p = mock.patch("oikonome.web.app._tenant_smtp", return_value=_SMTP)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.dict(os.environ,
                            {"OIKONOME_BASE_URL": "https://example.dev"})
        p.start()
        self.addCleanup(p.stop)
        self.sent: list[list[str]] = []
        p = mock.patch("oikonome.web.report.send",
                       side_effect=lambda *a, **k: self.sent.append(a[3]))
        self.send = p.start()
        self.addCleanup(p.stop)
        old = webapp.DEV_MODE
        webapp.DEV_MODE = False
        self.addCleanup(setattr, webapp, "DEV_MODE", old)

    def _age(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(
                "UPDATE recipient_invites SET last_sent_at = now() - "
                "interval '11 minutes' WHERE tenant_id=%s AND email=%s",
                (self.tid, self.email))
        finally:
            admin.close()


class RefusedResendKeepsTheLinkTests(_Base):

    def test_a_refused_relay_leaves_the_previous_link_working(self):
        first = recipient_invites.invite(self.tid, self.email)
        self._age()
        self.send.side_effect = RuntimeError("(550, b'5.1.1 user unknown')")
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "undelivered"})
        self.assertIsNotNone(recipient_invites.lookup(first),
                             "the mail carrying the new link never left, so "
                             "the old link is the only one anyone holds")
        self.assertIsNotNone(recipient_invites.accept(first))

    def test_a_delivered_resend_still_retires_the_previous_link(self):
        first = recipient_invites.invite(self.tid, self.email)
        self._age()
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": True, "reason": "invited"})
        self.assertIsNone(recipient_invites.accept(first))

    def test_a_refused_first_invite_keeps_its_fresh_link(self):
        # nothing to go back to: the one minted link stays, as logged
        self.send.side_effect = RuntimeError("(550, b'5.1.1 user unknown')")
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "undelivered"})
        st = recipient_invites.states(self.tid, [self.email])
        self.assertEqual(st[self.email]["state"], "invited")

    def test_a_refused_resend_still_counts_against_the_cooldown(self):
        recipient_invites.invite(self.tid, self.email)
        self._age()
        self.send.side_effect = RuntimeError("(550, b'5.1.1 user unknown')")
        recipient_invites.resend(self.tid, self.email)
        self.send.side_effect = None
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "cooldown"},
                         "the relay was asked; a refusing relay is not a "
                         "way around the throttle")


class UnreadableRelaySettingsTests(_Base):

    def test_unreadable_settings_are_an_error_not_a_mailless_install(self):
        first = recipient_invites.invite(self.tid, self.email)
        self._age()
        with mock.patch("oikonome.web.app._tenant_smtp",
                        return_value=webapp.SMTP_UNRESOLVED):
            got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "error"})
        self.assertEqual(self.sent, [])
        self.assertIsNotNone(recipient_invites.accept(first),
                             "nothing was sent, so nothing was rotated")

    def test_a_held_address_gets_no_check_from_unreadable_settings(self):
        recipient_invites.accept(recipient_invites.invite(self.tid,
                                                          self.email))
        self._age()
        delivery.record_failure(self.email, bounce_type="HardBounce",
                                reason="test")
        with mock.patch("oikonome.web.app._tenant_smtp",
                        return_value=webapp.SMTP_UNRESOLVED):
            got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "error"})
        self.assertIsNotNone(delivery.state_for(self.email))


class ResolutionFailureTests(unittest.TestCase):
    """The sentinel comes from a resolution that raised, and every sender
    that meets it reports nothing delivered."""

    def test_a_failed_resolution_answers_the_sentinel(self):
        old = webapp.DEV_MODE
        webapp.DEV_MODE = False
        try:
            with mock.patch.object(webapp.tenancy, "tenant_connect",
                                   side_effect=RuntimeError("db down")):
                smtp = webapp._tenant_smtp("t-1")
        finally:
            webapp.DEV_MODE = old
        self.assertTrue(webapp.smtp_unresolved(smtp))
        self.assertFalse(webapp.smtp_unresolved(None))
        self.assertFalse(webapp.smtp_unresolved({"configured": False}))

    def test_the_invite_sender_logs_the_link_but_claims_no_delivery(self):
        with mock.patch("oikonome.web.app._log_link") as logged, \
             mock.patch("oikonome.web.spamdefense.recipient_blocked",
                        return_value=False):
            got = webapp._deliver_recipient_invite(
                "nobody@example.dev", "tok", "t-1",
                smtp=webapp.SMTP_UNRESOLVED, resolved=True)
        self.assertEqual(got, (False, "error"))
        logged.assert_called_once()


class UnbuildableLinkOnResendTests(_Base):

    def test_resend_without_a_base_url_records_the_hold_and_keeps_the_link(
            self):
        first = recipient_invites.invite(self.tid, self.email)
        self._age()
        with mock.patch.dict(os.environ, {"OIKONOME_BASE_URL": ""}):
            got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "no_mail"})
        self.assertEqual(self.sent, [])
        st = delivery.state_for(self.email)
        self.assertIsNotNone(st, "the row must say why nothing arrives")
        self.assertEqual(st["bounce_type"], "Unconfigured")
        self.assertIn("OIKONOME_BASE_URL", st["reason"])
        self.assertIsNotNone(recipient_invites.accept(first))

    def test_an_owner_address_is_never_recorded(self):
        with mock.patch("oikonome.web.app._is_an_owner_address",
                        return_value=True), \
             mock.patch("oikonome.notify.delivery.record_failure") as rec:
            webapp.record_unbuildable_link(self.email)
        rec.assert_not_called()


class DeliveryCheckLeavesTheHoldAloneTests(_Base):

    def _held(self):
        recipient_invites.accept(recipient_invites.invite(self.tid,
                                                          self.email))
        self._age()
        delivery.record_failure(self.email, bounce_type="HardBounce",
                                reason="test")

    def test_a_timed_out_check_does_not_undo_a_send_that_healed_the_address(
            self):
        self._held()

        def heal_then_time_out(*a, **k):
            # a scheduled send to the same address, accepted by the relay
            # while the check was in flight, lifted the hold itself
            delivery.clear(self.email, reactivate=False)
            raise TimeoutError("timed out")

        self.send.side_effect = heal_then_time_out
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "undelivered"})
        self.assertIsNone(delivery.state_for(self.email),
                          "a hold a successful send lifted must not come "
                          "back because an unrelated check timed out")

    def test_a_timed_out_check_keeps_an_untouched_hold(self):
        self._held()
        self.send.side_effect = TimeoutError("timed out")
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "undelivered"})
        st = delivery.state_for(self.email)
        self.assertIsNotNone(st)
        self.assertEqual(st["bounce_type"], "HardBounce")

    def test_an_accepted_check_lifts_the_hold(self):
        self._held()
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": True, "reason": "delivery_check"})
        self.assertIsNone(delivery.state_for(self.email))

    def test_lifting_the_providers_suppression_is_noted_on_the_row(self):
        recipient_invites.accept(recipient_invites.invite(self.tid,
                                                          self.email))
        self._age()
        delivery.record_failure(self.email, bounce_type="HardBounce",
                                reason="test", provider_id="456",
                                suppressed=True)
        self.send.side_effect = TimeoutError("timed out")
        with mock.patch.object(delivery, "_reactivate") as react:
            recipient_invites.resend(self.tid, self.email)
        react.assert_called_once_with(self.email, "456")
        st = delivery.state_for(self.email)
        self.assertIsNotNone(st, "the hold stays until a send proves the "
                                 "mailbox works")
        self.assertFalse(st["suppressed"],
                         "the row says what the provider now says")


if __name__ == "__main__":
    unittest.main()
