"""Two business-row doors.

Undoing an import must not destroy an archived business's transactions:
archiving makes a business read-only, its rows are retention records, and
every other door that moves or deletes them refuses.

A Schedule C confirm aimed at a pending hold the sync has since posted
must land on the posted row, which every reader counts — not on the
retired hold, and not be refused. And its free-text note must be a
string: anything else is the client's mistake, a 400, never a 500.
"""

import json
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"bizrows-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def setUp(self):
        self.eid = self.client.post("/api/business/entities", json={
            "name": f"Shop {uuid.uuid4().hex[:4]}",
            "structure": "sole_prop"}).json()["id"]
        self.acct = f"a-{uuid.uuid4().hex[:6]}"
        self._sql("INSERT INTO accounts (id,item_id,name,type,subtype,"
                  "balance_current,entity_id) VALUES (%s,'it1','Biz Card',"
                  "'credit','credit card',0,%s)", (self.acct, self.eid))

    def _sql(self, sql, args=()):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(sql, args).fetchall() \
                if sql.lstrip().upper().startswith("SELECT") \
                else conn.execute(sql, args)
        finally:
            conn.close()


class UndoImportOfAnArchivedBusinessTests(_Base):
    def _import(self):
        batch = f"b-{uuid.uuid4().hex[:8]}"
        rid = f"imp-{uuid.uuid4().hex[:8]}"
        self._sql(
            "INSERT INTO transactions (id, account_id, date, amount, name, "
            "pending, removed, raw) VALUES (%s,%s,'2025-04-02',80,'Paper',"
            "0,0,%s)", (rid, self.acct,
                        json.dumps({"_batch": batch, "_batches": [batch]})))
        self._sql("INSERT INTO business_txn_class (txn_id, bucket, "
                  "sched_c_line) VALUES (%s,'operating','Supplies')", (rid,))
        return batch, rid

    def test_undo_refuses_while_the_business_is_archived(self):
        batch, rid = self._import()
        r = self.client.post(f"/api/business/entities/{self.eid}/archive")
        self.assertEqual(r.status_code, 200, r.text)
        r = self.client.post("/api/import/rollback", json={"batch_id": batch})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("archived", r.json()["detail"])
        self.assertTrue(self._sql(
            "SELECT 1 FROM transactions WHERE id=%s", (rid,)))
        self.assertTrue(self._sql(
            "SELECT 1 FROM business_txn_class WHERE txn_id=%s", (rid,)))

    def test_the_rollback_itself_refuses_under_its_lock(self):
        # a door's own look happens before the lock; a business archived
        # after it must still stop the delete, so the check that counts
        # is the one the rollback makes
        from oikonome.sync import batches
        batch, rid = self._import()
        self._sql("UPDATE business_entity SET status='archived', "
                  "archived_at=now() WHERE id=%s", (self.eid,))
        conn = tenancy.tenant_connect(self.tid)
        try:
            with self.assertRaises(batches.ArchivedBusinessRows):
                batches.rollback(conn, batch)
        finally:
            conn.close()
        self.assertTrue(self._sql(
            "SELECT 1 FROM transactions WHERE id=%s", (rid,)))
        # the form door answers with the same refusal instead of a 500
        r = self.client.post("/import/rollback", data={"batch_id": batch},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertIn("archived", r.headers["location"].replace("%20", " "))
        self.assertTrue(self._sql(
            "SELECT 1 FROM transactions WHERE id=%s", (rid,)))

    def test_undo_still_works_for_an_active_business(self):
        batch, rid = self._import()
        r = self.client.post("/api/import/rollback", json={"batch_id": batch})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["deleted"], 1)
        self.assertFalse(self._sql(
            "SELECT 1 FROM transactions WHERE id=%s", (rid,)))


class ClassifyASettledHoldTests(_Base):
    def _settled_hold(self):
        hold, posted = (f"h-{uuid.uuid4().hex[:8]}",
                        f"p-{uuid.uuid4().hex[:8]}")
        # the hold the review queue listed, retired by the sync, and the
        # posted charge (for a different amount: a tip) that replaced it
        self._sql("INSERT INTO transactions (id, account_id, date, amount, "
                  "name, pending, removed) VALUES (%s,%s,'2025-05-01',40,"
                  "'Diner',1,1)", (hold, self.acct))
        self._sql("INSERT INTO transactions (id, account_id, date, amount, "
                  "name, pending, removed, pending_transaction_id) VALUES "
                  "(%s,%s,'2025-05-02',47.5,'Diner',0,0,%s)",
                  (posted, self.acct, hold))
        return hold, posted

    def _classify(self, txn, **body):
        return self.client.post(
            f"/api/business/entities/{self.eid}/transactions/{txn}/class",
            json={"bucket": "operating", **body})

    def test_a_confirm_on_a_settled_hold_lands_on_the_posted_row(self):
        hold, posted = self._settled_hold()
        r = self._classify(hold, sched_c_line="Meals")
        self.assertEqual(r.status_code, 200, r.text)
        rows = self._sql("SELECT txn_id, sched_c_line FROM "
                         "business_txn_class WHERE txn_id IN (%s,%s)",
                         (hold, posted))
        self.assertEqual([(x["txn_id"], x["sched_c_line"]) for x in rows],
                         [(posted, "Meals")])

    def test_another_entitys_row_is_still_not_found(self):
        other = self.client.post("/api/business/entities", json={
            "name": f"Other {uuid.uuid4().hex[:4]}",
            "structure": "sole_prop"}).json()["id"]
        _, posted = self._settled_hold()
        r = self.client.post(
            f"/api/business/entities/{other}/transactions/{posted}/class",
            json={"bucket": "operating"})
        self.assertEqual(r.status_code, 404, r.text)

    def test_a_note_that_is_not_text_is_a_400(self):
        _, posted = self._settled_hold()
        for bad in ({"x": 1}, [1, 2], 7):
            r = self._classify(posted, note=bad)
            self.assertEqual(r.status_code, 400, r.text)
        r = self._classify(posted, sched_c_line={"x": 1})
        self.assertEqual(r.status_code, 400, r.text)
        r = self._classify(posted, note="lunch with a client")
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
