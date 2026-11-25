"""A person's answer about two receipts read alike goes through a door of
its own, is stored, and is never asked again.

"Same receipt" makes the two one slip, counted once; "Two purchases"
keeps them apart for good. Both are logged; a viewer may not answer, and
an answer about a receipt that is not there is a 404.
"""
import unittest

from oikonome.db import tenancy

from .test_waiting_receipt_doors import _app, _claim, _signup


class SlipAnswerDoorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = _app()
        cls.owner, cls.tid = _signup(cls.app, "slip")

    def _snap(self, image):
        r = self.owner.post("/api/receipts",
                            files={"file": ("r.png", image, "image/png")})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def _read_alike(self, *rids, total=12.5):
        """The receipts read as one store, day and total (the reading the
        person is asked about)."""
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                "UPDATE receipts SET status = 'parsed', parsed = %s::jsonb"
                " WHERE id = ANY(%s::uuid[])",
                ('{"merchant": "Invented Diner", "date": "2026-01-05", '
                 f'"total": {total}}}', list(rids)))
        finally:
            conn.close()

    def _key(self, rid):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT COALESCE(slip_id, id)::text AS k FROM receipts"
                " WHERE id = %s::uuid", (rid,)).fetchone()["k"]
        finally:
            conn.close()

    def test_same_receipt_then_two_purchases(self):
        a, b = self._snap(b"\x89PNG one"), self._snap(b"\x89PNG two")
        self._read_alike(a, b)
        self.assertNotEqual(self._key(a), self._key(b))
        r = self.owner.post(f"/api/receipts/{a}/same-slip",
                            json={"other": b})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"id": a, "other": b, "same": True,
                                    "released": []})
        self.assertEqual(self._key(a), self._key(b))
        waiting = {w["id"]: w for w in
                   self.owner.get("/api/receipts/waiting").json()["waiting"]}
        self.assertEqual([t["receipt_id"] for t in waiting[a]["slip_twins"]],
                         [b])
        r = self.owner.post(f"/api/receipts/{a}/two-purchases",
                            json={"other": b})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["same"])
        self.assertNotEqual(self._key(a), self._key(b))
        conn = tenancy.tenant_connect(self.tid)
        try:
            acts = {x["action"] for x in conn.execute(
                "SELECT action FROM activity_log WHERE kind = 'receipt'"
            ).fetchall()}
        finally:
            conn.close()
        self.assertTrue({"same_slip", "two_purchases"} <= acts)

    def test_two_receipts_nobody_was_asked_about_are_never_joined(self):
        a, b = self._snap(b"\x89PNG six"), self._snap(b"\x89PNG seven")
        self._read_alike(a)
        self._read_alike(b, total=99.0)
        for door in ("same-slip", "two-purchases"):
            r = self.owner.post(f"/api/receipts/{a}/{door}",
                                json={"other": b})
            self.assertEqual(r.status_code, 400, r.text)
        self.assertNotEqual(self._key(a), self._key(b))
        conn = tenancy.tenant_connect(self.tid)
        try:
            self.assertEqual(conn.execute(
                "SELECT cardinality(not_same_slip) AS n FROM receipts"
                " WHERE id = %s::uuid", (a,)).fetchone()["n"], 0)
        finally:
            conn.close()

    def test_an_answer_about_a_missing_receipt_is_404(self):
        a = self._snap(b"\x89PNG three")
        r = self.owner.post(f"/api/receipts/{a}/same-slip", json={
            "other": "00000000-0000-0000-0000-000000000000"})
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.owner.post(
            f"/api/receipts/{a}/two-purchases", json={"other": a}
        ).status_code, 400)
        self.assertEqual(self.owner.post(
            f"/api/receipts/{a}/two-purchases", json={}).status_code, 400)

    def test_a_viewer_cannot_answer(self):
        a, b = self._snap(b"\x89PNG four"), self._snap(b"\x89PNG five")
        viewer = _claim(self.app, self.owner, "viewer")
        r = viewer.post(f"/api/receipts/{a}/same-slip", json={"other": b})
        self.assertEqual(r.status_code, 403, r.text)


if __name__ == "__main__":
    unittest.main()
