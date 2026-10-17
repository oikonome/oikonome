"""Approving a bill offer that clears something the person set must say
so to the client.

A switch to an envelope clears a pinned amount (the pin was on a
per-charge price the pool no longer has). The engine explains that in a
note; the single-approve and approve-all doors must carry the note to the
client, and the activity log keeps it beside the decision, so the pin is
never lost silently. An approve that clears nothing carries no note."""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills
from oikonome.engine.compat import jsonb

from .util import TODAY, _ensure_db, add_bill, seed_accounts, write_config

NEXT = TODAY + dt.timedelta(days=10)


def _make_client() -> TestClient:
    import os
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return TestClient(app)


class ClearedPinNoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = _make_client()
        cls.client.post("/api/signup", data={
            "email": f"pin-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _pinned_envelope_offer(self, payee: str, pid: str,
                               pinned: bool = True) -> None:
        conn = tenancy.tenant_connect(self.tid)
        try:
            add_bill(conn, payee, 6.0, next_due=NEXT)
            if pinned:
                conn.execute(
                    "UPDATE bills SET raw = raw || %s::jsonb WHERE payee=%s",
                    (jsonb({"lock_amount": True}), payee))
            bills._insert_proposal(
                conn, pid=pid, kind="type_change", payee=payee, amount=54.0,
                bill_type="envelope",
                evidence={"old_amount": 6.0, "old_due": NEXT.isoformat()})
        finally:
            conn.close()

    def _logged_note(self, pid: str):
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = conn.execute(
                "SELECT detail FROM activity_log "
                "WHERE detail->>'pid' = %s", (pid,)).fetchone()
            return (dict(row["detail"]) if row else {}).get("note")
        finally:
            conn.close()

    def test_a_single_approve_returns_the_note(self):
        pid = f"prop:{uuid.uuid4().hex[:8]}"
        self._pinned_envelope_offer("Corner Coffee", pid)
        r = self.client.post("/api/bills/proposal",
                             json={"pid": pid, "action": "approve"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["payee"], "Corner Coffee")
        self.assertIn("pin", body.get("note", ""))
        self.assertIn("pin", self._logged_note(pid) or "")

    def test_an_approve_that_clears_nothing_has_no_note(self):
        pid = f"prop:{uuid.uuid4().hex[:8]}"
        self._pinned_envelope_offer("Night Bakery", pid, pinned=False)
        r = self.client.post("/api/bills/proposal",
                             json={"pid": pid, "action": "approve"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("note", r.json())

    def test_approve_all_returns_each_note_by_payee(self):
        pid = f"prop:{uuid.uuid4().hex[:8]}"
        self._pinned_envelope_offer("Tea House", pid)
        r = self.client.post("/api/bills/proposals/approve-all")
        self.assertEqual(r.status_code, 200, r.text)
        notes = r.json()["notes"]
        self.assertTrue(any(n["payee"] == "Tea House" and "pin" in n["note"]
                            for n in notes), notes)


if __name__ == "__main__":
    unittest.main()
