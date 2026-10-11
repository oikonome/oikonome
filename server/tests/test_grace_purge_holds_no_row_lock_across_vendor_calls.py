"""The nightly grace purge must not hold the tenants row across vendor calls,
and must not erase a household while the billing add-on is unavailable.

* FOR UPDATE on the tenants row conflicts with the FOR KEY SHARE every
  insert into a table keyed to it takes — a sign-in's session row, a phone's
  device token. Held across the Plaid, MX and add-on releases (minutes, at
  their timeouts), those requests park on the few control-pool connections
  every request on the instance shares.
* The pending-release marker is the retry handle for a release that did not
  land. It is committed on its own, so a failure after the irreversible
  release still leaves it behind.
* The claim is a stamp of its own. Status and the remembered prior status
  say whether anybody asked for the deletion, and a payment or gift that
  arrives while the links are released must still be able to lift a
  freeze nobody asked for; such a household is kept, never erased.
* With the add-on not loaded, nothing can archive or release its billing
  record, and erasing the household would take the only copy of it. Then
  nothing is released and nothing deleted.
"""

import unittest
from unittest import mock

from oikonome import erasure, ext
from oikonome.db import tenancy
from oikonome.jobs import unverified, worker

from .util import make_db

_RELEASED = {"plaid_items": 0, "released": "none", "mx_user": "none"}


def _due_tenant(status: str = "pending_delete") -> str:
    conn = make_db()
    try:
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    finally:
        conn.close()
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "UPDATE tenants SET status=%s, "
            "status_before_delete = CASE WHEN %s::text = 'pending_delete' "
            "  THEN NULL ELSE 'active' END, "
            "delete_after = now() - interval '1 hour' WHERE id=%s",
            (status, status, tid))
    finally:
        admin.close()
    return str(tid)


def _row(tid):
    probe = tenancy.admin_connect()
    try:
        return probe.execute(
            "SELECT status, status_before_delete, delete_after, "
            "purge_claimed_at FROM tenants WHERE id=%s", (tid,)).fetchone()
    finally:
        probe.close()


def _exists(tid) -> bool:
    probe = tenancy.admin_connect()
    try:
        return probe.execute("SELECT 1 FROM tenants WHERE id=%s",
                             (tid,)).fetchone() is not None
    finally:
        probe.close()


