"""The recipient grid and Resend answer alike about another household's
delivery record.

Both surfaces decide, for an instance-wide delivery record, whether this
household may see its detail (a complaint, the provider's words) or only
that its mail to the address is held. Two copies of that rule drift; this
pins the two surfaces to the same answer in every case that decides it:
no standing on a multi-household box, an account of the household, a
recipient who accepted its invitation, and a single-household box.
"""
import unittest
import uuid
from unittest import mock

from oikonome.db import tenancy
from oikonome.notify import delivery, recipient_invites

from .util import make_db


class DeliveryDetailFollowsOneRule(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        make_db().close()

    def setUp(self):
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(admin, f"r-{uuid.uuid4().hex[:6]}")
            self.other = tenancy.create_tenant(
                admin, f"o-{uuid.uuid4().hex[:6]}")
        finally:
            admin.close()
        self.x = f"rule-{uuid.uuid4().hex[:8]}@example.dev"
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

    def _admin(self, sql, args):
        admin = tenancy.admin_connect()
        try:
            admin.execute(sql, args)
        finally:
            admin.close()

    def _answers(self):
        from oikonome.web.api import _recipient_status
        row = next(r for r in _recipient_status(self.tid, [self.x])
                   if r["email"] == self.x)
        grid_sees = row["delivery_reason"] is not None
        resend_sees = (recipient_invites._complaint_reason(self.tid, self.x)
                       == "complained")
        return grid_sees, resend_sees

    def test_no_standing_on_a_shared_box_hides_the_detail_on_both(self):
        self.assertEqual(self._answers(), (False, False))

    def test_an_account_of_the_household_sees_it_on_both(self):
        self._admin("INSERT INTO users (tenant_id, email, password_hash, "
                    "role) VALUES (%s, %s, 'x', 'member')", (self.tid, self.x))
        self.assertEqual(self._answers(), (True, True))

    def test_an_accepted_recipient_sees_it_on_both(self):
        self._admin("INSERT INTO recipient_invites (tenant_id, email, "
                    "accepted_at) VALUES (%s, %s, now())", (self.tid, self.x))
        self.assertEqual(self._answers(), (True, True))

    def test_another_households_standing_does_not_count_on_either(self):
        self._admin("INSERT INTO recipient_invites (tenant_id, email, "
                    "accepted_at) VALUES (%s, %s, now())",
                    (self.other, self.x))
        self.assertEqual(self._answers(), (False, False))

    def test_a_single_household_box_shows_it_on_both(self):
        with mock.patch("oikonome.web.pages._tenant_count", return_value=1):
            self.assertEqual(self._answers(), (True, True))

    def test_unable_to_tell_discloses_nothing(self):
        with mock.patch("oikonome.web.pages._tenant_count",
                        side_effect=RuntimeError("down")):
            self.assertFalse(recipient_invites.delivery_detail_visible(
                self.tid, self.x))

    def test_unable_to_count_households_is_logged_and_hides_the_detail(self):
        with mock.patch("oikonome.web.pages._tenant_count",
                        side_effect=RuntimeError("down")), \
                self.assertLogs("oikonome.recipient_invites", "ERROR"):
            self.assertEqual(self._answers(), (False, False))

    def test_a_grid_of_failing_addresses_counts_households_once(self):
        extra = [f"rule-{uuid.uuid4().hex[:8]}@example.dev" for _ in range(3)]
        self.addCleanup(self._admin,
                        "DELETE FROM email_delivery_state WHERE email = ANY(%s)",
                        (extra,))
        for e in extra:
            delivery.record_failure(e, state="bouncing", bounce_type="HardBounce",
                                    reason="no such mailbox")
        from oikonome.web.api import _recipient_status
        with mock.patch("oikonome.web.pages._tenant_count",
                        return_value=2) as count:
            rows = _recipient_status(self.tid, [self.x, *extra])
        self.assertEqual(count.call_count, 1)
        hidden = {r["email"] for r in rows if r["email"] in {self.x, *extra}
                  and r["delivery"] and r["delivery_reason"] is None}
        self.assertEqual(hidden, {self.x, *extra})


if __name__ == "__main__":
    unittest.main()
