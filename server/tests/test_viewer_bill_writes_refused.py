"""A household viewer cannot write to a bill through any door the triage
queue uses — the SERVER gate is the real control, the client's hidden
buttons are only courtesy.

save / toggle / hint are plain mutating routes with no per-route role
check; the refusal comes from the write gate in `current_user`. This
pins that: if a route ever grows a carve-out (SELF_WRITE_OK) or the gate
regresses, a viewer's tap on Accept / Disable / Dismiss would move the
household's money schedule.
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_bill, seed_accounts, write_config


class ViewerBillWriteTests(unittest.TestCase):
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
            "email": f"vbw-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            add_bill(conn, "Acme Clinic", 100.0, frequency="MONTHLY",
                     interval=1, next_due="2026-09-22")
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
        assert cls.viewer.get("/api/me").json()["role"] == "viewer"

    def test_accept_disable_dismiss_doors_refuse_a_viewer(self):
        for path, body in (
            ("/api/bills/save", {"payee": "Acme Clinic", "amount": 150,
                                 "cadence": "MONTHLY:1"}),
            ("/api/bills/toggle", {"payee": "Acme Clinic"}),
            ("/api/bills/hint", {"payee": "Acme Clinic",
                                 "action": "dismiss", "target": "health"}),
        ):
            r = self.viewer.post(path, json=body)
            self.assertEqual(r.status_code, 403, f"{path}: {r.text}")
        # and nothing moved
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                "SELECT amount FROM bills WHERE payee='Acme Clinic'"
            ).fetchone()
            self.assertEqual(abs(row["amount"]), 100.0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
