"""An email change and a signup (or invite claim) aimed at the same
address queue behind one another, and the loser is told the address is
taken.

Both doors check "is this address free?" and then write it. They take
the same advisory lock on the address first, so the second one's check
sees the first one's row. A writer that does not take that lock and wins
the race to the unique index still leaves the change with a 409 — never a
500 after the caller has gone through a full step-up.
"""

import os
import threading
import time
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class EmailChangeSerializesWithSignup(unittest.TestCase):
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
        self.email = f"chg-{uuid.uuid4().hex[:8]}@example.dev"
        self.assertEqual(self.client.post("/api/signup", data={
            "email": self.email, "password": PW}).status_code, 200)
        self.target = f"target-{uuid.uuid4().hex[:8]}@example.dev"

    def _change(self, out):
        out["r"] = self.client.post("/api/email/change", data={
            "new_email": self.target, "password": PW})

    def test_a_change_waits_for_a_signup_holding_the_address(self):
        out = {}
        hold = tenancy.admin_connect()
        try:
            with hold.transaction():
                hold.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                             ("oikonome:signup:" + self.target,))
                t = threading.Thread(target=self._change, args=(out,))
                t.start()
                time.sleep(1.0)
                self.assertTrue(t.is_alive(), "the change did not wait on "
                                              "the signup's address lock")
            t.join(30)
        finally:
            hold.close()
        self.assertEqual(out["r"].status_code, 200, out["r"].text)

    def test_losing_the_race_to_the_unique_index_is_a_409(self):
        other = f"other-{uuid.uuid4().hex[:8]}@example.dev"
        self.assertEqual(TestClient(self.app).post("/api/signup", data={
            "email": other, "password": PW}).status_code, 200)
        out = {}
        hold = tenancy.admin_connect()
        try:
            with hold.transaction():
                # a writer that takes no address lock claims the address
                hold.execute("UPDATE users SET email=%s WHERE email=%s",
                             (self.target, other))
                t = threading.Thread(target=self._change, args=(out,))
                t.start()
                time.sleep(1.0)
            t.join(30)
        finally:
            hold.close()
        self.assertEqual(out["r"].status_code, 409, out["r"].text)


if __name__ == "__main__":
    unittest.main()
