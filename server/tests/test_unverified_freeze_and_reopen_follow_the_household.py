"""The never-confirmed freeze is a HOUSEHOLD's, in its dates and its exit.

* The freeze date is decided on the household's first signup, and the
  app's own nudge shows that date. The day-7 reminder used to compute it
  from each person's OWN signup, so a member who joined on day 10 was told
  they had until day 40 when the household froze on day 30.
* Reopening a frozen household used to write its stored prior status
  straight back. The installed gate is the one that knows where a tenant
  lands when it comes back — a trial that ran out during the freeze lands
  in its lockout, not in free access — so the reopen asks it, the way the
  console's reactivation does.
"""

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from oikonome import ext
from oikonome.db import tenancy
from oikonome.jobs import unverified

from .util import _ensure_db


def _admin():
    return tenancy.admin_connect()


class HouseholdDatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            "OIKONOME_HOSTED": "1",
            "OIKONOME_BASE_URL": "https://app.example.test",
            "OIKONOME_UNVERIFIED_REAP_DAYS": "30"})
        self._env.start()
        self.addCleanup(self._env.stop)
        self.sent: list[tuple[str, str]] = []
        p = mock.patch.object(
            unverified.account_mail, "send",
            side_effect=lambda email, subject, plain, html, **kw:
                self.sent.append((email, plain)) or True)
        p.start()
        self.addCleanup(p.stop)

    def test_a_late_member_is_told_the_households_freeze_date(self):
        admin = _admin()
        try:
            tid = tenancy.create_tenant(admin, f"hh-{uuid.uuid4().hex[:8]}")
            u = uuid.uuid4().hex[:8]
            owner, member = f"own-{u}@x.dev", f"mem-{u}@x.dev"
            since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=17)
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "created_at) VALUES (%s,%s,'x','owner',%s)",
                (tid, owner, since))
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "created_at) VALUES (%s,%s,'x','viewer',%s)",
                (tid, member, since + dt.timedelta(days=9)))
            household_date = unverified.deadline(admin, tid)
        finally:
            admin.close()
        unverified.sweep()
        want = unverified.notice_date(household_date)
        mail = {e: plain for e, plain in self.sent if e in (owner, member)}
        self.assertEqual(set(mail), {owner, member})
        for e, plain in mail.items():
            self.assertIn(f"frozen on {want}", plain,
                          f"{e} must be given the household's date")


class ReopenAsksTheGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def _frozen(self):
        admin = _admin()
        try:
            tid = tenancy.create_tenant(admin, f"fz-{uuid.uuid4().hex[:8]}")
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,'x','owner',now()) RETURNING id",
                (tid, f"fz-{uuid.uuid4().hex[:8]}@x.dev")).fetchone()["id"]
            admin.execute(
                "UPDATE tenants SET status=%s, status_before_delete='active', "
                "delete_after = now() + interval '7 days' WHERE id=%s",
                (unverified.STATUS, tid))
        finally:
            admin.close()
        return str(tid), uid

    def _status(self, tid):
        admin = _admin()
        try:
            return admin.execute(
                "SELECT status, delete_after, status_before_delete "
                "FROM tenants WHERE id=%s", (tid,)).fetchone()
        finally:
            admin.close()

    def test_the_gate_decides_where_a_reopened_household_lands(self):
        tid, uid = self._frozen()
        asked = []

        def land_in_lockout(admin, tenant_id):
            # an add-on whose trial ran out during the freeze
            asked.append(str(tenant_id))
            admin.execute("UPDATE tenants SET status='suspended' "
                          "WHERE id=%s", (tenant_id,))
            return "suspended"
        admin = _admin()
        try:
            with mock.patch.object(ext.gate, "reactivate",
                                   side_effect=land_in_lockout):
                self.assertEqual(unverified.reopen(admin, uid), tid)
        finally:
            admin.close()
        self.assertEqual(asked, [tid])
        row = self._status(tid)
        self.assertEqual(row["status"], "suspended")
        self.assertIsNone(row["delete_after"])
        self.assertIsNone(row["status_before_delete"])

    def test_with_no_gate_installed_the_household_is_active_again(self):
        tid, uid = self._frozen()
        admin = _admin()
        try:
            self.assertEqual(unverified.reopen(admin, uid), tid)
        finally:
            admin.close()
        row = self._status(tid)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["delete_after"])

    def test_an_unavailable_gate_leaves_the_household_frozen_not_a_crash(self):
        """The fail-closed gate refuses to say where the household lands.
        The reopen reports nothing reopened, writes nothing, and the
        household stays frozen with its window intact — the page offers
        the button again instead of an error."""
        tid, uid = self._frozen()
        before = self._status(tid)
        admin = _admin()
        try:
            with mock.patch.object(ext, "gate", ext._FailClosedGate()):
                self.assertIsNone(unverified.reopen(admin, uid))
        finally:
            admin.close()
        self.assertEqual(dict(self._status(tid)), dict(before))


if __name__ == "__main__":
    unittest.main()
