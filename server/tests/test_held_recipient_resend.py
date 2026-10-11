"""Resend on a held recipient must actually send, and say when it did not.

A recipient who accepted the daily email and whose mail later bounced is
held: the scheduled cadences skip the address until something proves it
works again. The Settings row offers Resend for exactly that case. If the
resend path treats "already accepted" as "nothing to do", the owner is told
the invitation went out while no mail is sent and the hold never lifts, so
the address can never recover.

These tests pin the module contract the resend door relies on:

* an accepted, held address gets a real delivery check, and a check the
  relay accepts lifts the hold;
* a check the relay refuses keeps the hold and says it was not delivered;
* every "nothing was sent" answer carries the reason (cooldown, hourly cap,
  already accepted and healthy, declined, complained), so the caller never
  has to guess;
* `invite()` keeps its old answer for the settings doors.
"""

import unittest
import uuid
from unittest import mock

from oikonome.db import tenancy
from oikonome.notify import delivery, recipient_invites

from .util import TEST_DB, _admin_dsn, _ensure_db, make_db

_SMTP = {"host": "relay.example.dev", "configured": True}


def _addr():
    return f"held-{uuid.uuid4().hex[:8]}@example.dev"


class HeldRecipientResendTests(unittest.TestCase):

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
        self.sent: list[list[str]] = []
        p = mock.patch("oikonome.web.report.send",
                       side_effect=lambda *a, **k: self.sent.append(a[3]))
        self.send = p.start()
        self.addCleanup(p.stop)

    def _accept(self):
        recipient_invites.accept(recipient_invites.invite(self.tid,
                                                          self.email))
        self._age()

    def _age(self):
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(
                "UPDATE recipient_invites SET last_sent_at = now() - "
                "interval '11 minutes' WHERE tenant_id=%s AND email=%s",
                (self.tid, self.email))
        finally:
            admin.close()

    def _bounce(self, state="bouncing"):
        delivery.record_failure(self.email, state=state,
                                bounce_type="HardBounce", reason="test")

    def test_a_held_accepted_recipient_gets_a_check_that_lifts_the_hold(self):
        self._accept()
        self._bounce()
        got = recipient_invites.resend(self.tid, self.email,
                                       inviter="owner@example.dev")
        self.assertEqual(got, {"sent": True, "reason": "delivery_check"})
        self.assertEqual(self.sent, [[self.email]],
                         "a resend to a held address must send real mail")
        self.assertIsNone(delivery.state_for(self.email),
                          "a check the relay accepted is the evidence that "
                          "lifts the hold")
        self.assertEqual(recipient_invites.accepted(self.tid, [self.email]),
                         {self.email}, "consent is untouched by a check")

    def test_a_refused_check_keeps_the_hold_and_says_so(self):
        self._accept()
        self._bounce()
        self.send.side_effect = RuntimeError(
            "(550, b'5.1.1 user unknown')")
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "undelivered"})
        self.assertIsNotNone(delivery.state_for(self.email))

    def test_a_second_check_inside_the_cooldown_sends_nothing(self):
        self._accept()
        self._bounce()
        recipient_invites.resend(self.tid, self.email)
        self._bounce()
        self.sent.clear()
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "cooldown"})
        self.assertEqual(self.sent, [])

    def test_the_hourly_cap_bounds_checks_too(self):
        self._accept()
        self._bounce()
        with mock.patch.object(recipient_invites, "_HOURLY_TENANT_CAP", 0):
            got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "cap"})
        self.assertEqual(self.sent, [])

    def test_a_check_that_hits_a_transient_error_keeps_the_hold(self):
        """A timeout says nothing about the mailbox, so the send path records
        nothing for it. The hold the check lifted to get the mail out must
        therefore come back on its own, or the address's scheduled mail
        resumes to a mailbox that hard-bounced."""
        self._accept()
        self._bounce()
        self.send.side_effect = TimeoutError("timed out")
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "undelivered"})
        st = delivery.state_for(self.email)
        self.assertIsNotNone(st)
        self.assertEqual(st["bounce_type"], "HardBounce")

    def test_an_operators_deactivation_is_not_undone_by_a_resend(self):
        """An address an operator deactivated at the provider, or the
        provider blocked, is a decision the household did not make. Its
        Resend must make no provider call, send nothing, and leave the
        row in place."""
        for btype in sorted(delivery.PROVIDER_DECISIONS):
            with self.subTest(btype=btype):
                self.email = _addr()
                self.addCleanup(delivery.clear, self.email, reactivate=False)
                self._accept()
                delivery.record_failure(self.email, bounce_type=btype,
                                        reason="test", provider_id="123",
                                        suppressed=True)
                self.sent.clear()
                with mock.patch.object(delivery, "_reactivate") as react:
                    got = recipient_invites.resend(self.tid, self.email)
                self.assertEqual(got, {"sent": False, "reason": "blocked"})
                react.assert_not_called()
                self.assertEqual(self.sent, [])
                st = delivery.state_for(self.email)
                self.assertIsNotNone(st)
                self.assertEqual(st["bounce_type"], btype)

    def test_a_typo_bounce_is_still_lifted_upstream(self):
        self._accept()
        delivery.record_failure(self.email, bounce_type="HardBounce",
                                reason="test", provider_id="456",
                                suppressed=True)
        with mock.patch.object(delivery, "_reactivate") as react:
            got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": True, "reason": "delivery_check"})
        react.assert_called_once_with(self.email, "456")

    def test_a_complaint_landing_after_the_first_read_is_not_cleared(self):
        """The complaint read in resend() is not the last word: one arriving
        between that read and the check must still stop the check and
        survive it."""
        self._accept()
        self._bounce(state="complained")
        with mock.patch.object(delivery, "state_for", side_effect=[None]
                               + [delivery.state_for(self.email)] * 5):
            got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "complained"})
        self.assertEqual(self.sent, [])
        self.assertEqual(delivery.state_for(self.email)["state"],
                         "complained")

    def test_a_healthy_accepted_recipient_is_not_mailed(self):
        self._accept()
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "accepted"})
        self.assertEqual(self.sent, [])

    def test_an_address_that_complained_is_never_rechecked(self):
        """A spam complaint is the recipient saying no to the mail itself;
        pressing Resend must not override it."""
        self._accept()
        self._bounce(state="complained")
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "complained"})
        self.assertEqual(self.sent, [])
        self.assertIsNotNone(delivery.state_for(self.email))

    def test_a_later_failure_does_not_downgrade_a_complaint(self):
        """A bounce or a refused send reported after a complaint must leave
        the complaint standing: read as an ordinary bounce, the next Resend
        would clear the row, lift the provider's suppression and mail
        someone who marked the mail as spam."""
        self._accept()
        self._bounce(state="complained")
        delivery.record_failure(self.email, bounce_type="Suppressed",
                                suppressed=True)
        self.assertEqual(delivery.state_for(self.email)["state"],
                         "complained")
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "complained"})
        self.assertEqual(self.sent, [])

    def test_a_declined_recipient_is_not_asked_again(self):
        recipient_invites.decline(recipient_invites.invite(self.tid,
                                                           self.email))
        self._age()
        got = recipient_invites.resend(self.tid, self.email)
        self.assertEqual(got, {"sent": False, "reason": "declined"})

    def test_an_unanswered_invite_is_re_sent_and_cooled_down(self):
        recipient_invites.invite(self.tid, self.email)
        self._age()
        with mock.patch.dict("os.environ",
                             {"OIKONOME_BASE_URL": "https://example.dev"}), \
             mock.patch("oikonome.web.app._deliver_recipient_invite",
                        return_value=(True, "sent")) as d:
            got = recipient_invites.resend(self.tid, self.email)
            self.assertEqual(got, {"sent": True, "reason": "invited"})
            self.assertEqual(d.call_count, 1)
            got = recipient_invites.resend(self.tid, self.email)
            self.assertEqual(got, {"sent": False, "reason": "cooldown"})
            self.assertEqual(d.call_count, 1)

    def test_invite_still_answers_none_for_a_held_accepted_address(self):
        """The settings doors re-add silently; only Resend checks."""
        self._accept()
        self._bounce()
        self.assertIsNone(recipient_invites.invite(self.tid, self.email))
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
