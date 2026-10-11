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

    def test_the_restore_door_refuses_a_household_the_purge_has_claimed(self):
        """Once the nightly purge has claimed a household it releases its
        bank links and billing without holding the row; a restore then
        would hand back an account whose connections are already gone."""
        _freeze(self.tid, "pending_delete")
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenants SET purge_claimed_at = now() "
                          "WHERE id=%s", (self.tid,))
        finally:
            admin.close()
        r = self.client.post("/admin/console/tenant-restore",
                             data={"tenant_id": self.tid})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("Deletion in progress", r.text)
        self.assertEqual(_tenant_row(self.tid)["status"], "pending_delete")
        # the claim given up, the same door works again
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenants SET purge_claimed_at = NULL "
                          "WHERE id=%s", (self.tid,))
        finally:
            admin.close()
        r = self.client.post("/admin/console/tenant-restore",
                             data={"tenant_id": self.tid})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(_tenant_row(self.tid)["status"], "active")

    def test_a_claim_left_by_a_killed_purge_does_not_lock_the_door(self):
        """A worker killed mid-purge leaves its claim behind, and nothing
        else clears it. One far older than any purge takes is given up:
        the restore goes through, and the Items that purge had already
        removed at Plaid come back archived rather than looking live."""
        _freeze(self.tid, "pending_delete")
        gone, live = (f"it-{uuid.uuid4().hex[:8]}" for _ in range(2))
        conn = tenancy.tenant_connect(self.tid)
        try:
            for iid in (gone, live):
                conn.execute(
                    "INSERT INTO items (id, aggregator, institution_name, "
                    "access_token) VALUES (%s,'plaid','Test Bank','tok')",
                    (iid,))
        finally:
            conn.close()
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenants SET purge_claimed_at = "
                          "now() - interval '7 hours' WHERE id=%s",
                          (self.tid,))
            for iid in (gone, live):
                admin.execute("INSERT INTO plaid_item_ledger (item_id) "
                              "VALUES (%s)", (iid,))
            admin.execute("UPDATE plaid_item_ledger SET removed_at=now() "
                          "WHERE item_id=%s", (gone,))
        finally:
            admin.close()
        r = self.client.post("/admin/console/tenant-restore",
                             data={"tenant_id": self.tid})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(_tenant_row(self.tid)["status"], "active")
        conn = tenancy.tenant_connect(self.tid)
        try:
            rows = {r["id"]: r for r in conn.execute(
                "SELECT id, status, archived_reason, access_token "
                "FROM items").fetchall()}
        finally:
            conn.close()
        self.assertEqual(rows[gone]["status"], "archived")
        self.assertEqual(rows[gone]["archived_reason"], "plaid-gone")
        self.assertIsNone(rows[gone]["access_token"])
        self.assertNotEqual(rows[live]["status"], "archived")
        self.assertIsNotNone(rows[live]["access_token"])

    def test_a_fresh_claim_still_refuses(self):
        _freeze(self.tid, "pending_delete")
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE tenants SET purge_claimed_at = "
                          "now() - interval '1 hour' WHERE id=%s",
                          (self.tid,))
        finally:
            admin.close()
        r = self.client.post("/admin/console/tenant-restore",
                             data={"tenant_id": self.tid})
        self.assertEqual(r.status_code, 409, r.text)


if __name__ == "__main__":
    unittest.main()
