"""Taking back a squatted address and an owner's credential rotation on
that same household keep one lock order: the settings row, then the
household-member lock.

A rotation pauses the household's outbound settings first (it holds the
settings row) and only then waits out the invite claims under the member
lock. The reclaim of an unverified owner decides under the member lock
whether the household is an empty shell, and then erases it, which deletes
the settings row. If the reclaim still held the member lock when it
reached for the settings row, the two would each hold what the other
wants: Postgres breaks the cycle by killing one of them, so either the
rotation fails or the erasure dies half way, after the reclaim link was
spent. The erasure therefore lets the member lock go before it waits on
anything, and takes it back after the settings row to re-check, under
both, that the household is still empty.
"""

import os
import threading
import time
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import reset as reset_mod
from oikonome.db import tenancy
from oikonome.notify import outbound_pause

from .lock_order_util import Gate, backend_pid, deadlocks, deadlocks_since
from .util import _ensure_db

PW = "correct-horse-battery"


def _another_backend_waits(not_pid: int, timeout: float = 20.0) -> bool:
    """True once some backend in this database other than `not_pid` is
    queued behind a lock — the reclaim runs inside the app, on connections
    the test cannot name."""
    admin = tenancy.admin_connect()
    try:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if admin.execute(
                    "SELECT 1 FROM pg_locks l"
                    "  JOIN pg_stat_activity a ON a.pid = l.pid"
                    " WHERE NOT l.granted AND l.pid <> %s"
                    "   AND a.datname = current_database()",
                    (not_pid,)).fetchone():
                return True
            time.sleep(0.02)
        return False
    finally:
        admin.close()


class ReclaimMeetsARotationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        patches = [
            mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                         "OIKONOME_OPEN_SIGNUP": "1"}),
            mock.patch.object(self.appmod, "DEV_MODE", False),
            mock.patch.object(self.appmod, "_deliver_verification"),
            mock.patch.object(
                self.appmod, "_deliver_reclaim",
                side_effect=lambda email, token: self.sent.append(
                    (email, token))),
        ]
        self.sent: list[tuple[str, str]] = []
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        from oikonome.web import security
        security._limiter._hits.clear()

    def _signup(self, email, password=PW):
        return TestClient(self.appmod.app).post(
            "/api/signup", data={"email": email, "password": password,
                                 "invite": "", "ref": ""})

    def _user(self, email):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT id, tenant_id, verified_at FROM users WHERE email=%s",
                (email,)).fetchone()
        finally:
            admin.close()

    def test_a_rotation_holding_the_settings_row_never_deadlocks_the_wipe(self):
        """An unverified owner's session rotates a credential while the
        mailbox's owner clicks the reclaim link. Both must finish: the
        rotation commits, and the reclaim then erases the shell and opens
        the fresh account — no deadlock, no half-done erasure."""
        email = f"rot-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(email).status_code, 200)
        squat = self._user(email)
        self.assertEqual(self._signup(email, password="owner-real-password")
                         .status_code, 409)
        token = self.sent[-1][1]

        gate = Gate()
        rot = {"conn": tenancy.admin_connect()}
        rot["conn"].execute("SET lock_timeout = '5s'")
        rot_pid = backend_pid(rot["conn"])

        def rotate():
            conn = rot["conn"]
            try:
                with conn.transaction():
                    # the settings row first, as every rotation door does
                    outbound_pause.pause_for_rotation(conn, squat["id"])
                    gate._pause()
                    # then the member lock, waiting out invite claims
                    reset_mod.evict_invite_footholds(conn, squat["id"])
            except BaseException as e:                   # noqa: BLE001
                rot["error"] = e

        def reclaim():
            try:
                rec["resp"] = TestClient(self.appmod.app).post(
                    "/signup/reclaim",
                    data={"token": token, "password": "the-clickers-password"},
                    follow_redirects=False)
            except BaseException as e:                   # noqa: BLE001
                rec["error"] = e

        rec: dict = {}
        a = threading.Thread(target=rotate, daemon=True)
        b = threading.Thread(target=reclaim, daemon=True)
        before = deadlocks()
        try:
            a.start()
            self.assertTrue(gate.reached.wait(20),
                            "the rotation never took the settings row")
            b.start()
            self.assertTrue(_another_backend_waits(rot_pid),
                            "the reclaim never met the rotation")
            gate.go.set()
            a.join(30)
            b.join(30)
            self.assertFalse(a.is_alive(), "the rotation hung")
            self.assertFalse(b.is_alive(), "the reclaim hung")
        finally:
            gate.go.set()
            try:
                rot["conn"].close()
            except Exception:                            # noqa: BLE001
                pass

        self.assertIsNone(rot.get("error"), rot.get("error"))
        self.assertIsNone(rec.get("error"), rec.get("error"))
        self.assertEqual(deadlocks_since(before), 0, "the two deadlocked")
        self.assertEqual(rec["resp"].status_code, 303, rec["resp"].text)
        after = self._user(email)
        self.assertNotEqual(after["tenant_id"], squat["tenant_id"])
        self.assertIsNotNone(after["verified_at"])
        admin = tenancy.admin_connect()
        try:
            self.assertIsNone(admin.execute(
                "SELECT 1 FROM tenants WHERE id=%s",
                (squat["tenant_id"],)).fetchone())
        finally:
            admin.close()

    def test_a_household_that_gains_a_person_before_the_wipe_is_left_whole(self):
        """The member lock is let go between the decision and the wipe, so
        the wipe asks again under the settings row and the member lock. A
        person seated in between (a claim of an invite the owner minted)
        makes the household somebody's: it is left in place, with that
        person in it, and the click is refused."""
        from oikonome import erasure
        email = f"gain-{uuid.uuid4().hex[:8]}@x.dev"
        self.assertEqual(self._signup(email).status_code, 200)
        squat = self._user(email)
        self.assertEqual(self._signup(email, password="owner-real-password")
                         .status_code, 409)
        token = self.sent[-1][1]
        member = f"mem-{uuid.uuid4().hex[:8]}@x.dev"
        real_release = erasure.release_external

        def a_claim_lands(tid):
            admin = tenancy.admin_connect()
            try:
                admin.execute(
                    "INSERT INTO users (tenant_id, email, password_hash, role)"
                    " VALUES (%s, %s, 'x', 'member')", (tid, member))
            finally:
                admin.close()
            return real_release(tid)

        with mock.patch.object(erasure, "release_external", a_claim_lands):
            r = TestClient(self.appmod.app).post(
                "/signup/reclaim",
                data={"token": token, "password": "the-clickers-password"},
                follow_redirects=False)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self._user(email)["tenant_id"], squat["tenant_id"])
        self.assertEqual(self._user(member)["tenant_id"], squat["tenant_id"])


if __name__ == "__main__":
    unittest.main()
