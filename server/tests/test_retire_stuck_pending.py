"""A pending hold the bank abandoned can be retired by hand.

A pending charge is real spending until it posts or vanishes, and the
connectors retire the holds their own source stops carrying. A hold on a
file-imported account, or on a connection that has since died, has nobody
to do that: it counts against the verdict every single day, forever, and
there was no way for the person looking at it to say so.

The window is what keeps this off a row that is merely slow — a hold
released tomorrow would be deleted today and then arrive again as a
duplicate when it posted. Two weeks is well past any card authorization.

Retiring is the same soft retirement every connector uses (`removed=1`), so
the row keeps its id and a hold that does eventually post is still
recognised as the same transaction.
"""

import datetime as dt
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import data

from .util import TODAY, _ensure_db, add_txn, make_db, seed_accounts, \
    write_config

OLD = TODAY - dt.timedelta(days=30)
YOUNG = TODAY - dt.timedelta(days=3)
PW = "correct-horse-battery"


class RetirePendingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)

    def _pending(self, date, name="ABANDONED HOLD"):
        return add_txn(self.conn, date, 42.0, name, account="chk",
                       primary="GENERAL_MERCHANDISE", pending=1)

    def _removed(self, txn_id):
        return self.conn.execute(
            "SELECT removed FROM transactions WHERE id=%s",
            (txn_id,)).fetchone()["removed"]

    def test_a_two_week_old_hold_leaves_the_ledger_but_keeps_its_id(self):
        tid = self._pending(OLD)
        data.retire_pending(self.conn, tid, today=TODAY)
        self.assertEqual(self._removed(tid), 1)
        self.assertEqual(
            [r["id"] for r in self.conn.execute(
                "SELECT id FROM transactions WHERE id=%s", (tid,)).fetchall()],
            [tid], "the row was deleted rather than retired — a hold that "
                   "does post would arrive as a new transaction")

    def test_a_retired_hold_stops_counting_as_spending(self):
        tid = self._pending(OLD)
        before = data.transactions(self.conn, OLD.year, OLD.month)
        self.assertIn(tid, [r["id"] for r in before])
        data.retire_pending(self.conn, tid, today=TODAY)
        after = data.transactions(self.conn, OLD.year, OLD.month)
        self.assertNotIn(tid, [r["id"] for r in after])

    def test_a_hold_still_inside_the_window_is_refused(self):
        tid = self._pending(YOUNG)
        with self.assertRaises(ValueError) as caught:
            data.retire_pending(self.conn, tid, today=TODAY)
        self.assertIn("still settling", str(caught.exception))
        self.assertEqual(self._removed(tid), 0)

    def test_a_posted_charge_is_refused(self):
        """This door retires an authorization that never settled. A posted
        row is the household's real money and is not deleted from here."""
        tid = add_txn(self.conn, OLD, 42.0, "REAL CHARGE", account="chk")
        with self.assertRaises(ValueError) as caught:
            data.retire_pending(self.conn, tid, today=TODAY)
        self.assertIn("already posted", str(caught.exception))
        self.assertEqual(self._removed(tid), 0)

    def test_an_unknown_row_is_not_found(self):
        with self.assertRaises(LookupError):
            data.retire_pending(self.conn, "no-such-txn", today=TODAY)

    def test_an_already_retired_row_is_not_found_either(self):
        """Retiring twice must not report success the second time — the
        first one already took the row out of every read."""
        tid = self._pending(OLD)
        data.retire_pending(self.conn, tid, today=TODAY)
        with self.assertRaises(LookupError):
            data.retire_pending(self.conn, tid, today=TODAY)


