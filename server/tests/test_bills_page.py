"""Recurring-finder wizard: run-anytime detection, approve/reject through
the page, onboarding nudge when data exists but nothing is tracked."""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import report

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config


class RecurringPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"rec-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # a clean monthly series detection should find — anchored to the
            # REAL today (detection runs against date.today() and its freshness
            # gate drops a stale last hit, so a fixed-TODAY series rots as the
            # calendar advances). 30-day spacing → monthly, last hit 30d ago.
            anchor = dt.date.today()
            for k in range(5, 0, -1):
                add_txn(conn, anchor - dt.timedelta(days=30 * k), 15.49,
                        "NETFLIX.COM")
            # filler so the onboarding nudge threshold (>=20 txns) is met
            for i in range(20):
                add_txn(conn, TODAY - dt.timedelta(days=i * 3), 9.0 + i,
                        f"SHOP {i}")
        finally:
            conn.close()

    def test_onboarding_nudge_then_wizard_flow(self):
        # 1. nudge: txns exist, nothing tracked → setup alert in gather
        conn = tenancy.tenant_connect(self.tid)
        try:
            st = report.gather(conn, TODAY)
        finally:
            conn.close()
        self.assertTrue(st["needs_recurring_setup"])
        self.assertTrue(any(a["kind"] == "setup" for a in st["alerts"]))

        # 2. run the finder (form endpoint lives on; its redirect target is
        # now the SPA, so assert the proposal through the API)
        r = self.client.post("/bills/detect", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        props = self.client.get("/api/bills").json()
        self.assertIn("NETFLIX.COM", str(props))

        # 3. approve the Netflix proposal
        conn = tenancy.tenant_connect(self.tid)
        try:
            pid = conn.execute(
                "SELECT id FROM bill_proposals WHERE status='pending' "
                "AND payee='NETFLIX.COM'").fetchone()["id"]
        finally:
            conn.close()
        r = self.client.post("/bills/proposal",
                             data={"pid": pid, "action": "approve"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        conn = tenancy.tenant_connect(self.tid)
        try:
            bill = conn.execute("SELECT amount, active FROM bills "
                                "WHERE payee='NETFLIX.COM'").fetchone()
            self.assertEqual(bill["active"], 1)
            self.assertEqual(bill["amount"], -15.49)
            # 4. nudge clears once a bill is tracked
            st = report.gather(conn, TODAY)
            self.assertFalse(st["needs_recurring_setup"])
        finally:
            conn.close()

    def test_reject_via_page_is_remembered(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            # a second detectable series — anchored to the REAL today for the
            # same reason NETFLIX is (see setUpClass): detection runs against
            # date.today(), so a series pinned to the fixed TODAY rots out of
            # the freshness gate as the calendar advances and stops being
            # proposed at all.
            anchor = dt.date.today()
            for k in range(5, 0, -1):
                add_txn(conn, anchor - dt.timedelta(days=30 * k), 11.99,
                        "SPOTIFY")
        finally:
            conn.close()
        self.client.post("/bills/detect")
        conn = tenancy.tenant_connect(self.tid)
        try:
            pid = conn.execute(
                "SELECT id FROM bill_proposals WHERE status='pending' "
                "AND payee='SPOTIFY'").fetchone()["id"]
        finally:
            conn.close()
        self.client.post("/bills/proposal",
                         data={"pid": pid, "action": "reject"})
        # re-running the finder must NOT resurrect it
        self.client.post("/bills/detect", follow_redirects=True)
        conn = tenancy.tenant_connect(self.tid)
        try:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM bill_proposals "
                "WHERE payee='SPOTIFY' AND status='pending'").fetchone()["n"]
            self.assertEqual(n, 0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()


class ApproveAllTests(unittest.TestCase):
    """The setup wizard's one-click approve of every pending proposal."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"aa-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            anchor = dt.date.today()
            for k in range(5, 0, -1):
                add_txn(conn, anchor - dt.timedelta(days=30 * k), 15.49,
                        "NETFLIX.COM")
            for i in range(20):
                add_txn(conn, TODAY - dt.timedelta(days=i * 3), 9.0 + i,
                        f"SHOP {i}")
        finally:
            conn.close()

    def test_approve_all_lands_every_pending_proposal(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            anchor = dt.date.today()
            for k in range(5, 0, -1):
                add_txn(conn, anchor - dt.timedelta(days=30 * k), 42.0,
                        "HULU")
        finally:
            conn.close()
        self.client.post("/bills/detect")
        conn = tenancy.tenant_connect(self.tid)
        try:
            n = conn.execute("SELECT COUNT(*) AS n FROM bill_proposals "
                             "WHERE status='pending'").fetchone()["n"]
        finally:
            conn.close()
        self.assertGreaterEqual(n, 1)
        r = self.client.post("/api/bills/proposals/approve-all")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["approved"], n)
        self.assertEqual(body["failed"], [])
        self.assertIn("HULU", body["payees"])
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) AS n FROM bill_proposals "
                "WHERE status='pending'").fetchone()["n"], 0)
            self.assertEqual(conn.execute(
                "SELECT active FROM bills WHERE payee='HULU'"
            ).fetchone()["active"], 1)
        finally:
            conn.close()
        # nothing left: a second call is a no-op, not an error
        self.assertEqual(
            self.client.post("/api/bills/proposals/approve-all").json()
            ["approved"], 0)
