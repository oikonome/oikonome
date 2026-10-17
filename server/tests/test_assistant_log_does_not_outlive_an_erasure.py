"""An answer recorded while the household is being erased neither
deadlocks the erasure nor outlives it.

The erasure sweeps every tenant table and then the tenant row, whose
cascade removes the members; the assistant log records an answer under a
share lock on the asker's member row and trims the log in the same
transaction. Taken in those orders the two met head-on when an at-cap
asker's answer landed mid-erasure, and an answer recorded after the sweep
had passed its table stayed behind with the asker's address. Pinned here:
the erasure locks the members first, so the record either commits before
the sweep (and is swept) or finds the household gone and writes nothing.
"""

import threading
import time
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import assistant

from .util import make_db

_OUT = {"answer": "ok", "blocks": [], "tools_used": []}


class _PausingAfterTheLogSweep:
    """The admin connection, paused once the erasure has deleted the
    assistant log, so a record can be attempted in the gap."""

    def __init__(self, conn, reached, go):
        self._c, self.reached, self.go = conn, reached, go

    def __getattr__(self, name):
        return getattr(self._c, name)

    def execute(self, q, *a, **k):
        r = self._c.execute(q, *a, **k)
        if "assistant_turns" in repr(q) and "DELETE" in repr(q):
            self.reached.set()
            self.go.wait(20)
        return r


class ErasureRaceTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        self.tid = self.conn.execute(
            "SELECT current_setting('app.tenant_id') t").fetchone()["t"]
        admin = tenancy.admin_connect()
        try:
            self.uid = str(admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'member') RETURNING id",
                (self.tid, f"m-{uuid.uuid4().hex[:8]}@example.dev")
            ).fetchone()["id"])
            # at the cap, so the record's trim deletes a row
            admin.execute(
                "INSERT INTO assistant_turns (tenant_id, asked_at, asked_by_id,"
                " asked_by, question, answer) SELECT %s, now() - (g || ' minutes')"
                "::interval, %s, 'a@example.dev', 'q' || g, 'a' FROM "
                "generate_series(1, %s) g",
                (self.tid, self.uid, assistant.HISTORY_KEEP))
            admin.commit()
        finally:
            admin.close()

    def tearDown(self):
        self.conn.close()

    def test_an_answer_landing_mid_erasure_neither_deadlocks_nor_survives(self):
        reached, go, errs = threading.Event(), threading.Event(), {}

        def erase():
            admin = tenancy.admin_connect()
            try:
                with admin.transaction():
                    tenancy.delete_tenant_rows(
                        _PausingAfterTheLogSweep(admin, reached, go), self.tid)
            except Exception as e:                      # noqa: BLE001
                errs["erase"] = e
            finally:
                admin.close()

        def record():
            try:
                errs["id"] = assistant.record_turn(
                    self.conn, {"user_id": self.uid, "email": "a@example.dev"},
                    "new", _OUT)
            except Exception as e:                      # noqa: BLE001
                errs["record"] = e

        e = threading.Thread(target=erase)
        e.start()
        self.assertTrue(reached.wait(20))
        r = threading.Thread(target=record)
        r.start()
        time.sleep(1.5)
        go.set()
        e.join(30)
        r.join(30)
        self.assertNotIn("erase", errs, errs.get("erase"))
        self.assertNotIn("record", errs, errs.get("record"))
        self.assertIsNone(errs.get("id"))
        admin = tenancy.admin_connect()
        try:
            left = admin.execute(
                "SELECT count(*) n FROM assistant_turns WHERE tenant_id = %s",
                (self.tid,)).fetchone()["n"]
        finally:
            admin.close()
        self.assertEqual(left, 0)


if __name__ == "__main__":
    unittest.main()
