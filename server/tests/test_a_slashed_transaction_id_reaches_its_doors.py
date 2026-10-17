"""A transaction id with a '/' in it reaches every door that names one.

File-import and bridge ids carry the source's own reference, and those
can contain a slash (an OFX FITID, a bridge's account path). The clients
send the id percent-encoded, but the server decodes the path before it
matches a route, so a plain `{txn_id}` segment stops at the slash and the
request falls through to a 404 — such a row could never be recategorized,
noted, split, flagged or given a receipt. Each door declares the id as a
path segment that may span slashes, keeping its fixed tail.
"""

import datetime as dt
import os
import unittest
import uuid
from urllib.parse import quote

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts

SLASHED = "ofx:card:2025/03/01-00017"
OTHER = "sfin:ACT-1/TRN-9"


def _url(prefix, txn_id, tail):
    # the clients' exact shape: encodeURIComponent of the id
    return f"{prefix}{quote(txn_id, safe='')}/{tail}"


class SlashedTransactionIdTests(unittest.TestCase):
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
        r = self.client.post("/api/signup", data={
            "email": f"slash-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            for tid, amt in ((SLASHED, 42.0), (OTHER, -42.0)):
                conn.execute(
                    "INSERT INTO transactions (id, account_id, date, amount,"
                    " name, merchant_name, category_primary, pending,"
                    " removed, raw) VALUES (%s, 'card', %s, %s,"
                    " 'CORNER GROCER', 'Corner Grocer',"
                    " 'GENERAL_MERCHANDISE', 0, 0, '{}'::jsonb)",
                    (tid, dt.date(2025, 3, 1), amt))
        finally:
            conn.close()

    def _row(self, txn_id):
        conn = tenancy.tenant_connect(self.tid)
        try:
            row = dict(conn.execute(
                "SELECT category_override FROM transactions WHERE id=%s",
                (txn_id,)).fetchone())
            note = conn.execute(
                "SELECT note FROM transaction_notes WHERE txn_id=%s",
                (txn_id,)).fetchone()
            row["note"] = note["note"] if note else None
            return row
        finally:
            conn.close()

    def _reached(self, r):
        """The request reached the door's handler: whatever it answered,
        it was not the router finding no route."""
        if r.status_code == 404:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = None
            self.assertNotEqual(detail, "Not Found", r.request.url)

    def test_a_slashed_id_can_be_recategorized(self):
        r = self.client.post(_url("/api/transactions/", SLASHED, "category"),
                             json={"category": "GROCERIES"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._row(SLASHED)["category_override"], "GROCERIES")

    def test_a_slashed_id_can_carry_a_note(self):
        r = self.client.post(_url("/api/transactions/", OTHER, "note"),
                             json={"note": "split with a neighbour"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._row(OTHER)["note"], "split with a neighbour")

    def test_every_transaction_door_takes_a_slashed_id(self):
        c = self.client
        doors = [
            ("post", "/api/transactions/", "category", {"json": {"category": "GROCERIES"}}),
            ("put", "/api/transactions/", "split",
             {"json": {"parts": [{"category": "GROCERIES", "amount": 42.0}]}}),
            ("delete", "/api/transactions/", "split", {}),
            ("post", "/api/transactions/", "note", {"json": {"note": "x"}}),
            ("post", "/api/transactions/", "owner", {"json": {"owner": None}}),
            ("get", "/api/reimburse/", "candidates", {}),
            ("post", "/api/reimburse/", "flag", {"json": {}}),
            ("post", "/api/reimburse/", "unflag", {}),
            ("post", "/api/transactions/", "receipt",
             {"files": {"file": ("r.png", b"\x89PNG\r\n\x1a\n", "image/png")}}),
            ("get", "/api/transactions/", "receipts", {}),
            ("post", "/api/business/", "flag", {}),
            ("post", "/api/business/", "unflag", {}),
            ("post", f"/api/business/entities/{uuid.uuid4()}/transactions/",
             "class", {"json": {"class": "personal"}}),
            ("post", "/api/transactions/", "entity", {"json": {"entity_id": None}}),
        ]
        for method, prefix, tail, kw in doors:
            with self.subTest(door=f"{method} {prefix}…/{tail}"):
                self._reached(getattr(c, method)(_url(prefix, SLASHED, tail), **kw))

    def test_the_receipts_door_lists_the_slashed_rows_receipts(self):
        r = self.client.get(_url("/api/transactions/", SLASHED, "receipts"))
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
