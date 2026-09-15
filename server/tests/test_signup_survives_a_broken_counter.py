"""A counting query that fails inside signup's transaction degrades the
way the door documents, instead of taking the whole signup down.

Two counters run inside `/api/signup`'s writing transaction — the intake
ceiling's account count and the reclaim mail's hourly quota — and both are
written to fail OPEN: admit the account, send the mail. That promise was
unkeepable as written. PostgreSQL aborts a transaction at the failed
statement, and catching the exception in Python does not un-abort it: every
later statement, and the COMMIT itself, then raise
`InFailedSqlTransaction`. So a bare try/except around a counter converted
any statement-level failure — a query cancelled during a deploy, `out of
shared memory` from max_locks_per_transaction on a busy box, a container
rolled ahead of its migrations — into a 500 on a door that was supposed to
carry on, and for the reclaim counter into a 500 that is itself the oracle
the shared 409 exists to hide.

The failures below are real PostgreSQL errors, not Python raises, because
an aborted transaction is the entire subject.
"""

import os
import threading
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class _BreakStatement:
    """Connection proxy that answers every statement containing `needle`
    with a genuine server-side error, leaving the transaction aborted
    exactly as a cancelled or resource-starved query would."""

    def __init__(self, conn, needle):
        self._conn = conn
        self._needle = needle
        self.fired = 0

    def execute(self, sql, *a, **kw):
        if self._needle in sql:
            self.fired += 1
            return self._conn.execute("SELECT 1 / 0")
        return self._conn.execute(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._conn, name)


class BrokenCounterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        for target in ("DEV_MODE",):
            p = mock.patch.object(self.appmod, target, False)
            p.start()
            self.addCleanup(p.stop)
        for target in ("_deliver_verification", "_operator_signup_notice",
                       "_deliver_reclaim"):
            p = mock.patch.object(self.appmod, target)
            self.addCleanup(p.stop)
            setattr(self, target.strip("_"), p.start())
        self._env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                                 "OIKONOME_OPEN_SIGNUP": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)
        # the reclaim mail goes out on its own thread, so the test waits
        # for it rather than reading `called` the instant the 409 lands
        self.mailed = threading.Event()
        self.deliver_reclaim.side_effect = \
            lambda *a, **kw: self.mailed.set()
        self._admin = tenancy.admin_connect

    def _break(self, needle):
        """Every admin connection this request opens breaks that
        statement — the signup door opens one per request."""
        self._admin = tenancy.admin_connect
        real = self._admin
        proxies = []

        def factory(*a, **kw):
            p = _BreakStatement(real(*a, **kw), needle)
            proxies.append(p)
            return p

        patch = mock.patch.object(tenancy, "admin_connect", factory)
        patch.start()
        self.addCleanup(patch.stop)
        return proxies

    def _signup(self, email):
        return TestClient(self.appmod.app).post(
            "/api/signup", data={"email": email, "password": PW,
                                 "invite": "", "ref": ""})

    def _user_exists(self, email) -> bool:
        conn = self._admin()
        try:
            return conn.execute("SELECT 1 FROM users WHERE email=%s",
                                (email,)).fetchone() is not None
        finally:
            conn.close()


    def test_a_broken_reclaim_count_still_answers_the_shared_409(self):
        """The reclaim quota fails open: the mail goes, and the answer stays
        the same 409 a verified address gets. A 500 here would be the
        oracle — it would say the address is sitting unconfirmed."""
        email = f"brkr-{uuid.uuid4().hex[:10]}@x.dev"
        self.assertEqual(self._signup(email).status_code, 200)
        proxies = self._break("count(*) AS n FROM signup_reclaims")
        r = self._signup(email)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("already has an account", r.text)
        self.assertTrue(any(p.fired for p in proxies))
        self.assertTrue(self.mailed.wait(3),
                        "a broken counter must not swallow the mail it "
                        "was meant only to rate-limit")


if __name__ == "__main__":
    unittest.main()


class BrokenCapacityCountAtTheMintDoors(unittest.TestCase):
    """The same class of failure at the OTHER doors that count under a lock.

    `capacity.usage` was written for the admin console's autocommit read
    and guards each figure with a bare try/except. Two intake-mint doors
    call it from INSIDE a transaction that goes on to mint an
    invite — the console's mint form and the operator's emailed one-click
    approve link, both under the instance-wide intake-mint lock. There the
    bare except guards nothing: PostgreSQL aborts the transaction at the
    failed statement, catching it in Python does not un-abort it, and the
    next statement raises `InFailedSqlTransaction` — a 500 on the approve
    link and no invite, instead of the fail-open degrade the except clause
    promises.
    """

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self._env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"})
        self._env.start()
        self.addCleanup(self._env.stop)


