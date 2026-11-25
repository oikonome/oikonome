"""'Approve all N' approves the N offers the person was shown.

A detection pass can file new offers (or move which twin of a series is
listed) between the page loading and the click. The clients send the ids
they rendered; the door approves only those still listed, and an offer
filed after the page loaded stays pending for the person to see. An older
client that sends no ids approves everything listed, as it always did.
"""

import datetime as dt
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import bills

from .util import _ensure_db, seed_accounts, write_config


class ApproveAllShownOnly(unittest.TestCase):
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
            "email": f"apall-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        tid = self.client.get("/api/me").json()["tenant_id"]
        self.conn = tenancy.tenant_connect(tid)
        self.addCleanup(self.conn.close)
        seed_accounts(self.conn)
        write_config(self.conn)
        for pid, payee in (("prop:shown-a", "LANTERN CLEANERS"),
                           ("prop:shown-b", "HARBOR GYM")):
            self._offer(pid, payee)
        self.shown = sorted(p["id"] for p in bills.pending_proposals(self.conn))
        self.assertEqual(self.shown, ["prop:shown-a", "prop:shown-b"])
        # filed by a detection pass after the page loaded
        self._offer("prop:late", "MEADOW PARKING")

    def _offer(self, pid, payee):
        bills._insert_proposal(
            self.conn, pid=pid, kind="add", payee=payee, amount=20.0,
            frequency="MONTHLY",
            next_due=dt.date.today() + dt.timedelta(days=10),
            evidence={"hits": 4, "cycle_days": 30})
        self.conn.commit()

    def _status(self, pid):
        return self.conn.execute(
            "SELECT status FROM bill_proposals WHERE id = %s",
            (pid,)).fetchone()["status"]

    def test_only_the_offers_shown_are_approved(self):
        r = self.client.post("/api/bills/proposals/approve-all",
                             json={"pids": self.shown})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["approved"], 2)
        self.assertEqual(self._status("prop:late"), "pending")

    def test_a_shown_offer_no_longer_listed_is_not_approved(self):
        self.conn.execute("UPDATE bill_proposals SET status = 'rejected' "
                          "WHERE id = 'prop:shown-b'")
        self.conn.commit()
        r = self.client.post("/api/bills/proposals/approve-all",
                             json={"pids": self.shown})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["approved"], 1)
        self.assertEqual(self._status("prop:shown-b"), "rejected")

    def test_an_older_client_without_ids_approves_everything_listed(self):
        r = self.client.post("/api/bills/proposals/approve-all")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["approved"], 3, r.json())


if __name__ == "__main__":
    unittest.main()
