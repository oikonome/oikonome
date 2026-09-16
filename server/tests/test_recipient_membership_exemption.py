"""A hosted-shape test instance may mail somebody who has no account.

Hosted's rule is that a household's balances only go to addresses holding an
account on the tenant, and that rule is what these tests mostly protect: with
nothing set, a configured non-member is dropped by both the settings door and
the nightly sweep. `OIKONOME_HOSTED_FREEFORM_RECIPIENTS` lifts it, so an
instance that runs the hosted shape for testing can send to a plain mailbox
without an account being created for it — and the two halves must agree, or a
list that saves fine silently mails nobody.

The invite gate is unaffected: an exempted recipient still receives nothing
until the address has accepted.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi import HTTPException

from oikonome.db import tenancy
from oikonome.jobs import worker
from oikonome.web import mailguard

from .util import accept_recipient_invite, make_db, write_config

HOSTED = {"OIKONOME_HOSTED": "1"}
EXEMPT = {"OIKONOME_HOSTED": "1", "OIKONOME_HOSTED_FREEFORM_RECIPIENTS": "1"}
GUEST = "guest@example.dev"


class MembersOnlyFlagTests(unittest.TestCase):

    def test_self_host_never_checks_membership(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OIKONOME_HOSTED", None)
            self.assertFalse(mailguard.members_only())

    def test_hosted_checks_membership(self):
        with mock.patch.dict(os.environ, HOSTED):
            self.assertTrue(mailguard.members_only())

    def test_the_exemption_only_applies_on_hosted(self):
        with mock.patch.dict(os.environ, EXEMPT):
            self.assertFalse(mailguard.members_only())
        with mock.patch.dict(
                os.environ, {"OIKONOME_HOSTED_FREEFORM_RECIPIENTS": "1"}):
            os.environ.pop("OIKONOME_HOSTED", None)
            self.assertFalse(mailguard.members_only())


class SettingsDoorTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_hosted_refuses_a_non_member(self):
        with mock.patch.dict(os.environ, HOSTED):
            with self.assertRaises(HTTPException) as e:
                mailguard.check_recipients(self.conn, [GUEST])
        self.assertEqual(e.exception.status_code, 400)

    def test_the_exemption_accepts_a_non_member(self):
        with mock.patch.dict(os.environ, EXEMPT):
            mailguard.check_recipients(self.conn, [GUEST])
            self.assertEqual(
                mailguard.filter_recipients(self.conn, [GUEST]), [GUEST])

    def test_hosted_drops_a_non_member_from_a_restored_config(self):
        with mock.patch.dict(os.environ, HOSTED):
            self.assertEqual(
                mailguard.filter_recipients(self.conn, [GUEST]), [])


class ScheduledSweepTests(unittest.TestCase):
    """The sweep has to reach the same answer the settings door gave."""

    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        self.owner = f"owner-{uuid.uuid4().hex[:8]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s, %s, 'x', 'owner', now())",
                (self.tid, self.owner))
        finally:
            admin.close()
        write_config(self.conn, email_recipients=[GUEST])
        accept_recipient_invite(self.tid, GUEST)

    def tearDown(self):
        self.conn.close()

    def test_hosted_mails_the_owner_only(self):
        with mock.patch.dict(os.environ, HOSTED):
            got = worker._recipients_raw(self.conn, self.tid)
        self.assertEqual(got, [self.owner])

    def test_the_exemption_mails_the_guest_too(self):
        with mock.patch.dict(os.environ, EXEMPT):
            got = worker._recipients_raw(self.conn, self.tid)
        self.assertEqual(got, [self.owner, GUEST])

    def test_an_exempted_guest_still_waits_for_the_invite(self):
        other = f"nother-{uuid.uuid4().hex[:8]}@example.dev"
        write_config(self.conn, email_recipients=[other])
        with mock.patch.dict(os.environ, EXEMPT):
            got = worker._recipients_raw(self.conn, self.tid)
        self.assertEqual(got, [self.owner])


if __name__ == "__main__":
    unittest.main()
