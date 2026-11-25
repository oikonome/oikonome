"""The receipt doors the rest of the suite leaves unread, and the parts of
the waiting list it leaves empty, match the shapes they document
(web/schemas/receipts.py).

The conformance hook (tests/api_conformance.py) validates each reply; this
module reaches them with a household that fills them: a receipt waiting
with a typed total between two charges that fit it equally (so it stays
waiting and `candidates` holds both), a tagged item line, the items search, the month's tagged-item
report as JSON, and a parse request on an instance with a vision model.
"""

from __future__ import annotations

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from . import api_conformance
from .util import _ensure_db, add_txn, seed_accounts, write_config

PW = "correct-horse-battery"
PNG = ("r.png", b"\x89PNG...", "image/png")
TODAY = dt.date.today()


class ReceiptShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"rshape-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        assert r.status_code == 200, r.text
        tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            # two charges that fit the receipt equally well: the matcher
            # leaves the choice to a person, so it stays waiting with both
            # as candidates
            add_txn(conn, TODAY, 27.35, "COPPER KETTLE CAFE",
                    merchant="Copper Kettle", account="card")
            add_txn(conn, TODAY, 27.35, "COPPER KETTLE CAFE",
                    merchant="Copper Kettle", account="chk")
        finally:
            conn.close()
        r = cls.client.post("/api/receipts", files={"file": PNG})
        assert r.status_code == 200, r.text
        cls.rid = r.json()["id"]
        r = cls.client.post(f"/api/receipts/{cls.rid}/details", json={
            "total": "27.35", "date": TODAY.isoformat(),
            "merchant": "Copper Kettle"})
        assert r.status_code == 200, r.text
        conn = tenancy.tenant_connect(tid)
        try:
            conn.execute("INSERT INTO receipt_items (receipt_id, line, "
                         "description, qty, amount, tag) VALUES "
                         "(%s::uuid, 1, 'Pour-over', 1, 4.5, 'business')",
                         (cls.rid,))
        finally:
            conn.close()

    def _ok(self, method, path, **kw):
        before = sum(api_conformance.validated().values())
        r = self.client.request(method, "/api" + path, **kw)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(sum(api_conformance.validated().values()), before,
                           f"{method} {path}: the reply was not validated")
        return r.json()

    def test_the_waiting_list_with_candidates(self):
        out = self._ok("GET", "/receipts/waiting")
        rows = [w for w in out["waiting"] if w["id"] == self.rid]
        self.assertEqual(len(rows), 1, out)
        self.assertTrue(rows[0]["tie"], rows[0])
        self.assertEqual(len(rows[0]["candidates"]), 2, rows[0])

    def test_items_search_and_the_report(self):
        items = self._ok("GET", "/receipts/items?q=pour")
        self.assertTrue(items["rows"])
        rep = self._ok("GET", f"/receipts/report?tag=business&y={TODAY.year}"
                              f"&m={TODAY.month}")
        self.assertEqual(rep["count"], len(rep["rows"]))

    def test_a_parse_request(self):
        r = self.client.post("/api/receipts", files={"file": PNG})
        self.assertEqual(r.status_code, 200, r.text)
        rid = r.json()["id"]
        from oikonome.engine import llm_categorize
        from oikonome.web import api_receipts
        with mock.patch.object(llm_categorize, "configured",
                               return_value=True), \
                mock.patch.object(api_receipts, "_parse_receipt_async"):
            out = self._ok("POST", f"/receipts/{rid}/parse")
        self.assertEqual(out["status"], "parsing")


if __name__ == "__main__":
    unittest.main()
