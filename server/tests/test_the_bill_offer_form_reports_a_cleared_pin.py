"""The form door that approves a bill offer says when the approve cleared
something the person set.

The JSON door carries apply_proposal's note (a switch to an envelope
clears a pinned amount) to the client; the form door's redirect message
has to carry it too, or the pin is lost silently on that path."""

import datetime as dt
import os
import unittest
import uuid
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills
from oikonome.engine.compat import jsonb

from .util import TODAY, _ensure_db, add_bill, seed_accounts, write_config

NEXT = TODAY + dt.timedelta(days=10)


class FormApproveNoteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"pinform-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _offer(self, payee: str, pinned: bool) -> str:
        pid = f"prop:{uuid.uuid4().hex[:8]}"
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
        return pid

    def _msg(self, pid: str) -> str:
        r = self.client.post("/bills/proposal",
                             data={"pid": pid, "action": "approve"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        return parse_qs(urlparse(r.headers["location"]).query)["msg"][0]

    def test_a_form_approve_that_clears_a_pin_says_so(self):
        msg = self._msg(self._offer("Northwind Coffee", pinned=True))
        self.assertIn("Northwind Coffee", msg)
        self.assertIn("pin", msg)

    def test_a_form_approve_that_clears_nothing_adds_no_note(self):
        msg = self._msg(self._offer("Contoso Bakery", pinned=False))
        self.assertEqual(msg, "approved: Contoso Bakery")


if __name__ == "__main__":
    unittest.main()