class ARetiredHoldStaysRetiredThroughTheNextSync(unittest.TestCase):
    """The hourly sync upserts every row the feed still reports and
    un-removes it — which would resurrect a hold the person just retired
    within the hour. The person's retirement carries a stamp the upsert
    respects."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_feed_re_reporting_the_hold_does_not_bring_it_back(self):
        from oikonome.sync import base as sync_base
        old = TODAY - dt.timedelta(days=20)
        tid = add_txn(self.conn, old, 42.0, "HOLD AT RIVERTON MART", account="chk",
                      primary="GENERAL_MERCHANDISE", pending=1)
        data.retire_pending(self.conn, tid, today=TODAY)
        sync_base.upsert_transactions(self.conn, [sync_base.Transaction(
            id=tid, account_id="chk", date=old, amount=42.0,
            name="HOLD AT RIVERTON MART", pending=True)])
        row = self.conn.execute(
            "SELECT removed, retired_at FROM transactions WHERE id=%s", (tid,)).fetchone()
        self.assertEqual(row["removed"], 1)
        self.assertIsNotNone(row["retired_at"])
        # a row the AGGREGATOR removed and then re-reported still comes back
        other = add_txn(self.conn, old, 9.0, "RIVERTON FUEL", account="chk", pending=1)
        sync_base.mark_removed(self.conn, [other])
        sync_base.upsert_transactions(self.conn, [sync_base.Transaction(
            id=other, account_id="chk", date=old, amount=9.0, name="RIVERTON FUEL",
            pending=True)])
        self.assertEqual(self.conn.execute(
            "SELECT removed FROM transactions WHERE id=%s", (other,)).fetchone()["removed"], 0)


class RetirePendingRouteTests(unittest.TestCase):
    """The door itself: who may open it, and what it says when it won't."""

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.app = app
        cls.owner = TestClient(app)
        cls.owner.post("/api/signup", data={
            "email": f"own-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()
        # a view-only household member, claimed from the owner's invite
        r = cls.owner.post("/api/invites", json={"label": "reader",
                                                 "role": "viewer",
                                                 "password": PW})
        token = r.json()["url"].rsplit("token=", 1)[1]
        cls.viewer = TestClient(app)
        cls.viewer.post("/api/invite/claim", json={
            "token": token, "email": f"view-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})

    def _conn(self):
        return tenancy.tenant_connect(self.tid)

    def _add(self, date, pending=1):
        conn = self._conn()
        try:
            return add_txn(conn, date, 42.0, f"HOLD {uuid.uuid4().hex[:6]}",
                           account="chk", pending=pending)
        finally:
            conn.close()

    def _removed(self, txn_id):
        conn = self._conn()
        try:
            return conn.execute("SELECT removed FROM transactions WHERE id=%s",
                                (txn_id,)).fetchone()["removed"]
        finally:
            conn.close()

    def test_an_abandoned_hold_is_retired(self):
        tid = self._add(dt.date.today() - dt.timedelta(days=30))
        r = self.owner.post("/api/transactions/retire-pending",
                            json={"id": tid})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "id": tid})
        self.assertEqual(self._removed(tid), 1)

    def test_a_hold_inside_the_window_is_refused_with_a_reason(self):
        tid = self._add(dt.date.today() - dt.timedelta(days=2))
        r = self.owner.post("/api/transactions/retire-pending",
                            json={"id": tid})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("still settling", r.json()["detail"])
        self.assertEqual(self._removed(tid), 0)

    def test_a_posted_charge_is_refused(self):
        tid = self._add(dt.date.today() - dt.timedelta(days=30), pending=0)
        r = self.owner.post("/api/transactions/retire-pending",
                            json={"id": tid})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._removed(tid), 0)

    def test_an_unknown_id_is_a_404(self):
        r = self.owner.post("/api/transactions/retire-pending",
                            json={"id": "no-such-txn"})
        self.assertEqual(r.status_code, 404, r.text)

    def test_a_missing_id_is_a_400(self):
        r = self.owner.post("/api/transactions/retire-pending", json={})
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_viewer_cannot_retire_anything(self):
        tid = self._add(dt.date.today() - dt.timedelta(days=30))
        r = self.viewer.post("/api/transactions/retire-pending",
                             json={"id": tid})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self._removed(tid), 0,
                         "a view-only login changed the ledger")


if __name__ == "__main__":
    unittest.main()
