"""A connection the bank feed removed asks for a person's hand only until the
household links that bank again. Once a live link to the same institution
exists, the old one is history: it must stop raising "needs attention" in the
header, the Settings list and the phone (all of which read /api/connections),
say once what happened to it, and go away when the owner forgets it.

A removed connection with no live replacement still needs attention — that is
the case the warning exists for."""
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db


class ReplacedConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"replaced-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def setUp(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute("DELETE FROM items")
        finally:
            conn.close()

    def _item(self, iid, name, *, inst_id=None, status="ok", reason=None):
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                """INSERT INTO items (id, aggregator, institution_id,
                                      institution_name, status,
                                      archived_reason, archived_at)
                   VALUES (%s, 'plaid', %s, %s, %s, %s,
                           CASE WHEN %s::text IS NULL THEN NULL
                                ELSE timestamptz '2026-06-03 18:00+00' END)""",
                (iid, inst_id, name, status, reason, reason))
        finally:
            conn.close()

    def _conns(self):
        r = self.client.get("/api/connections")
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_a_removed_link_with_a_live_twin_is_not_counted(self):
        self._item("old", "Maple Credit Union", status="archived",
                   reason="plaid-gone")
        self._item("new", "maple  credit union")
        body = self._conns()
        self.assertEqual(body["attention"], 0)
        old = next(c for c in body["connections"] if c["id"] == "old")
        self.assertEqual(old["status_kind"], "gone")
        self.assertTrue(old["replaced"])
        self.assertFalse(old["needs_attention"])
        self.assertEqual(old["removed_text"],
                         "removed by the bank feed on 06/03/26 — replaced")
        new = next(c for c in body["connections"] if c["id"] == "new")
        self.assertFalse(new["replaced"])
        self.assertFalse(new["needs_attention"])
        self.assertIsNone(new["removed_text"])

    def test_the_institution_id_decides_when_both_carry_one(self):
        self._item("old", "Maple CU", inst_id="ins_1", status="archived",
                   reason="auto-reap")
        self._item("new", "Maple Credit Union", inst_id="ins_1")
        self._item("other", "Maple CU", inst_id="ins_2", status="archived",
                   reason="plaid-gone")
        body = self._conns()
        by = {c["id"]: c for c in body["connections"]}
        self.assertTrue(by["old"]["replaced"])
        # same name, another institution: nothing replaced it
        self.assertFalse(by["other"]["replaced"])
        self.assertTrue(by["other"]["needs_attention"])
        self.assertEqual(body["attention"], 1)

    def test_a_removed_link_with_no_replacement_still_needs_attention(self):
        self._item("old", "Juniper Bank", status="archived",
                   reason="plaid-gone")
        body = self._conns()
        self.assertEqual(body["attention"], 1)
        self.assertTrue(body["connections"][0]["needs_attention"])
        self.assertIsNone(body["connections"][0]["removed_text"])

    def test_a_bank_outage_is_never_counted(self):
        self._item("down", "Juniper Bank", status="error:INSTITUTION_DOWN")
        self._item("login", "Cedar Bank",
                   status="error:ITEM_LOGIN_REQUIRED")
        body = self._conns()
        by = {c["id"]: c for c in body["connections"]}
        self.assertFalse(by["down"]["needs_attention"])
        self.assertTrue(by["login"]["needs_attention"])
        self.assertEqual(body["attention"], 1)

    def test_forgetting_a_replaced_link_removes_it_from_the_list(self):
        self._item("old", "Maple Credit Union", status="archived",
                   reason="plaid-gone")
        self._item("new", "Maple Credit Union")
        r = self.client.post("/api/connections/old/disconnect")
        self.assertEqual(r.status_code, 200, r.text)
        ids = [c["id"] for c in self._conns()["connections"]]
        self.assertEqual(ids, ["new"])


if __name__ == "__main__":
    unittest.main()
