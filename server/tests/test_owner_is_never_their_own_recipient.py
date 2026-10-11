"""The owner is mailed by construction and is never invited to their own
household's mail.

Typing the owner's own address into an add box is refused: it can only
mean the person expects to start receiving mail they already get, and the
invitation it would trigger is where a delivery failure gets recorded
against an address. A whole list that includes the owner is a different
statement and is accepted — a configured list means "only these people",
so `[owner]` keeps the daily mail to the owner alone — but it invites
nobody, and neither does any other door.

Separately, an invitation that could not be built because the instance has
no pinned base URL is a configuration problem, not a mailbox problem. The
delivery record is shared by every send to that address, so writing one
against an owner would hold the owner's own scheduled mail, which needs no
link at all.
"""

import os
import unittest
from unittest import mock

from oikonome.db import tenancy

from .test_recipient_invites import _addr, _row, _signed_in_client
from .util import _ensure_db


def _delivery_row(email):
    admin = tenancy.admin_connect()
    try:
        return admin.execute(
            "SELECT * FROM email_delivery_state WHERE lower(email)=lower(%s)",
            (email,)).fetchone()
    finally:
        admin.close()


def _stored(tid) -> dict:
    admin = tenancy.admin_connect()
    try:
        row = admin.execute("SELECT config FROM tenant_settings "
                            "WHERE tenant_id=%s", (tid,)).fetchone()
    finally:
        admin.close()
    return dict(row["config"] or {}) if row else {}


class OwnerAddressIsRefused(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()

    def setUp(self):
        self.client, self.tid, self.owner = _signed_in_client()

    def _assert_not_stored_or_invited(self):
        self.assertNotIn(self.owner.lower(),
                         str(_stored(self.tid).get("email_recipients"))
                         .lower())
        self.assertIsNone(_row(self.tid, self.owner))

    def test_delta_add_of_the_owners_address_is_refused(self):
        r = self.client.post("/api/settings", json={
            "email_recipients_added": [self.owner.upper()]})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("you", r.json()["detail"].lower())
        self._assert_not_stored_or_invited()

    def test_delta_add_alongside_a_whole_list_is_refused_too(self):
        """A phone sends its add as the delta beside the whole list, and
        the delta is what the save applies."""
        r = self.client.post("/api/settings", json={
            "email_recipients": [self.owner],
            "email_recipients_added": [self.owner]})
        self.assertEqual(r.status_code, 400, r.text)
        self._assert_not_stored_or_invited()

    def test_whole_list_naming_the_owner_saves_but_invites_nobody(self):
        r = self.client.post("/api/settings", json={
            "email_recipients": [self.owner]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(self.owner.lower(),
                      str(_stored(self.tid).get("email_recipients")).lower())
        self.assertIsNone(_row(self.tid, self.owner))

    def test_form_door_naming_the_owner_invites_nobody(self):
        r = self.client.post("/settings/email",
                             data={"email_recipients": self.owner,
                                   "email_send_hour_utc": 14},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertIsNone(_row(self.tid, self.owner))

    def test_a_list_already_holding_the_owner_still_saves(self):
        from .util import write_config
        conn = tenancy.tenant_connect(self.tid)
        try:
            write_config(conn, email_recipients=[self.owner])
        finally:
            conn.close()
        guest = _addr()
        r = self.client.post("/api/settings", json={
            "email_recipients": [self.owner, guest], "food_monthly": 77})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(_stored(self.tid).get("food_monthly"), 77)
        self.assertIsNone(_row(self.tid, self.owner))

    def test_resend_to_the_owner_is_refused(self):
        from .util import write_config
        conn = tenancy.tenant_connect(self.tid)
        try:
            write_config(conn, email_recipients=[self.owner])
        finally:
            conn.close()
        r = self.client.post("/api/settings/recipients/resend",
                             json={"email": self.owner})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNone(_row(self.tid, self.owner))

    def test_the_invite_fan_out_never_invites_an_owner(self):
        from oikonome.web import mailguard
        me = self.client.get("/api/me").json()
        mailguard.invite_added({"tenant_id": self.tid,
                                "user_id": me.get("user_id"),
                                "email": self.owner}, [self.owner])
        self.assertIsNone(_row(self.tid, self.owner))


class UnbuildableInviteNeverHoldsAnOwner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()

    def setUp(self):
        self.client, self.tid, self.owner = _signed_in_client()

    def _deliver(self, email):
        import oikonome.web.app as appmod
        with mock.patch.dict(os.environ, {"OIKONOME_BASE_URL": ""}), \
             mock.patch.object(appmod, "_log_link"):
            appmod._deliver_recipient_invite(
                email, "tok", self.tid, None,
                smtp={"configured": True}, resolved=True)

    def test_no_base_url_records_nothing_against_an_owner(self):
        self._deliver(self.owner)
        self.assertIsNone(_delivery_row(self.owner))

    def test_no_base_url_is_still_recorded_for_an_invitee(self):
        guest = _addr()
        self._deliver(guest)
        row = _delivery_row(guest)
        self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