class GracePurgeLockTests(unittest.TestCase):

    def test_a_sign_in_is_not_blocked_while_links_are_released(self):
        tid = _due_tenant()
        seen = []

        def release(tenant_id):
            if str(tenant_id) == tid:
                probe = tenancy.admin_connect()
                try:
                    # what INSERT INTO sessions takes on its parent row
                    with probe.transaction():
                        probe.execute("SET LOCAL lock_timeout = '300ms'")
                        probe.execute(
                            "SELECT 1 FROM tenants WHERE id=%s "
                            "FOR KEY SHARE", (tid,))
                    seen.append(True)
                except Exception:                    # noqa: BLE001
                    seen.append(False)
                finally:
                    probe.close()
            return dict(_RELEASED)

        with mock.patch.object(erasure, "release_external",
                               side_effect=release):
            purged = worker.purge_scheduled_deletions()
        self.assertIn(tid, purged)
        self.assertEqual(seen, [True],
                         "the tenants row was locked across the release")
        self.assertFalse(_exists(tid))

    def test_the_retry_marker_outlives_a_failed_erase(self):
        tid = _due_tenant()
        failed = dict(_RELEASED, plaid_failed="all")

        def boom(admin, tenant_id):
            raise RuntimeError("database went away mid-erase")

        with mock.patch.object(erasure, "release_external",
                               return_value=failed), \
                mock.patch.object(erasure, "collect_retry_ids",
                                  return_value={"plaid_item_ids": ["it-1"]}), \
                mock.patch.object(tenancy, "delete_tenant_rows",
                                  side_effect=boom):
            purged = worker.purge_scheduled_deletions()
        self.assertNotIn(tid, purged)
        self.assertTrue(_exists(tid))
        probe = tenancy.admin_connect()
        try:
            marker = probe.execute(
                "SELECT 1 FROM admin_audit WHERE target=%s "
                "AND action LIKE %s", (tid, "%pending%")).fetchone()
            row = probe.execute("SELECT status FROM tenants WHERE id=%s",
                                (tid,)).fetchone()
        finally:
            probe.close()
        self.assertIsNotNone(marker, "the release's retry marker was lost")
        # still frozen and due: tomorrow's run finishes it, and the claim
        # is given up so the restore door is open meanwhile
        self.assertEqual(row["status"], "pending_delete")
        self.assertIsNone(_row(tid)["purge_claimed_at"])

    def test_the_claim_leaves_status_and_prior_status_alone(self):
        tid = _due_tenant(unverified.STATUS)
        seen = []

        def release(tenant_id):
            if str(tenant_id) == tid:
                seen.append(dict(_row(tid)))
            return dict(_RELEASED)

        with mock.patch.object(erasure, "release_external",
                               side_effect=release):
            purged = worker.purge_scheduled_deletions()
        self.assertIn(tid, purged)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["status"], unverified.STATUS)
        self.assertEqual(seen[0]["status_before_delete"], "active")
        self.assertIsNotNone(seen[0]["purge_claimed_at"])

    def test_a_household_lifted_mid_purge_is_kept(self):
        tid = _due_tenant(unverified.STATUS)

        def release(tenant_id):
            if str(tenant_id) == tid:
                lift = tenancy.admin_connect()
                try:
                    # what confirming the address or a lockout's lift does
                    lift.execute(
                        "UPDATE tenants SET status='active', "
                        "delete_after=NULL, status_before_delete=NULL "
                        "WHERE id=%s", (tid,))
                finally:
                    lift.close()
            return dict(_RELEASED)

        with mock.patch.object(erasure, "release_external",
                               side_effect=release):
            purged = worker.purge_scheduled_deletions()
        self.assertNotIn(tid, purged)
        row = _row(tid)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["purge_claimed_at"])

    def test_a_household_lifted_after_its_release_gets_no_dead_links(self):
        """The release removes every Item at Plaid but leaves the tokens
        in place for the erase. A household lifted after that must come
        back with those Items archived, not looking connected while every
        sync fails against an Item that no longer exists."""
        tid = _due_tenant(unverified.STATUS)
        iid = f"it-lift-{tid[:8]}"
        conn = tenancy.tenant_connect(tid)
        try:
            conn.execute(
                "INSERT INTO items (id, aggregator, institution_name, "
                "access_token) VALUES (%s,'plaid','Test Bank','tok')",
                (iid,))
        finally:
            conn.close()
        probe = tenancy.admin_connect()
        try:
            probe.execute("INSERT INTO plaid_item_ledger (item_id) "
                          "VALUES (%s) ON CONFLICT DO NOTHING", (iid,))
        finally:
            probe.close()

        def release(tenant_id):
            if str(tenant_id) == tid:
                lift = tenancy.admin_connect()
                try:
                    # what the Plaid release stamps, then the lift
                    lift.execute(
                        "UPDATE plaid_item_ledger SET removed_at=now() "
                        "WHERE item_id=%s", (iid,))
                    lift.execute(
                        "UPDATE tenants SET status='active', "
                        "delete_after=NULL, status_before_delete=NULL "
                        "WHERE id=%s", (tid,))
                finally:
                    lift.close()
            return dict(_RELEASED, plaid_items=1)

        with mock.patch.object(erasure, "release_external",
                               side_effect=release):
            purged = worker.purge_scheduled_deletions()
        self.assertNotIn(tid, purged)
        conn = tenancy.tenant_connect(tid)
        try:
            item = conn.execute(
                "SELECT status, archived_reason, access_token FROM items "
                "WHERE id=%s", (iid,)).fetchone()
        finally:
            conn.close()
        self.assertEqual(item["status"], "archived")
        self.assertEqual(item["archived_reason"], "plaid-gone")
        self.assertIsNone(item["access_token"])

    def test_a_household_paid_for_mid_purge_is_restored_not_erased(self):
        """Money arriving for a freeze nobody asked for puts the household
        back where the restore door would, even when nothing lifted its
        status in time."""
        tid = _due_tenant(unverified.STATUS)
        paid = {"now": False}

        def release(tenant_id):
            if str(tenant_id) == tid:
                paid["now"] = True
            return dict(_RELEASED)

        def attached(admin, tenant_id):
            return paid["now"] and str(tenant_id) == tid

        with mock.patch.object(erasure, "release_external",
                               side_effect=release), \
                mock.patch.object(ext.gate._resolve(), "money_attached",
                                  side_effect=attached):
            purged = worker.purge_scheduled_deletions()
        self.assertNotIn(tid, purged)
        row = _row(tid)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["delete_after"])
        self.assertIsNone(row["purge_claimed_at"])

    def test_a_requested_deletion_is_erased_even_if_money_arrives(self):
        """A deletion somebody asked for is not called off by a payment."""
        tid = _due_tenant()
        with mock.patch.object(erasure, "release_external",
                               return_value=dict(_RELEASED)), \
                mock.patch.object(ext.gate._resolve(), "money_attached",
                                  return_value=True):
            purged = worker.purge_scheduled_deletions()
        self.assertIn(tid, purged)
        self.assertFalse(_exists(tid))

    def test_a_household_rescheduled_mid_purge_keeps_its_new_window(self):
        tid = _due_tenant()

        def release(tenant_id):
            if str(tenant_id) == tid:
                again = tenancy.admin_connect()
                try:
                    again.execute(
                        "UPDATE tenants SET delete_after = now() + "
                        "interval '7 days' WHERE id=%s", (tid,))
                finally:
                    again.close()
            return dict(_RELEASED)

        with mock.patch.object(erasure, "release_external",
                               side_effect=release):
            purged = worker.purge_scheduled_deletions()
        self.assertNotIn(tid, purged)
        self.assertTrue(_exists(tid))
        self.assertIsNone(_row(tid)["purge_claimed_at"])

    def test_an_unavailable_billing_add_on_holds_the_purge(self):
        tid = _due_tenant()
        released = mock.Mock(return_value=dict(_RELEASED))
        with mock.patch.object(ext, "gate", ext._FailClosedGate()), \
                mock.patch.object(erasure, "release_external", released):
            purged = worker.purge_scheduled_deletions()
        self.assertNotIn(tid, purged)
        self.assertTrue(_exists(tid))
        released.assert_not_called()

    def test_the_erasure_itself_stops_before_deleting_anything(self):
        """Every door that erases a household — not only the nightly
        purge — stops when the add-on cannot archive its record."""
        tid = _due_tenant()
        admin = tenancy.admin_connect()
        try:
            with mock.patch.object(ext, "gate", ext._FailClosedGate()):
                with self.assertRaises(ext.GateUnavailable):
                    tenancy.delete_tenant_rows(admin, tid)
        finally:
            admin.close()
        self.assertTrue(_exists(tid))


if __name__ == "__main__":
    unittest.main()
