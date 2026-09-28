"""Two nightly sweeps running at once must not write the same person twice.

The console's run-job button enqueues a fresh nightly on every click, so a
manual run can overlap the cron's. Both runs read "who has not been
reminded" and both compose a final notice before either takes a row lock,
so without a single-flight lock the same person gets the same letter
twice — the freeze itself is protected by the row lock, the mail is not.
A second entrant skips rather than waits: the run in flight is doing the
same sweep.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.jobs import unverified

from .util import _ensure_db

PW = "a-long-enough-password-1"


def _age(email: str, days: int) -> None:
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "UPDATE users SET created_at = now() - make_interval(days => %s) "
            "WHERE email=%s", (days, email))
    finally:
        admin.close()


def _status(email: str):
    admin = tenancy.admin_connect()
    try:
        return admin.execute(
            "SELECT t.status FROM tenants t JOIN users u ON u.tenant_id=t.id "
            "WHERE u.email=%s", (email,)).fetchone()["status"]
    finally:
        admin.close()


class SingleFlightTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            "OIKONOME_HOSTED": "1", "OIKONOME_OPEN_SIGNUP": "1",
            "OIKONOME_BASE_URL": "https://app.example.test",
            "OIKONOME_UNVERIFIED_REAP_DAYS": "30"})
        self._env.start()
        self._dev = mock.patch.object(self.appmod, "DEV_MODE", False)
        self._dev.start()
        self._vmail = mock.patch.object(self.appmod, "_deliver_verification")
        self._vmail.start()
        self.sent: list[str] = []
        self._mail = mock.patch.object(
            unverified.account_mail, "send",
            side_effect=lambda email, subject, plain, html, **kw:
                self.sent.append(email) or True)
        self._mail.start()
        from oikonome.web import security
        security._limiter._hits.clear()

    def tearDown(self):
        for p in (self._mail, self._vmail, self._dev, self._env):
            p.stop()

    def _signup(self):
        email = f"sf-{uuid.uuid4().hex[:8]}@x.dev"
        c = TestClient(self.appmod.app)
        r = c.post("/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return email

    def _mine(self, email):
        return [m for m in self.sent if m == email]

    def test_a_sweep_already_in_flight_makes_the_second_one_a_no_op(self):
        """The overlapping run sends nothing and freezes nothing: the one
        holding the lock is already doing both."""
        email = self._signup()
        _age(email, 8)
        holder = tenancy.admin_connect()
        try:
            self.assertTrue(holder.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (unverified._SWEEP_LOCK,)).fetchone()["ok"])
            out = unverified.sweep()
        finally:
            holder.close()
        self.assertEqual(self._mine(email), [])
        self.assertEqual(out, {"reminded": [], "scheduled": [], "held": []})

    def test_the_lock_is_released_so_the_next_night_still_runs(self):
        email = self._signup()
        _age(email, 8)
        unverified.sweep()
        self.assertEqual(len(self._mine(email)), 1, self.sent)
        # a fresh sweep in the same process must still be able to take the
        # lock — a leaked one would silence the sweep until the backend died
        probe = tenancy.admin_connect()
        try:
            self.assertTrue(probe.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                (unverified._SWEEP_LOCK,)).fetchone()["ok"])
        finally:
            probe.close()

    def test_the_freeze_night_sends_the_final_notice_and_not_the_reminder(self):
        """A household the sweep first meets when it is already past the
        window gets ONE letter: the reminder would name a freeze date
        that has already gone by."""
        email = self._signup()
        _age(email, 31)
        unverified.sweep()
        self.assertEqual(len(self._mine(email)), 1, self.sent)
        self.assertEqual(_status(email), unverified.STATUS)


if __name__ == "__main__":
    unittest.main()
