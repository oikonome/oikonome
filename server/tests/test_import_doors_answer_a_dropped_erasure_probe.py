"""The file-import doors that find the household's sync lock held ask
`erasure.being_deleted(tid, conn)` before answering "a sync is running".
That probe may DISCARD the door's pooled connection (a take or release of
the push lock that failed). The door must still answer — 200 with its
"try again" error, not a 500 from a later statement on the dropped
handle — and must answer 409 when the household really is being deleted."""
import os
import unittest
import unittest.mock
import uuid

from fastapi.testclient import TestClient

from oikonome import erasure
from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config

CSV = b"Date,Description,Amount\n2026-07-01,CORNER GROCER,-40.00\n"


def _dropping_probe(conn, _tid):
    conn.discard()
    return False


class ImportDoorsAnswerADroppedProbe(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"probe-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        # another session holds the household's sync lock
        self.holder = tenancy.tenant_connect(self.tid)
        key = f"oikonome:sync:{self.tid}"
        self.assertTrue(self.holder.execute(
            "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
            (key,)).fetchone()["ok"])
        self.addCleanup(self.holder.close)
        self.addCleanup(tenancy.release_lock, self.holder, key)

    def _import(self):
        return self.client.post(
            "/api/import", data={"account_id": "chk", "amount_sign": "bank"},
            files={"file": ("box.csv", CSV, "text/csv")})

    def _ynab(self):
        return self.client.post("/api/import/ynab-accounts",
                                json={"token": "synthetic", "accounts": {}})

    def test_a_dropped_probe_answers_try_again_not_500(self):
        with unittest.mock.patch.object(erasure, "_probe_push_lock",
                                        _dropping_probe):
            for name, call in (("import", self._import),
                               ("ynab", self._ynab)):
                with self.subTest(door=name):
                    r = call()
                    self.assertEqual(r.status_code, 200, r.text[:300])
                    self.assertIn("sync is running",
                                  (r.json()["result"] or {})["error"])

    def test_a_household_being_deleted_answers_409(self):
        with unittest.mock.patch.object(erasure, "_probe_push_lock",
                                        lambda *_a: True):
            for name, call in (("import", self._import),
                               ("ynab", self._ynab)):
                with self.subTest(door=name):
                    self.assertEqual(call().status_code, 409)


if __name__ == "__main__":
    unittest.main()
