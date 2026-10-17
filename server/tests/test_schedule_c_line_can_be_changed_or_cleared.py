"""A Schedule C line saved on a business transaction can be changed and
cleared through the class door, and a save that does not mention the line
keeps it.

A body naming `sched_c_line` sets it (null or "" clears it); a body
without the key — the Books tab changing a bucket alone — leaves the
stored line as it is. Without the clear, a line saved by mistake could
never be taken off the row.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import books, entities

from .util import TODAY, _ensure_db, add_txn, seed_accounts, write_config


class ScheduleCLineChangesAndClears(unittest.TestCase):
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
            "email": f"schedc-clr-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
            cls.eid = entities.create_entity(
                conn, name="Line Venture LLC", structure="sole_prop")["id"]
            entities.assign_account(conn, "card", cls.eid)
        finally:
            conn.close()

    def _txn(self, name):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return add_txn(conn, TODAY, 25.0, name, account="card")
        finally:
            conn.close()

    def _line(self, txn):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return conn.execute(
                "SELECT sched_c_line FROM business_txn_class "
                "WHERE txn_id=%s", (txn,)).fetchone()["sched_c_line"]
        finally:
            conn.close()

    def _post(self, txn, body):
        r = self.client.post(
            f"/api/business/entities/{self.eid}/transactions/{txn}/class",
            json=body)
        self.assertEqual(r.status_code, 200, r.text)

    def test_set_change_and_clear(self):
        a, b = books.SCHED_C_LINES[0], books.SCHED_C_LINES[1]
        t = self._txn("PRINTER INK")
        self._post(t, {"bucket": "operating", "sched_c_line": a})
        self.assertEqual(self._line(t), a)
        self._post(t, {"bucket": "operating", "sched_c_line": b})
        self.assertEqual(self._line(t), b)
        self._post(t, {"bucket": "operating", "sched_c_line": None})
        self.assertIsNone(self._line(t))

    def test_empty_string_clears(self):
        t = self._txn("DESK LAMP")
        self._post(t, {"bucket": "operating",
                       "sched_c_line": books.SCHED_C_LINES[0]})
        self._post(t, {"bucket": "operating", "sched_c_line": ""})
        self.assertIsNone(self._line(t))

    def test_absent_key_keeps_the_line(self):
        t = self._txn("CABLE BILL")
        self._post(t, {"bucket": "operating",
                       "sched_c_line": books.SCHED_C_LINES[0]})
        self._post(t, {"bucket": "startup_195"})                 # Books tab
        self.assertEqual(self._line(t), books.SCHED_C_LINES[0])

    def test_unknown_line_is_refused(self):
        t = self._txn("MYSTERY")
        r = self.client.post(
            f"/api/business/entities/{self.eid}/transactions/{t}/class",
            json={"bucket": "operating", "sched_c_line": "Line 99 Made up"})
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
