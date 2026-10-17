"""Resend does not tell a household that an address it has no standing
with marked someone's mail as spam.

The delivery record is keyed on the address, instance-wide: on a box with
several households, the complaint may have come from another household's
mail. The recipient status view already shows such a household only that
mail to the address is held; Resend's answer must not say more. A
household's own account, or a recipient who accepted its invitation,
keeps the full answer. Either way nothing is sent.
"""
import unittest
import uuid
from unittest import mock

from oikonome.db import tenancy
from oikonome.notify import delivery, recipient_invites

from .util import make_db

_SMTP = {"host": "relay.example.dev", "configured": True}


class ResendHidesAnotherHouseholdsComplaint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_db().close()

    def setUp(self):
        admin = tenancy.admin_connect()
        try:
            self.tid_a = tenancy.create_tenant(admin,
                                               f"ra-{uuid.uuid4().hex[:6]}")
            self.tid_b = tenancy.create_tenant(admin,
                                               f"rb-{uuid.uuid4().hex[:6]}")
        finally:
            admin.close()
        self.x = f"spam-{uuid.uuid4().hex[:8]}@example.dev"
        delivery.record_failure(self.x, state="complained",
                                bounce_type="SpamComplaint",
                                reason="marked as spam")
        self.addCleanup(delivery.clear, self.x, reactivate=False)
        p = mock.patch("oikonome.web.app._tenant_smtp", return_value=_SMTP)
        p.start()
        self.addCleanup(p.stop)
        self.sent = []
        p = mock.patch("oikonome.web.report.send",
                       side_effect=lambda *a, **k: self.sent.append(a[3]))
        p.start()
        self.addCleanup(p.stop)

    def test_a_household_without_standing_is_told_only_held(self):
        got = recipient_invites.resend(self.tid_a, self.x)
        self.assertEqual(got, {"sent": False, "reason": "held"})
        self.assertEqual(self.sent, [])

    def test_the_household_the_address_belongs_to_hears_complained(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'member')", (self.tid_b, self.x))
        finally:
            admin.close()
        self.assertEqual(recipient_invites.resend(self.tid_b, self.x),
                         {"sent": False, "reason": "complained"})
        # and the other household still hears only that it is held
        self.assertEqual(recipient_invites.resend(self.tid_a, self.x),
                         {"sent": False, "reason": "held"})
        self.assertEqual(self.sent, [])

    def test_a_recipient_who_accepted_this_households_invite_shows_through(
            self):
        recipient_invites.accept(recipient_invites.invite(self.tid_b, self.x))
        self.assertEqual(recipient_invites.resend(self.tid_b, self.x),
                         {"sent": False, "reason": "complained"})
        self.assertEqual(recipient_invites.resend(self.tid_a, self.x),
                         {"sent": False, "reason": "held"})


if __name__ == "__main__":
    unittest.main()
