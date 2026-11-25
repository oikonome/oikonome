"""Approving an offer whose bill is already tracked says the offer is gone.

The approve withdraws such an offer rather than add a second bill, so the
card that offered it has nothing left to do. The answer carries
`superseded: true` so a client drops the card instead of leaving a button
that fails every time it is pressed. Approve-all marks such an offer the
same way in its failures when one it listed turns out tracked; an offer
the list hides (covered by an active bill, or a twin of the one shown) is
not tried at all.
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
        # the series was found on the fixture's fixed day; the offer is a
        # current one, so its next date is ahead of the real calendar (a
        # date a full cycle behind is an offer that lapsed)
        self.conn.execute("UPDATE bill_proposals SET next_due = "
                          "current_date + 10 WHERE id = %s", (self.pid,))
        add_bill(self.conn, "Streamflix", 16.00, source="manual")

    def test_the_single_approve_says_superseded(self):
        r = self.client.post("/api/bills/proposal",
                             json={"pid": self.pid, "action": "approve"})
        self.assertEqual(r.status_code, 400, r.text)
        body = r.json()
        self.assertTrue(body.get("superseded"), body)
        self.assertIn("already tracked", body["detail"])

    def test_approve_all_leaves_an_offer_it_did_not_list(self):
        # the list hides an offer an active bill covers, so the button
        # that counted the list neither approves it nor reports it as one
        # that "stayed pending"; the nightly retires it
        self.assertNotIn(self.pid,
                         [p["id"] for p in rd.pending_proposals(self.conn)])
        r = self.client.post("/api/bills/proposals/approve-all")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["approved"], r.json()["failed"]), (0, []))

    def test_approve_all_after_one_twin_reports_no_withdrawn_twin(self):
        # a series offered at two amounts is listed once; approving the
        # listed one makes the bill that covers its twin, and approve-all
        # afterwards has nothing to say about the twin it never showed
        self.conn.execute("UPDATE bill_proposals SET status='rejected' "
                          "WHERE id = %s", (self.pid,))
        for pid, amount, days in (("prop:twin-a", 15.0, 9),
                                  ("prop:twin-b", 18.0, 12)):
            rd._insert_proposal(
                self.conn, pid=pid, kind="add", payee="Lantern Cleaners",
                amount=amount, frequency="MONTHLY",
                next_due=dt.date.today() + dt.timedelta(days=days),
                evidence={"hits": 4, "cycle_days": 30})
        self.conn.commit()
        listed = [p["id"] for p in rd.pending_proposals(self.conn)
                  if p["payee"] == "Lantern Cleaners"]
        self.assertEqual(listed, ["prop:twin-b"])
        r = self.client.post("/api/bills/proposal",
                             json={"pid": "prop:twin-b", "action": "approve"})
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/bills/proposals/approve-all")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["failed"], [])

    def test_an_ordinary_refusal_is_not_superseded(self):
        r = self.client.post("/api/bills/proposal",
                             json={"pid": "no-such-offer",
                                   "action": "approve"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertFalse(r.json().get("superseded"))


if __name__ == "__main__":
    unittest.main()
