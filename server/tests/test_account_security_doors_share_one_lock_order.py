"""Every door that touches a user's `users` row and that user's factor
lock takes them in ONE order: the advisory lock first, the row second.

Both are real locks in one lock manager — PostgreSQL's deadlock detector
does not distinguish an advisory lock from the row-exclusive lock an UPDATE
takes — so two doors that acquire them in opposite orders deadlock, and the
loser is aborted with 40P01. Uncaught, that is a bare 500; and since each
rotation is now ONE transaction, the whole act rolls back after the person
was walked through a full step-up.

It is not a hypothetical interleaving. The web Settings tab and the mobile
Security screen are two clients signed into one account, and a double
submit is one client on its own. The TOTP doors take the factor lock
first, because their question ("would this leave the account with no second
factor?") has to be asked under it; a password- or email-change door that
updated `users` first and reached the same lock later, inside the passkey
eviction, would close the cycle — harmless only while such a door ran on
autocommit and held the row for a single statement.

What is asserted here is the invariant, not the deadlock: while a rotation
is waiting for the factor lock, it must not already be holding the user's
row. If it is, the cycle exists and a concurrent TOTP door closes it.
"""

import os
import threading
import unittest
import uuid
from unittest import mock

import psycopg
from fastapi.testclient import TestClient

from oikonome.auth import passwords
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class LockOrderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        # not hosted: a bare account with no second factor, so both doors
        # need nothing but the current password and the question stays
        # about lock order rather than about step-up
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        os.environ.pop("OIKONOME_HOSTED", None)
        self.addCleanup(self._env.stop)
        self.email = f"lock-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            self.tid = str(tenancy.create_tenant(admin, self.email))
            self.uid = str(admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (self.tid, self.email,
                 passwords.hash_password(PW))).fetchone()["id"])
            admin.commit()
        finally:
            admin.close()
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/api/login",
                             data={"email": self.email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    # ---- probes --------------------------------------------------------

    def _await_blocked_on_the_factor_lock(self, hold) -> None:
        """Wait until some other backend is queued behind the advisory
        lock we are holding. Without this the row probe below could read
        a request that has not started yet and call it a pass."""
        import time
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            n = hold.execute(
                "SELECT count(*) AS n FROM pg_locks WHERE locktype='advisory'"
                " AND NOT granted AND pid <> pg_backend_pid()"
            ).fetchone()["n"]
            if n:
                return
            time.sleep(0.05)
        self.fail("the door never reached the factor lock")

    def _user_row_is_free(self) -> bool:
        """Can an independent session take the user's row right now?

        NOWAIT rather than a timeout: the question is whether the row is
        held at this instant, and a wait would only tell us how long the
        other side runs."""
        probe = tenancy.admin_connect()
        try:
            with probe.transaction():
                probe.execute("SELECT 1 FROM users WHERE id=%s FOR UPDATE "
                              "NOWAIT", (self.uid,))
            return True
        except psycopg.errors.LockNotAvailable:
            return False
        finally:
            probe.close()

    def _assert_row_free_while_waiting_for_the_lock(self, fire):
        """Hold the factor lock, run `fire` (a door) against this account,
        and assert it queues for the lock WITHOUT the user's row in hand."""
        hold = tenancy.admin_connect()
        result: dict = {}
        try:
            with hold.transaction():
                hold.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                             ("oikonome:passkeys:" + self.uid,))

                def run():
                    try:
                        result["r"] = fire()
                    except Exception as e:            # noqa: BLE001
                        result["exc"] = e

                t = threading.Thread(target=run, daemon=True)
                t.start()
                self._await_blocked_on_the_factor_lock(hold)
                free = self._user_row_is_free()
            # the lock is released here; the door finishes
            t.join(timeout=30)
        finally:
            hold.close()
        self.assertNotIn("exc", result, repr(result.get("exc")))
        self.assertTrue(
            free,
            "the door held the users row while waiting for the factor "
            "lock — that is the inverted order, and a concurrent TOTP "
            "door (which takes the lock first) closes the cycle into a "
            "deadlock")
        return result["r"]

    # ---- the two rotation doors ---------------------------------------

    def test_password_change_takes_the_factor_lock_before_the_users_row(self):
        new = "brand-new-password-9"
        r = self._assert_row_free_while_waiting_for_the_lock(
            lambda: self.client.post(
                "/api/password/change",
                json={"current_password": PW, "new_password": new}))
        self.assertEqual(r.status_code, 200, r.text)
        # and the rotation still does its job once the lock is free
        again = TestClient(self.appmod.app).post(
            "/api/login", data={"email": self.email, "password": new})
        self.assertEqual(again.status_code, 200, again.text)

    def test_email_change_takes_the_factor_lock_before_the_users_row(self):
        new_addr = f"lockdest-{uuid.uuid4().hex[:8]}@x.dev"
        r = self._assert_row_free_while_waiting_for_the_lock(
            lambda: self.client.post(
                "/api/email/change",
                data={"new_email": new_addr, "password": PW}))
        self.assertEqual(r.status_code, 200, r.text)
        admin = tenancy.admin_connect()
        try:
            row = admin.execute("SELECT email FROM users WHERE id=%s",
                                (self.uid,)).fetchone()
        finally:
            admin.close()
        self.assertEqual(row["email"], new_addr)
