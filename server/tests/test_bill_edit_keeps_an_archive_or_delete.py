"""An edit form opened before its bill was archived or deleted must not
bring the bill back when it is saved.

The edit form names the bill it was opened on (`orig_payee`). Archiving
takes a bill out of the plan, Today and the email; deleting it removes it.
A Save still in flight, or another member's stale form, answers 409 rather
than writing into an archived bill or recreating a deleted one through the
insert path — a rename included. Adding a brand-new bill (no `orig_payee`)
is unaffected.

An ADD whose name an archived bill holds is refused too (409 with
`archived: true`), because writing into the archived row would leave it
archived while the form says the bill was added; `restore: true` on the add
is the explicit ask to bring it back. The queue's amount accept is an edit
(it names the bill it shows) and never brings an archived bill back.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


def _client() -> TestClient:
    os.environ["OIKONOME_DEV"] = "1"
    _ensure_db()
    import oikonome.web.app as appmod
    appmod.DEV_MODE = True
    return TestClient(appmod.app)


class BillEditAfterArchiveOrDelete(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = _client()
        r = cls.client.post("/api/signup", data={
            "email": f"billgone-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _row(self, payee):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute("SELECT active, amount FROM bills "
                                "WHERE payee=%s", (payee,)).fetchone()
        finally:
            conn.close()

    def _add(self, payee, amount=20):
        r = self.client.post("/api/bills/save", json={
            "payee": payee, "amount": amount, "cadence": "MONTHLY:1"})
        self.assertEqual(r.status_code, 200, r.text)

    def _edit(self, payee, amount, orig=None):
        return self.client.post("/api/bills/save", json={
            "payee": payee, "orig_payee": orig or payee, "amount": amount,
            "cadence": "MONTHLY:1"})

    def test_edit_of_a_bill_archived_since_is_409_and_it_stays_archived(self):
        self._add("Archived Gym")
        self.client.post("/api/bills/delete", json={"payee": "Archived Gym"})
        r = self._edit("Archived Gym", 55)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("archived", r.json()["detail"])
        row = self._row("Archived Gym")
        self.assertEqual(row["active"], 0)
        self.assertEqual(abs(row["amount"]), 20)

    def test_edit_of_a_bill_deleted_since_is_409_and_not_recreated(self):
        self._add("Deleted Stream")
        self.client.post("/api/bills/delete", json={"payee": "Deleted Stream"})
        self.client.post("/api/bills/delete",
                         json={"payee": "Deleted Stream", "mode": "purge"})
        self.assertIsNone(self._row("Deleted Stream"))
        r = self._edit("Deleted Stream", 30)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("deleted", r.json()["detail"])
        self.assertIsNone(self._row("Deleted Stream"))

    def test_rename_of_an_archived_bill_is_409_and_moves_nothing(self):
        self._add("Old Insurer")
        self.client.post("/api/bills/delete", json={"payee": "Old Insurer"})
        r = self._edit("New Insurer", 90, orig="Old Insurer")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIsNone(self._row("New Insurer"))
        self.assertEqual(self._row("Old Insurer")["active"], 0)

    def test_rename_of_a_deleted_bill_is_409_and_creates_nothing(self):
        r = self._edit("Fresh Name", 90, orig="Never Existed Bill")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIsNone(self._row("Fresh Name"))

    def test_a_live_bill_still_edits_and_renames(self):
        self._add("Live Phone")
        r = self._edit("Live Phone", 45)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(abs(self._row("Live Phone")["amount"]), 45)
        r = self._edit("Live Mobile", 45, orig="Live Phone")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self._row("Live Phone"))
        self.assertEqual(self._row("Live Mobile")["active"], 1)

    def test_adding_over_an_archived_name_is_409_and_writes_nothing(self):
        self._add("Archived Stream", 15)
        self.client.post("/api/bills/delete", json={"payee": "Archived Stream"})
        r = self.client.post("/api/bills/save", json={
            "payee": "Archived Stream", "merchant": "Archived Stream",
            "amount": 18, "cadence": "MONTHLY:1"})
        self.assertEqual(r.status_code, 409, r.text)
        body = r.json()
        self.assertIs(body["archived"], True)
        self.assertEqual(body["payee"], "Archived Stream")
        self.assertIn("restore", body["detail"])
        row = self._row("Archived Stream")
        self.assertEqual(row["active"], 0)
        self.assertEqual(abs(row["amount"]), 15)

    def test_an_add_that_asks_to_restore_brings_the_bill_back(self):
        self._add("Returning Stream", 15)
        self.client.post("/api/bills/delete",
                         json={"payee": "Returning Stream"})
        r = self.client.post("/api/bills/save", json={
            "payee": "Returning Stream", "amount": 18,
            "cadence": "MONTHLY:1", "restore": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(r.json()["restored"], True)
        row = self._row("Returning Stream")
        self.assertEqual(row["active"], 1)
        self.assertEqual(abs(row["amount"]), 18)

    def test_the_queue_amount_accept_never_revives(self):
        self._add("Queued Gym", 30)
        self.client.post("/api/bills/delete", json={"payee": "Queued Gym"})
        for extra in ({}, {"restore": True}):
            with self.subTest(extra=extra):
                r = self.client.post("/api/bills/save", json={
                    "payee": "Queued Gym", "orig_payee": "Queued Gym",
                    "amount": 33, **extra})
                self.assertEqual(r.status_code, 409, r.text)
                row = self._row("Queued Gym")
                self.assertEqual(row["active"], 0)
                self.assertEqual(abs(row["amount"]), 30)

    def test_adding_a_brand_new_payee_still_works(self):
        r = self.client.post("/api/bills/save", json={
            "payee": "Brand New Payee", "merchant": "Brand New Payee",
            "amount": 12, "cadence": "MONTHLY:1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._row("Brand New Payee")["active"], 1)


if __name__ == "__main__":
    unittest.main()
