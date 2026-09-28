"""GET /api/bills rolls a received income series' due date on page load
(payday roll) — a write riding a read. Viewers are read-only everywhere,
so THEIR page load must leave due_on untouched; the same load by the
owner advances it. Pins the inline role check inside the bills read,
which the global write gate (non-GET only) cannot cover."""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_bill, add_txn, seed_accounts, write_config

PAYCHECK = "ACME PAYROLL DIRECT DEP"


class ViewerBillsReadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.owner = TestClient(app)
        cls.owner.post("/api/signup", data={
            "email": f"vbo-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        # an income series due today with its deposit already posted —
        # exactly the state the payday roll advances. The endpoint rolls
        # against the real clock, so the fixture uses it too.
        cls.today = dt.date.today()
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            add_bill(conn, "Acme Payroll", 2000.0,
                     frequency="WEEKLY", interval=2, next_due=cls.today,
                     last_seen=cls.today - dt.timedelta(days=14),
                     income=True)
            add_txn(conn, cls.today, -2000.00, PAYCHECK, account="chk",
                    primary="INCOME")
        finally:
            conn.close()
        inv = cls.owner.post("/api/invites", json={
            "label": "spouse", "password": "correct-horse-battery"}).json()
        token = inv["url"].rsplit("token=", 1)[1]
        cls.viewer = TestClient(app)
        r = cls.viewer.post("/api/invite/claim", json={
            "token": token,
            "email": f"vbv-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "family-member-pass"})
        assert r.status_code == 200, r.text

    def _due_on(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT due_on FROM bills "
                "WHERE payee='Acme Payroll'").fetchone()["due_on"]
        finally:
            conn.close()

    def test_viewer_load_leaves_due_on_owner_load_rolls_it(self):
        self.assertEqual(self.viewer.get("/api/me").json()["role"], "viewer")
        # the viewer's page load reads fine and mutates nothing
        self.assertEqual(self.viewer.get("/api/bills").status_code, 200)
        self.assertEqual(self._due_on(), self.today)
        # the identical load by the owner proves the fixture is potent:
        # the deposit evidence rolls due_on one cycle forward
        self.assertEqual(self.owner.get("/api/bills").status_code, 200)
        self.assertEqual(self._due_on(),
                         self.today + dt.timedelta(days=14))


if __name__ == "__main__":
    unittest.main()
