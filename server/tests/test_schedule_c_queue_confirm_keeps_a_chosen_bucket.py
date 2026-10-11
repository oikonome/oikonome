"""Confirming a Schedule C line from the review queue keeps a bucket the
person chose since.

The queue lists rows nobody has classified and offers a date-derived
default bucket beside the suggested line; its confirm sends both. The Books
tab sends a bucket alone. When a bucket was set on a row after the queue
was loaded, the queue's confirm records the line and leaves that bucket
alone — the default must not overwrite a decision.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import entities

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config


class QueueConfirmKeepsAChosenBucket(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"schedc-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cls.eid = entities.create_entity(
                conn, name="Queue Venture LLC", structure="sole_prop")["id"]
            entities.assign_account(conn, "card", cls.eid)
        finally:
            conn.close()

    def _txn(self, name):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return add_txn(conn, TODAY, 25.0, name, account="card")
        finally:
            conn.close()

    def _class(self, txn):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT bucket, sched_c_line FROM business_txn_class "
                "WHERE txn_id=%s", (txn,)).fetchone()
        finally:
            conn.close()

    def _post(self, txn, body):
        return self.client.post(
            f"/api/business/entities/{self.eid}/transactions/{txn}/class",
            json=body)

    def _a_line(self):
        from oikonome.engine import books
        return books.SCHED_C_LINES[0]

    def test_queue_confirm_after_a_books_tab_bucket_keeps_that_bucket(self):
        t = self._txn("OFFICE SUPPLY A")
        r = self._post(t, {"bucket": "startup_195"})          # Books tab
        self.assertEqual(r.status_code, 200, r.text)
        r = self._post(t, {"bucket": "operating",             # queue default
                           "sched_c_line": self._a_line()})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._class(t)
        self.assertEqual(row["bucket"], "startup_195")
        self.assertEqual(row["sched_c_line"], self._a_line())

    def test_queue_confirm_on_an_unclassified_row_takes_its_bucket(self):
        t = self._txn("OFFICE SUPPLY B")
        r = self._post(t, {"bucket": "operating",
                           "sched_c_line": self._a_line()})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._class(t)["bucket"], "operating")

    def test_books_tab_still_changes_the_bucket(self):
        t = self._txn("OFFICE SUPPLY C")
        self._post(t, {"bucket": "operating", "sched_c_line": self._a_line()})
        r = self._post(t, {"bucket": "organizational"})
        self.assertEqual(r.status_code, 200, r.text)
        row = self._class(t)
        self.assertEqual(row["bucket"], "organizational")
        self.assertEqual(row["sched_c_line"], self._a_line())

    def test_a_bad_bucket_is_still_400_when_a_bucket_is_stored(self):
        t = self._txn("OFFICE SUPPLY D")
        self._post(t, {"bucket": "operating"})
        r = self._post(t, {"bucket": "nonsense",
                           "sched_c_line": self._a_line()})
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
