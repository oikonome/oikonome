"""A household frozen with a deletion date is only ever lifted by the
restore door.

The operator's suspend/reactivate lever writes tenants.status; the freeze,
the restore and the nightly purge write status TOGETHER WITH delete_after
and status_before_delete, under a row lock. If the lever writes over a
frozen status, the pair is stranded: the purge selects by status, so a
household on its way to erasure sits frozen for ever with a date nothing
reads, and a reactivate leaves a "restore me to" value behind that the
restore door now never sees. So the lever refuses a frozen row and names
the door that unwinds it, and it takes the same row lock to decide.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.jobs import unverified

from .util import _ensure_db

TOKEN = "test-admin-token-" + "f" * 32


def _tenant_row(tid):
    admin = tenancy.admin_connect()
    try:
        return admin.execute(
            "SELECT status, delete_after, status_before_delete "
            "FROM tenants WHERE id=%s", (tid,)).fetchone()
    finally:
        admin.close()


def _freeze(tid, status):
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "UPDATE tenants SET status=%s, status_before_delete='active', "
            "delete_after = now() + interval '7 days' WHERE id=%s",
            (status, tid))
    finally:
        admin.close()


class StatusLeverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        os.environ["OIKONOME_ADMIN_TOKEN"] = TOKEN
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/admin/console/login", data={"token": TOKEN},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        admin = tenancy.admin_connect()
        try:
            self.tid = str(tenancy.create_tenant(
                admin, f"lever-{uuid.uuid4().hex[:8]}"))
        finally:
            admin.close()

    def tearDown(self):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)

    def _post(self, to):
        r = self.client.post("/admin/console/tenant-status",
                             data={"tenant_id": self.tid, "to": to})
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def test_the_lever_still_suspends_and_reactivates_an_ordinary_tenant(self):
        self._post("suspended")
        self.assertEqual(_tenant_row(self.tid)["status"], "suspended")
        self._post("active")
        row = _tenant_row(self.tid)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["delete_after"])

    def test_suspending_a_frozen_household_is_refused_not_half_done(self):
        """Suspending over the freeze would lose the purge AND the restore
        path: the status that selects the row for both is gone, and the
        date it was to be erased on stays behind."""
        _freeze(self.tid, unverified.STATUS)
        r = self._post("suspended")
        self.assertIn("Restore it first", r.text)
        row = _tenant_row(self.tid)
        self.assertEqual(row["status"], unverified.STATUS)
        self.assertIsNotNone(row["delete_after"])
        self.assertEqual(row["status_before_delete"], "active")

    def test_activating_a_frozen_household_is_refused_not_half_done(self):
        """Reactivating by hand lifts the lockout but strands the deletion
        date and the remembered previous status — the restore door is what
        clears them."""
        _freeze(self.tid, "pending_delete")
        r = self._post("active")
        self.assertIn("Restore it first", r.text)
        row = _tenant_row(self.tid)
        self.assertEqual(row["status"], "pending_delete")
        self.assertIsNotNone(row["delete_after"])

    def test_a_refusal_writes_no_audit_row(self):
        _freeze(self.tid, unverified.STATUS)
        self._post("suspended")
        admin = tenancy.admin_connect()
        try:
            acts = [a["action"] for a in admin.execute(
                "SELECT action FROM admin_audit WHERE target=%s",
                (self.tid,)).fetchall()]
        finally:
            admin.close()
        self.assertNotIn("tenant_suspend", acts)

    def test_the_console_offers_no_status_lever_on_a_frozen_tenant(self):
        """A button the server refuses is a button the operator should
        not be reaching for: the tenant page draws the restore door
        instead, which is the one that clears the deletion date."""
        page = self.client.get(f"/admin/console/tenant/{self.tid}")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('name="to" value="suspended"', page.text)
        _freeze(self.tid, unverified.STATUS)
        page = self.client.get(f"/admin/console/tenant/{self.tid}")
        self.assertEqual(page.status_code, 200, page.text)
        self.assertNotIn('action="/admin/console/tenant-status"', page.text)
        self.assertIn('action="/admin/console/tenant-restore"', page.text)

    def test_the_restore_door_is_the_way_out_of_a_freeze(self):
        """What the refusal points at has to work: restore lifts the
        status AND clears the pair."""
        _freeze(self.tid, unverified.STATUS)
        r = self.client.post("/admin/console/tenant-restore",
                             data={"tenant_id": self.tid})
        self.assertEqual(r.status_code, 200, r.text)
        row = _tenant_row(self.tid)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["delete_after"])
        self.assertIsNone(row["status_before_delete"])


if __name__ == "__main__":
    unittest.main()
