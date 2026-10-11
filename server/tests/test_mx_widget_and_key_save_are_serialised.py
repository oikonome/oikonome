"""Opening the MX widget and saving new MX keys never interleave.

Opening the widget may create the household's MX user under the keys read
at its start, while a key save under another client decides whether a user
exists to move. Interleaved, the old client's user ends up stored beside
the new client's keys: every later sync fails on it, and the banks linked
into it stay at the old client with nothing left to release them. Both
doors therefore hold the household's sync lock.
"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db


class MxWidgetLockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={
            "email": f"mxl-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]

    def _lock_free(self) -> bool:
        conn = tenancy.tenant_connect(self.tid)
        try:
            key = f"oikonome:sync:{self.tid}"
            got = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) "
                               "AS ok", (key,)).fetchone()["ok"]
            if got:
                conn.execute("SELECT pg_advisory_unlock(hashtext(%s))",
                             (key,))
            return got
        finally:
            conn.close()

    def test_the_widget_is_opened_under_the_sync_lock(self):
        seen = []

        def widget(conn, transport=None):
            seen.append(self._lock_free())
            return "https://widget.example/x"
        with mock.patch("oikonome.sync.mx.connect_widget_url", widget):
            r = self.client.post("/api/accounts/mx/connect")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(seen, [False])

    def test_a_running_sync_or_key_save_makes_the_widget_wait(self):
        holder = tenancy.tenant_connect(self.tid)
        key = f"oikonome:sync:{self.tid}"
        called = []
        try:
            holder.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
            with mock.patch("oikonome.sync.mx.connect_widget_url",
                            lambda *a, **k: called.append(1) or "u"):
                r = self.client.post("/api/accounts/mx/connect")
            self.assertEqual(r.status_code, 409, r.text)
            self.assertEqual(called, [])
        finally:
            holder.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))
            holder.close()


if __name__ == "__main__":
    unittest.main()
