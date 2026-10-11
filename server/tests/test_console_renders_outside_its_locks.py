"""Console doors finish their locked work before they draw the page, and
say so plainly when the billing add-on is not there to decide.

The dashboard is a page of fleet-wide reads plus the installed gate's own
context. Drawn while a door still holds the instance-wide intake lock, every
signup and reclaim on the instance waits behind it, each holding a database
backend. Drawn inside an open transaction, it also cannot show the audit row
that transaction is about to commit.
"""

import os
import unittest
import unittest.mock
import uuid

from fastapi.testclient import TestClient

from oikonome import ext
from oikonome.db import tenancy

from .util import _ensure_db

TOKEN = "test-admin-token-" + "y" * 32


def _admin_rows(sql, params=()):
    admin = tenancy.admin_connect()
    try:
        return admin.execute(sql, params).fetchall()
    finally:
        admin.close()


class _Base(unittest.TestCase):
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
        self.addCleanup(ext.set_gate, None)

    def tearDown(self):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)


class InviteAtCapacityTests(_Base):

    def test_the_refusal_page_is_drawn_after_the_intake_lock_is_released(
            self):
        from oikonome.web import adminconsole
        g = ext.gate._resolve()
        self.enterContext(unittest.mock.patch.object(
            g, "at_cap", return_value=True, create=True))
        self.enterContext(unittest.mock.patch.object(
            g, "intake_cap", return_value=1, create=True))
        email = f"cap-{uuid.uuid4().hex[:8]}@x.dev"
        seen = {}
        real = adminconsole._dashboard

        def watching(request, **kw):
            probe = tenancy.admin_connect()
            try:
                got = probe.execute(
                    "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                    ("oikonome:intake-mint",)).fetchone()["ok"]
                if got:
                    probe.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                                  ("oikonome:intake-mint",))
                seen["lock_free"] = got
                seen["audited"] = bool(probe.execute(
                    "SELECT 1 FROM admin_audit WHERE action='invite_refused'"
                    " AND target=%s", (email,)).fetchone())
            finally:
                probe.close()
            return real(request, **kw)

        with unittest.mock.patch.object(adminconsole, "_dashboard", watching):
            r = self.client.post("/admin/console/invite",
                                 data={"email": email})
        self.assertEqual(200, r.status_code, r.text)
        self.assertIn("NOT minted", r.text)
        self.assertTrue(seen.get("lock_free"),
                        "the dashboard was drawn while the instance-wide "
                        "intake lock was still held")
        self.assertTrue(seen.get("audited"),
                        "the refusal's audit row was not committed before "
                        "the page that reports it was drawn")
        self.assertFalse(_admin_rows(
            "SELECT 1 FROM signup_invites WHERE email=%s", (email,)))


class ReactivateWithoutTheAddOnTests(_Base):

    def _suspended_tenant(self):
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(
                admin, f"susp-{uuid.uuid4().hex[:8]}@x.dev")
            admin.execute("UPDATE tenants SET status='suspended' "
                          "WHERE id=%s", (tid,))
        finally:
            admin.close()
        return str(tid)

    def test_the_console_says_so_and_changes_nothing(self):
        tid = self._suspended_tenant()
        # the fail-closed gate's own answer, on whichever gate the page
        # renders with
        self.enterContext(unittest.mock.patch.object(
            ext.gate._resolve(), "reactivate",
            side_effect=ext._FailClosedGate().reactivate, create=True))
        r = self.client.post("/admin/console/tenant-status",
                             data={"tenant_id": tid, "to": "active"})
        self.assertEqual(200, r.status_code, r.text[:500])
        self.assertIn("NOT reactivated", r.text)
        self.assertEqual("suspended", _admin_rows(
            "SELECT status FROM tenants WHERE id=%s", (tid,))[0]["status"])
        self.assertFalse(_admin_rows(
            "SELECT 1 FROM admin_audit WHERE action='tenant_activate' "
            "AND target=%s", (tid,)))


class PurgeWithoutTheAddOnTests(unittest.TestCase):
    """An erasure while the add-on is not loaded stops before any row is
    deleted: sweeping its tables then would erase the only copy of a
    billing record nothing could archive."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_nothing_is_deleted_and_the_caller_hears_why(self):
        self.addCleanup(ext.set_gate, None)
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(
                admin, f"purge-{uuid.uuid4().hex[:8]}@x.dev")
            ext.set_gate(ext._FailClosedGate())
            with self.assertRaises(ext.GateUnavailable):
                tenancy.delete_tenant_rows(admin, tid)
            self.assertTrue(admin.execute(
                "SELECT 1 FROM tenants WHERE id=%s", (tid,)).fetchone())
        finally:
            ext.set_gate(None)
            try:
                tenancy.delete_tenant_rows(admin, tid)
            finally:
                admin.close()


if __name__ == "__main__":
    unittest.main()
