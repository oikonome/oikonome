"""The receipt doors answer "no such receipt" only when there is none.

A receipt id that is not a uuid names nothing, and reads as 404. Anything
else that goes wrong while pairing, unpairing, entering details or deleting —
a lock wait that timed out, a lost database connection — is a server fault
and must surface as one: answering 404 would tell the person the receipt on
their screen does not exist, and a client that trusts a 404 drops it from
view.

Also here: the demo household may pair and unpair a receipt (row-level,
undone from the same screen) but not delete one (the upload that could put
it back is refused there); and the transaction's receipt list carries the
charge's own amount and date, which the panel ranks waiting receipts by.
"""
import datetime as dt
import os
import unittest
import uuid
from unittest import mock

import psycopg
from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, add_txn, seed_accounts, write_config

PW = "correct-horse-battery"
PNG = ("r.png", b"\x89PNG...", "image/png")


def _app():
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    from oikonome.web.app import app
    return app


def _signup(app, prefix):
    # server faults answer as responses, not as exceptions in the test
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post("/api/signup", data={
        "email": f"{prefix}-{uuid.uuid4().hex[:8]}@example.dev",
        "password": PW})
    assert r.status_code == 200, r.text
    tid = client.get("/api/me").json()["tenant_id"]
    conn = tenancy.tenant_connect(tid)
    try:
        seed_accounts(conn)
        write_config(conn)
    finally:
        conn.close()
    return client, tid


def _lock_timeout(*_a, **_k):
    raise psycopg.errors.LockNotAvailable(
        "canceling statement due to lock timeout")


class MissingVersusBrokenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()
        cls.owner, cls.tid = _signup(cls.app, "rmb")
        conn = tenancy.tenant_connect(cls.tid)
        try:
            cls.txn = add_txn(conn, dt.date.today(), 23.15,
                              "LANTERN HARDWARE", account="chk")
        finally:
            conn.close()

    def _snap(self):
        r = self.owner.post("/api/receipts", files={"file": PNG})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def test_a_malformed_id_is_no_such_receipt(self):
        for call in (
                lambda: self.owner.post("/api/receipts/not-a-uuid/match",
                                        json={"txn_id": self.txn}),
                lambda: self.owner.post("/api/receipts/not-a-uuid/unmatch"),
                lambda: self.owner.post("/api/receipts/not-a-uuid/details",
                                        json={"total": "5.00",
                                              "date": dt.date.today()
                                              .isoformat()}),
                lambda: self.owner.delete("/api/receipts/not-a-uuid")):
            self.assertEqual(call().status_code, 404)

    def test_a_lock_timeout_while_pairing_is_not_a_missing_receipt(self):
        rid = self._snap()
        with mock.patch("oikonome.engine.receipt_match.match_by_hand",
                        _lock_timeout):
            r = self.owner.post(f"/api/receipts/{rid}/match",
                                json={"txn_id": self.txn})
        self.assertGreaterEqual(r.status_code, 500, r.text)

    def test_a_lock_timeout_while_unpairing_is_not_a_missing_receipt(self):
        rid = self._snap()
        m = self.owner.post(f"/api/receipts/{rid}/match",
                            json={"txn_id": self.txn})
        self.assertEqual(m.status_code, 200, m.text)
        with mock.patch("oikonome.engine.receipt_match.unmatch",
                        _lock_timeout):
            r = self.owner.post(f"/api/receipts/{rid}/unmatch")
        self.assertGreaterEqual(r.status_code, 500, r.text)
        # nothing moved: the receipt is still on its charge
        on = self.owner.get(f"/api/transactions/{self.txn}/receipts").json()
        self.assertIn(rid, [x["id"] for x in on["receipts"]])

    def test_the_receipt_list_carries_the_charge_it_belongs_to(self):
        got = self.owner.get(f"/api/transactions/{self.txn}/receipts")
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["txn"],
                         {"amount": 23.15,
                          "date": dt.date.today().isoformat()})
        gone = self.owner.get("/api/transactions/no-such-txn/receipts")
        self.assertIsNone(gone.json()["txn"])

    def test_a_charge_with_no_amount_still_lists_its_receipts(self):
        # a restored row can carry a NULL (or non-finite) amount: nothing
        # to rank against, but the panel must still open
        conn = tenancy.tenant_connect(self.tid)
        try:
            bare = add_txn(conn, dt.date.today(), 9.99, "LANTERN HARDWARE",
                           account="chk")
            conn.execute("UPDATE transactions SET amount = NULL"
                         " WHERE id = %s", (bare,))
            nan = add_txn(conn, dt.date.today(), 9.99, "LANTERN HARDWARE",
                          account="chk")
            conn.execute("UPDATE transactions SET amount = 'NaN'"
                         " WHERE id = %s", (nan,))
        finally:
            conn.close()
        for tid in (bare, nan):
            got = self.owner.get(f"/api/transactions/{tid}/receipts")
            self.assertEqual(got.status_code, 200, got.text)
            self.assertEqual(got.json(), {"receipts": [], "txn": None})


class DemoReceiptDoorTests(unittest.TestCase):
    def test_a_demo_household_may_pair_but_not_delete(self):
        app = _app()
        client, tid = _signup(app, "rmbd")
        conn = tenancy.tenant_connect(tid)
        try:
            txn = add_txn(conn, dt.date.today(), 9.99, "TEAPOT CAFE",
                          account="chk")
        finally:
            conn.close()
        # a waiting receipt already in the household, as the seed leaves one
        rid = client.post("/api/receipts", files={"file": PNG}).json()["id"]
        from oikonome.engine import budget
        from oikonome.web import demoguard
        conn = tenancy.tenant_connect(tid)
        try:
            cfg = budget.load_config(conn)
            cfg["demo_mode"] = True
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        demoguard._cache.clear()
        try:
            self.assertEqual(client.post(
                f"/api/receipts/{rid}/match",
                json={"txn_id": txn}).status_code, 200)
            self.assertEqual(client.post(
                f"/api/receipts/{rid}/unmatch").status_code, 200)
            self.assertEqual(
                client.delete(f"/api/receipts/{rid}").status_code, 403)
            conn = tenancy.tenant_connect(tid)
            try:
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM receipts WHERE id = %s", (rid,)).fetchone())
            finally:
                conn.close()
        finally:
            demoguard._cache.clear()


if __name__ == "__main__":
    unittest.main()
