"""Another household's delivery record for an address is not shown in
detail.

`email_delivery_state` is keyed on the address, instance-wide: on a box
with several households, the bounce or complaint it records may have come
from another household's mail. A household that merely typed the address
into its recipient list learns only that mail to it is held — not whether
it was a complaint, nor the provider's own words about the mailbox. Its
own accounts, and recipients who accepted its invitation, keep the full
record; so does every address on a single-household box.
"""
import unittest
import uuid
from unittest import mock

from oikonome.db import tenancy
from oikonome.notify import delivery

from .util import make_db


def _addr():
    return f"held-{uuid.uuid4().hex[:8]}@example.dev"


class DeliveryStateStaysWithItsHousehold(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_db().close()

    def setUp(self):
        admin = tenancy.admin_connect()
        try:
            self.tid_a = tenancy.create_tenant(admin, f"a-{uuid.uuid4().hex[:6]}")
            self.tid_b = tenancy.create_tenant(admin, f"b-{uuid.uuid4().hex[:6]}")
        finally:
            admin.close()
        self.x = _addr()
        delivery.record_failure(self.x, state="complained",
                                bounce_type="SpamComplaint",
                                reason="marked as spam")

    def tearDown(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM email_delivery_state WHERE email=%s",
                          (self.x,))
        finally:
            admin.close()

    def _row(self, tid):
        from oikonome.web.api import _recipient_status
        return next(r for r in _recipient_status(tid, [self.x])
                    if r["email"] == self.x)

    def test_a_household_without_standing_sees_only_that_it_is_held(self):
        row = self._row(self.tid_a)
        self.assertEqual(row["delivery"], "bouncing")
        self.assertIsNone(row["delivery_reason"])

    def test_the_household_the_address_belongs_to_sees_the_record(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'member')", (self.tid_b, self.x))
        finally:
            admin.close()
        row = self._row(self.tid_b)
        self.assertEqual(row["delivery"], "complained")
        self.assertEqual(row["delivery_reason"], "marked as spam")
        # and the other household still sees only the hold
        self.assertIsNone(self._row(self.tid_a)["delivery_reason"])

    def test_a_recipient_who_accepted_sees_through_to_the_record(self):
        from oikonome.notify import recipient_invites
        with mock.patch.object(
                recipient_invites, "states",
                return_value={self.x: {"state": "accepted",
                                       "invited_at": None}}):
            row = self._row(self.tid_a)
        self.assertEqual(row["delivery"], "complained")

    def test_a_single_household_box_shows_everything(self):
        with mock.patch("oikonome.web.pages._tenant_count", return_value=1):
            row = self._row(self.tid_a)
        self.assertEqual(row["delivery"], "complained")
        self.assertEqual(row["delivery_reason"], "marked as spam")


if __name__ == "__main__":
    unittest.main()
