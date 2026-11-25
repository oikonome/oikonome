"""The household's alert history and the operator's audit trail are bounded:
the nightly retention pass drops what is older than a year plus the quarter
that reads it, and nothing younger.

An alert that is still standing is current state, not history, so it stays
however old its last sighting is. A dismissed alert that keeps firing keeps a
fresh last-seen date (the alert writer advances it on every sighting), so
dropping by last sighting never un-hides one the household dismissed.
"""

import datetime as dt
import unittest
import uuid

import psycopg

from oikonome.db import tenancy
from oikonome.jobs import retention

from .util import TEST_DB, _admin_dsn, _ensure_db, make_db

TODAY = dt.date.today()
NOW = dt.datetime.now(dt.timezone.utc)
KEEP = retention.HISTORY_KEEP_DAYS


class AlertHistoryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.rollback()
        self.conn.close()

    def _alert(self, message, days_ago, *, active=0, dismissed=0):
        seen = TODAY - dt.timedelta(days=days_ago)
        self.conn.execute(
            "INSERT INTO alerts_log (kind, severity, message, first_seen, "
            "last_seen, active, dismissed) VALUES ('drift', 'info', %s, "
            "%s, %s, %s, %s)", (message, seen, seen, active, dismissed))

    def test_only_alerts_last_seen_past_the_floor_go(self):
        self.assertEqual(KEEP, 400)
        self._alert("young", KEEP - 1)
        self._alert("old", KEEP + 1)
        self._alert("old-dismissed", KEEP + 1, dismissed=1)
        self._alert("old-but-standing", KEEP + 1, active=1)
        deleted = retention.prune_alerts_log(self.conn, batch=1)
        self.assertEqual(deleted, 2)
        left = {r["message"] for r in self.conn.execute(
            "SELECT message FROM alerts_log").fetchall()}
        self.assertEqual(left, {"young", "old-but-standing"})
        self.assertEqual(retention.prune_alerts_log(self.conn), 0)


class AdminAuditRetentionTests(unittest.TestCase):
    def test_only_audit_rows_past_the_floor_go(self):
        target = "retention-" + uuid.uuid4().hex[:8]
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            with admin.transaction():
                for label, days in (("young", KEEP - 1), ("old", KEEP + 1),
                                    ("older", KEEP + 30)):
                    admin.execute(
                        "INSERT INTO admin_audit (at, action, target, detail)"
                        " VALUES (%s, %s, %s, '{}'::jsonb)",
                        (NOW - dt.timedelta(days=days), label, target))
                self.assertGreaterEqual(
                    retention.prune_admin_audit(admin, batch=1), 2)
                left = {r["action"] for r in admin.execute(
                    "SELECT action FROM admin_audit WHERE target=%s",
                    (target,)).fetchall()}
                self.assertEqual(left, {"young"})
                raise psycopg.Rollback()
        finally:
            admin.close()

    def test_the_nightly_sweep_runs_both(self):
        _ensure_db()            # points the owner connection at the test DB
        out = retention.sweep()
        self.assertIn("alerts_log", out)
        self.assertIn("admin_audit", out)


if __name__ == "__main__":
    unittest.main()
