"""The waiting-receipts door pages, and the details door refuses a total
the matcher could never use.

`count` is every receipt waiting, so a client can tell there is more than
the page it holds and ask for the rest with `offset`. A hand-typed total
past the matcher's ceiling would be saved and then read as unknown — a
receipt that silently never meets its charge — so the door says no.
"""
import datetime as dt
import unittest

from oikonome.db import tenancy
from oikonome.engine import receipt_match, receipts

from .test_receipt_doors_tell_missing_from_broken import _app, _signup

PNG = b"\x89PNG..."


class WaitingPagesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()

    def setUp(self):
        self.client, tid = _signup(self.app, "waitpage")
        conn = tenancy.tenant_connect(tid)
        try:
            self.rids = [receipts.add(conn, None, PNG, "image/png")
                         for _ in range(3)]
        finally:
            conn.close()

    def test_limit_and_offset_page_while_count_stays_the_total(self):
        first = self.client.get("/api/receipts/waiting?limit=2").json()
        self.assertEqual(first["count"], 3)
        self.assertEqual(len(first["waiting"]), 2)
        rest = self.client.get(
            "/api/receipts/waiting?limit=2&offset=2").json()
        self.assertEqual(rest["count"], 3)
        self.assertEqual(len(rest["waiting"]), 1)
        seen = {w["id"] for w in first["waiting"] + rest["waiting"]}
        self.assertEqual(seen, {str(r) for r in self.rids})

    def test_the_default_page_is_unchanged_and_the_limit_is_bounded(self):
        r = self.client.get("/api/receipts/waiting").json()
        self.assertEqual((r["count"], len(r["waiting"])), (3, 3))
        self.assertEqual(r["limit"], receipt_match.PAGE_LIMIT)
        big = self.client.get("/api/receipts/waiting?limit=100000").json()
        self.assertEqual(big["limit"], receipt_match.PAGE_LIMIT)
        for bad in ("limit=0", "offset=-1"):
            self.assertEqual(self.client.get(
                f"/api/receipts/waiting?{bad}").status_code, 422, bad)

    def test_a_total_past_the_matchers_ceiling_is_refused(self):
        rid = str(self.rids[0])
        day = dt.date.today().isoformat()
        r = self.client.post(f"/api/receipts/{rid}/details",
                             json={"total": "1000000000001", "date": day})
        self.assertEqual(r.status_code, 400, r.text)
        ok = self.client.post(f"/api/receipts/{rid}/details",
                              json={"total": "12.50", "date": day})
        self.assertEqual(ok.status_code, 200, ok.text)


if __name__ == "__main__":
    unittest.main()
