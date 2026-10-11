"""An owner's credential rotation and a member's own rotation, on one
household at one moment, keep one lock order and both finish.

An owner's rotation removes the members who joined in the last days
through the owner's invites. It holds the household-member lock while it
does, and takes each removed member's factor lock under it before deleting
the row. A member's own rotation (a password change, an authenticator
turned on or off, a first passkey) takes that member's factor lock first,
because every account-security door does. If the member's door then asked
for the household-member lock too, each side would hold what the other
wants: Postgres breaks the cycle by killing one of them, usually the
owner, whose whole rotation then rolls back with a 500 and leaves every
foothold it was closing in place. Exactly the member being removed (an
account a hijacker planted through a stolen owner session) can provoke
that on purpose.

So a member's rotation never waits on the household-member lock: it
removes nobody, and cancelling its own pending invites needs only those
invite rows. Here a member's door is parked holding its factor lock while
the owner's password change queues behind it; once released, both must
commit, no deadlock may be counted, and the member must be gone.
"""

import os
import threading
import time
import unittest
import uuid
from unittest import mock

import psycopg
from psycopg.rows import dict_row
from fastapi.testclient import TestClient

from oikonome.auth import invites
from oikonome.auth import reset as reset_mod
from oikonome.db import tenancy

from .lock_order_util import Gate, deadlocks, deadlocks_since
from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"
NEW = "a-brand-new-passphrase"


def _code(secret):
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)


def _some_backend_waits_on_a_lock(timeout: float = 15.0) -> bool:
    """True once a backend in this database is queued behind a lock — the
    doors run on the app's pooled connections, which the test cannot
    name."""
    admin = tenancy.admin_connect()
    try:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if admin.execute(
                    "SELECT 1 FROM pg_locks l"
                    "  JOIN pg_stat_activity a ON a.pid = l.pid"
                    " WHERE NOT l.granted"
                    "   AND a.datname = current_database()").fetchone():
                return True
            time.sleep(0.02)
        return False
    finally:
        admin.close()


class OwnerAndMemberRotationsTakeOneOrder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self._env = {k: os.environ.pop(k, None) for k in (
            "OIKONOME_HOSTED", "OIKONOME_RECOVERY_MEMBER_WINDOW_DAYS")}
        self.addCleanup(self._restore_env)
        self.email = f"own-{uuid.uuid4().hex[:8]}@example.dev"
        self.owner = TestClient(self.appmod.app,
                                raise_server_exceptions=False)
        r = self.owner.post("/api/signup", data={"email": self.email,
                                                 "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.cc = psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                                  autocommit=True)
        self.addCleanup(self.cc.close)
        row = self.cc.execute("SELECT id, tenant_id FROM users WHERE email=%s",
                              (self.email,)).fetchone()
        self.uid, self.tid = row["id"], row["tenant_id"]
        # a member who joined through the owner's invite just now: the one
        # the owner's rotation removes
        self.member_email = f"mem-{uuid.uuid4().hex[:8]}@example.dev"
        tok = invites.create(self.cc, self.tid, self.uid, role="member",
                             label=self.member_email)["token"]
        self.member_id = invites.claim(self.cc, tok, self.member_email,
                                       PW)["user_id"]
        self.member = TestClient(self.appmod.app,
                                 raise_server_exceptions=False)
        r = self.member.post("/api/login", data={"email": self.member_email,
                                                 "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def _restore_env(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _race(self, gate: Gate, member_door) -> tuple:
        """Run `member_door` until it reaches the gate (holding its factor
        lock), queue the owner's password change behind it, then let the
        member go on. Returns (member response, owner response)."""
        out: dict = {}

        def member():
            out["member"] = member_door()

        def owner():
            out["owner"] = self.owner.post("/api/password/change", json={
                "current_password": PW, "new_password": NEW})

        a = threading.Thread(target=member, daemon=True)
        b = threading.Thread(target=owner, daemon=True)
        before = deadlocks()
        try:
            a.start()
            self.assertTrue(gate.reached.wait(20),
                            "the member's door never reached the gate")
            b.start()
            self.assertTrue(_some_backend_waits_on_a_lock(),
                            "the owner's rotation never met the member's")
        finally:
            gate.go.set()
        a.join(30)
        b.join(30)
        self.assertFalse(a.is_alive(), "the member's door hung")
        self.assertFalse(b.is_alive(), "the owner's rotation hung")
        self.assertEqual(deadlocks_since(before), 0,
                         "the owner's and the member's rotations deadlocked")
        return out["member"], out["owner"]

    def _assert_owner_finished(self, owner):
        self.assertEqual(owner.status_code, 200, owner.text)
        self.assertEqual([m["email"] for m in owner.json()["members_removed"]],
                         [self.member_email])
        self.assertIsNone(self.cc.execute(
            "SELECT 1 FROM users WHERE id=%s", (self.member_id,)).fetchone(),
            "the member the owner's invite created survived the rotation")

    def test_a_members_authenticator_enrolment_lets_the_owners_rotation_finish(self):
        """The member is turning an authenticator on (holding their factor
        lock and their users row) when the owner's password change reaches
        for that factor lock to remove them. The member's door commits
        first and the owner's rotation then removes the member: neither is
        killed."""
        secret = self.member.post("/api/totp/enroll",
                                  data={"password": PW}).json()["secret"]
        code = _code(secret)
        gate = Gate()
        with mock.patch.object(
                self.appmod, "_revoke_bearer_persistence",
                gate.before(self.appmod._revoke_bearer_persistence)):
            member, owner = self._race(gate, lambda: self.member.post(
                "/api/totp/confirm",
                data={"secret": secret, "code": code, "password": PW}))
        self.assertEqual(member.status_code, 200, member.text)
        self._assert_owner_finished(owner)

    def test_a_members_password_change_lets_the_owners_rotation_finish(self):
        """The member is changing their own password (holding their factor
        lock) when the owner's password change reaches for it. Both
        commit, the member's first, and the member is then removed."""
        gate = Gate()
        with mock.patch.object(
                reset_mod, "evict_invite_footholds",
                gate.before(reset_mod.evict_invite_footholds)):
            member, owner = self._race(gate, lambda: self.member.post(
                "/api/password/change",
                json={"current_password": PW, "new_password": NEW}))
        self.assertEqual(member.status_code, 200, member.text)
        self.assertEqual(member.json().get("members_removed"), [])
        self._assert_owner_finished(owner)

    def test_a_members_rotation_never_takes_the_household_member_lock(self):
        """The direct form of the rule both races above depend on: while
        a claim holds the household-member lock, a member's own rotation
        still cancels that member's pending invites and returns at once."""
        later = invites.create(self.cc, self.tid, self.member_id,
                               role="member", label="later@example.dev")
        hold = tenancy.admin_connect()
        try:
            with hold.transaction():
                hold.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                             ("oikonome:tenant-members:" + str(self.tid),))
                conn = tenancy.admin_connect()
                try:
                    conn.execute("SET lock_timeout = '2s'")
                    cancelled, removed = reset_mod.evict_invite_footholds(
                        conn, self.member_id)
                finally:
                    conn.close()
        finally:
            hold.close()
        self.assertEqual((cancelled, removed), (1, []))
        self.assertIsNone(self.cc.execute(
            "SELECT 1 FROM invites WHERE token_hash=%s",
            (later["token_hash"],)).fetchone(),
            "the member's pending invite outlived their rotation")


if __name__ == "__main__":
    unittest.main()
