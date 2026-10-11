"""Two grace purges that overlap must not both claim the same household.

The nightly sweep and a purge started by hand from the console can run at
the same time. The claim is committed before the vendor releases, so a
second run reaching the household meanwhile finds its row unlocked. If
the second run overwrote the first one's live claim stamp, the first
would read its household as lifted and give it up, and the second would
take its "was money already attached" baseline after a payment that
arrived mid-purge: a household that paid while its links were released
would be erased, and the vendor state released twice. A live claim
belongs to its run; a second run leaves the household alone.
"""

import unittest
from unittest import mock

from oikonome import erasure
from oikonome.db import tenancy
from oikonome.jobs import worker

from .util import make_db

_RELEASED = {"plaid_items": 0, "released": "none", "mx_user": "none"}


class _SecondRunClaimed(Exception):
    pass


def _due_tenant() -> str:
    conn = make_db()
    try:
        tid = conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"]
    finally:
        conn.close()
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "UPDATE tenants SET status='pending_delete', "
            "delete_after = now() - interval '1 hour' WHERE id=%s", (tid,))
    finally:
        admin.close()
    return str(tid)


def _stamp(tid):
    probe = tenancy.admin_connect()
    try:
        row = probe.execute("SELECT purge_claimed_at FROM tenants WHERE id=%s",
                            (tid,)).fetchone()
        return row["purge_claimed_at"] if row else None
    finally:
        probe.close()


class OverlappingPurgeTests(unittest.TestCase):

    def test_a_second_purge_leaves_a_claimed_household_alone(self):
        from oikonome.web.adminconsole import _audit, _frozen_statuses
        tid = _due_tenant()
        statuses = _frozen_statuses()
        second = tenancy.admin_connect()
        real_hold = erasure.hold_writers
        seen = {}

        def hold(conn, tenant_id, *, xact=False):
            # the second run would block here on the first run's writer
            # locks; reaching this point at all means it took the claim
            if conn is second:
                raise _SecondRunClaimed()
            return real_hold(conn, tenant_id, xact=xact)

        def release(tenant_id):
            if str(tenant_id) == tid:
                before = _stamp(tid)
                try:
                    seen["second"] = worker._purge_one(second, tid, statuses,
                                                       _audit)
                except _SecondRunClaimed:
                    seen["second"] = "claimed"
                seen["stamp_kept"] = _stamp(tid) == before
            return dict(_RELEASED)

        try:
            with mock.patch.object(erasure, "hold_writers", side_effect=hold), \
                    mock.patch.object(erasure, "release_external",
                                      side_effect=release):
                purged = worker.purge_scheduled_deletions()
        finally:
            second.close()
        self.assertIsNone(seen.get("second"),
                          "the second run claimed a household already claimed")
        self.assertTrue(seen.get("stamp_kept"),
                        "the first run's claim stamp was overwritten")
        self.assertIn(tid, purged, "the first run lost its own claim")

    def test_a_stale_claim_is_taken_over(self):
        """A claim left by a worker killed mid-purge is cleared by nothing
        else, so an old enough stamp does not hold the household forever."""
        tid = _due_tenant()
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE tenants SET purge_claimed_at = now() - interval "
                "'%s hours' - interval '1 minute' WHERE id=%%s"
                % int(worker.PURGE_CLAIM_STALE_HOURS), (tid,))
        finally:
            admin.close()
        with mock.patch.object(erasure, "release_external",
                               return_value=dict(_RELEASED)):
            purged = worker.purge_scheduled_deletions()
        self.assertIn(tid, purged)


if __name__ == "__main__":
    unittest.main()
