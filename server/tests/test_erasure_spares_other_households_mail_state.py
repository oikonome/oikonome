"""Erasing one household never erases another household's mail verdicts.

The bounce and complaint record is keyed by address, not household: it is
what holds back scheduled mail to a mailbox that bounced or reported mail
as spam, for every household that mails it. An erasure sweeps the record by
address, so the addresses it sweeps must be ones the erased household
actually mailed, and none another household still mails. A spam complaint
on someone else's address stands: only the person who made it can take it
back, and a household that names the address must not be able to.
"""
import io
import unittest
import uuid
import zipfile

from oikonome.db import tenancy
from oikonome.notify import delivery, recipient_invites
from oikonome.sync import export, restore

from .util import TEST_DB, _admin_dsn, _ensure_db, make_db


def _addr(tag: str) -> str:
    return f"keep-{tag}-{uuid.uuid4().hex[:8]}@example.test"


def _admin():
    return tenancy.admin_connect(_admin_dsn(TEST_DB))


class ErasureSparesOthersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = _admin()
        self.addCleanup(self.admin.close)
        self.tid = tenancy.create_tenant(
            self.admin, f"era-{uuid.uuid4().hex[:8]}")
        self.owner = _addr("own")
        self.admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash)"
            " VALUES (%s, %s, 'x')", (self.tid, self.owner))
        self.seen: list[str] = []

    def tearDown(self):
        for e in self.seen:
            self.admin.execute(
                "DELETE FROM email_delivery_state WHERE email=%s", (e,))

    def _complained(self, email):
        self.seen.append(email)
        delivery.record_failure(email, state="complained",
                                bounce_type="SpamComplaint")
        self.assertEqual(delivery.state_for(email)["state"], "complained")

    def _planted_decline(self, email):
        """What a restore archive leaves: a decline nobody here mailed."""
        self.admin.execute(
            "INSERT INTO recipient_invites (tenant_id, email, declined_at)"
            " VALUES (%s, %s, now())", (self.tid, email))

    def _erase(self):
        tenancy.delete_tenant_rows(self.admin, self.tid)

    def test_a_planted_decline_cannot_erase_a_complaint(self):
        victim = _addr("victim")
        self._complained(victim)
        self._planted_decline(victim)
        self._erase()
        self.assertEqual(delivery.state_for(victim)["state"], "complained")

    def test_an_address_another_household_mails_keeps_its_record(self):
        victim = _addr("shared")
        other = tenancy.create_tenant(
            self.admin, f"era-b-{uuid.uuid4().hex[:8]}")
        self.addCleanup(tenancy.delete_tenant_rows, _admin(), other)
        recipient_invites.invite(other, victim)
        recipient_invites.invite(self.tid, victim)
        self.seen.append(victim)
        delivery.record_send_failure(victim, "550 5.1.1 unknown user")
        self._erase()
        self.assertIsNotNone(delivery.state_for(victim))

    def test_another_households_unsent_invite_does_not_keep_the_record(self):
        """An invitation another household only read out of a restore
        archive, and never sent, is no claim on the address's record: this
        household's own bounce row for it goes with this household."""
        victim = _addr("unsent")
        other = tenancy.create_tenant(
            self.admin, f"era-c-{uuid.uuid4().hex[:8]}")
        self.addCleanup(tenancy.delete_tenant_rows, _admin(), other)
        self.admin.execute(
            "INSERT INTO recipient_invites (tenant_id, email, declined_at)"
            " VALUES (%s, %s, now())", (other, victim))
        recipient_invites.invite(self.tid, victim)
        self.seen.append(victim)
        delivery.record_send_failure(victim, "550 5.1.1 unknown user")
        self._erase()
        self.assertIsNone(delivery.state_for(victim))

    def test_a_named_recipient_cannot_erase_a_complaint(self):
        """Free-form recipients in the settings name an address; they do not
        make its complaint this household's to withdraw."""
        from oikonome.engine.compat import jsonb
        victim = _addr("named")
        self._complained(victim)
        self.admin.execute(
            "INSERT INTO tenant_settings (tenant_id, config) VALUES (%s, %s)"
            " ON CONFLICT (tenant_id) DO UPDATE SET config = EXCLUDED.config",
            (self.tid, jsonb({"email_recipients": [victim]})))
        self._erase()
        self.assertEqual(delivery.state_for(victim)["state"], "complained")

    def test_the_households_own_records_still_go(self):
        self._complained(self.owner)
        self._erase()
        self.assertIsNone(delivery.state_for(self.owner))


class RestoredDeclineAddressTests(unittest.TestCase):
    def setUp(self):
        self.src = make_db()
        self.dest = make_db()
        self.addCleanup(self.src.close)
        self.addCleanup(self.dest.close)

    def test_a_restored_decline_must_be_one_real_mailbox(self):
        data = export.build_zip(self.src)
        src = zipfile.ZipFile(io.BytesIO(data))
        buf = io.BytesIO()
        body = ("email,invited_by_email,created_at,expires_at,accepted_at,"
                "declined_at,last_sent_at,send_count\r\n"
                "\"a@x.test, b@y.test\",,,,,2026-01-02T00:00:00+00:00,,0\r\n"
                "not-an-address,,,,,2026-01-02T00:00:00+00:00,,0\r\n")
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
            for item in src.infolist():
                out.writestr(item.filename,
                             body if item.filename == "recipient_invites.csv"
                             else src.read(item))
            if "recipient_invites.csv" not in src.namelist():
                out.writestr("recipient_invites.csv", body)
        restore.restore_zip(self.dest, buf.getvalue())
        tid = self.dest.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
        admin = _admin()
        try:
            n = admin.execute(
                "SELECT count(*) AS n FROM recipient_invites"
                " WHERE tenant_id = %s", (tid,)).fetchone()["n"]
        finally:
            admin.close()
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
