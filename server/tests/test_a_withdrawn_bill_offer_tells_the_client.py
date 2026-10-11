"""Approving an offer whose bill is already tracked says the offer is gone.

The approve withdraws such an offer rather than add a second bill, so the
card that offered it has nothing left to do. The answer carries
`superseded: true` so a client drops the card instead of leaving a button
that fails every time it is pressed. Approve-all marks such an offer the
same way in its failures.
"""

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills as rd

from .util import (TODAY, _ensure_db, add_bill, add_txn, seed_accounts,
                   write_config)


class WithdrawnOfferAnswerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        self.client.post("/api/signup", data={
            "email": f"offer-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = self.client.get("/api/me").json()["tenant_id"]
        self.conn = tenancy.tenant_connect(tid)
        self.addCleanup(self.conn.close)
        seed_accounts(self.conn)
        write_config(self.conn)
        for i in range(5):
            add_txn(self.conn, dt.date(TODAY.year, TODAY.month - 5 + i, 8),
                    15.49, "STREAMFLIX.COM")
        rd.run(self.conn, TODAY)
        adds = self.conn.execute(
            "SELECT id FROM bill_proposals WHERE status='pending' "
            "AND kind='add'").fetchall()
        self.assertEqual(len(adds), 1)
        self.pid = adds[0]["id"]
        add_bill(self.conn, "Streamflix", 16.00, source="manual")

    def test_the_single_approve_says_superseded(self):
        r = self.client.post("/api/bills/proposal",
                             json={"pid": self.pid, "action": "approve"})
        self.assertEqual(r.status_code, 400, r.text)
        body = r.json()
        self.assertTrue(body.get("superseded"), body)
        self.assertIn("already tracked", body["detail"])

    def test_approve_all_marks_it_in_its_failures(self):
        r = self.client.post("/api/bills/proposals/approve-all")
        self.assertEqual(r.status_code, 200, r.text)
        failed = r.json()["failed"]
        self.assertEqual([f["pid"] for f in failed], [self.pid])
        self.assertTrue(failed[0]["superseded"])

    def test_an_ordinary_refusal_is_not_superseded(self):
        r = self.client.post("/api/bills/proposal",
                             json={"pid": "no-such-offer",
                                   "action": "approve"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertFalse(r.json().get("superseded"))


if __name__ == "__main__":
    unittest.main()
