"""The signup door's seat lock costs nothing to hold.

An instance-wide seat count needs an instance-wide lock, and an advisory
xact lock is held until COMMIT. Taken at the TOP of the signup
transaction it therefore puts every signup in one queue, behind an
argon2id hash (~100 ms and 64 MiB by design) plus the whole
tenant/settings/user write — and each waiting request already holds an
unpooled connection it opened before the lock, so arrivals pile up raw
backends until Postgres refuses new ones, which closes every other
admin-connection door (sign-in unlock, email verification, the reclaim
link, the console) and not just signup.

So the lock belongs on the LAST cheap thing inside the transaction.
Nothing expensive may run while it is held — in particular the password
hash is computed before any advisory lock is taken at all. What that
trades is the refusal's timing, not its accuracy: a request that passes
a cheap pre-check can still be turned away at the end.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class IntakeCapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self._dev = mock.patch.object(self.appmod, "DEV_MODE", False)
        self._dev.start()
        self.addCleanup(self._dev.stop)
        self._vmail = mock.patch.object(self.appmod, "_deliver_verification")
        self._vmail.start()
        self.addCleanup(self._vmail.stop)
        self._notice = mock.patch.object(self.appmod,
                                         "_operator_signup_notice")
        self._notice.start()
        self.addCleanup(self._notice.stop)

    def _seats_taken(self) -> int:
        admin = tenancy.admin_connect()
        try:
            return self.appmod._intake_seats_taken(admin)
        finally:
            admin.close()

    def _signup(self, email: str | None = None):
        return TestClient(self.appmod.app).post(
            "/api/signup",
            data={"email": email or f"cap-{uuid.uuid4().hex[:10]}@x.dev",
                  "password": PW, "invite": "", "ref": ""})

    def test_no_advisory_lock_is_held_while_the_password_is_hashed(self):
        """argon2id is the expensive step; holding either the instance-wide
        intake lock or the per-email one across it serializes the door.
        Observed from an independent connection, because a lock is only a
        problem for the requests that are waiting on it."""
        held = []
        email = f"cap-{uuid.uuid4().hex[:10]}@x.dev"

        real = self.appmod.passwords.hash_password

        def watched(pw):
            probe = tenancy.admin_connect()
            try:
                held.append(probe.execute(
                    # exactly the two locks the door takes — the instance
                    # intake lock and this signup's own email lock — read
                    # back by key (pg_locks shows a bigint advisory key as
                    # its low 32 bits in objid). Any other advisory lock in
                    # the database is another test's or another shard's.
                    "SELECT count(*) AS n FROM pg_locks "
                    "WHERE locktype = 'advisory' "
                    "AND database = (SELECT oid FROM pg_database "
                    "WHERE datname = current_database()) "
                    "AND objid IN (hashtext(%s)::bigint & 4294967295, "
                    "hashtext(%s)::bigint & 4294967295) "
                    "AND pid <> pg_backend_pid()",
                    (self.appmod._INTAKE_LOCK, "oikonome:signup:" + email)
                ).fetchone()["n"])
            finally:
                probe.close()
            return real(pw)

        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                          "OIKONOME_OPEN_SIGNUP": "1"}):
            with mock.patch.object(self.appmod.passwords, "hash_password",
                                   side_effect=watched):
                self.assertEqual(self._signup(email).status_code, 200)
        self.assertEqual(held, [0],
                         "a signup hashed the password while holding an "
                         "advisory lock")


if __name__ == "__main__":
    unittest.main()
