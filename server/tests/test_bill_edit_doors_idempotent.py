"""The bill edit doors the triage queue drives must be safe to repeat.

toggle: an explicit target state makes a duplicated request a no-op
instead of a second flip. save: an amount-only save keeps the bill's own
cadence instead of defaulting a yearly bill to monthly.
"""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


class BillEditDoorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.c = TestClient(app)
        cls.c.post("/api/signup", data={
            "email": f"bed-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.c.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _row(self, payee):
        for b in self.c.get("/api/bills").json()["bills"]:
            if b["payee"] == payee:
                return b
        return None

    def test_toggle_with_target_state_is_idempotent(self):
        r = self.c.post("/api/bills/save", json={
            "payee": "Idem Co", "amount": 20, "cadence": "MONTHLY:1",
            "next_due": "2026-09-22"})
        self.assertEqual(r.status_code, 200, r.text)
        for _ in range(2):
            r = self.c.post("/api/bills/toggle",
                            json={"payee": "Idem Co", "disabled": True})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["disabled"])
        self.assertTrue(self._row("Idem Co")["disabled"])
        r = self.c.post("/api/bills/toggle",
                        json={"payee": "Idem Co", "disabled": False})
        self.assertFalse(r.json()["disabled"])
        # a bare flip still flips
        r = self.c.post("/api/bills/toggle", json={"payee": "Idem Co"})
        self.assertTrue(r.json()["disabled"])

    def test_amount_only_save_keeps_a_yearly_cadence(self):
        r = self.c.post("/api/bills/save", json={
            "payee": "Yearly Co", "amount": 120, "cadence": "YEARLY:1",
            "next_due": "2027-03-01"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.c.post("/api/bills/save",
                        json={"payee": "Yearly Co", "amount": 150})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._row("Yearly Co")
        self.assertEqual(row["cadence_value"], "YEARLY:1")
        self.assertEqual(row["amount"], 150)
        self.assertEqual(row["due_on"], "2027-03-01")


if __name__ == "__main__":
    unittest.main()
