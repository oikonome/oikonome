"""Two doors that must decide against the household's real state.

- An invite claim whose burn committed before an owner's recovery, and
  whose member insert would land after it, is refused: the recovery cannot
  see or name a member that does not exist yet, so that member must never
  come to exist.
- A role change aimed at an owner (a second owner a restore brought back)
  is refused BEFORE the step-up, so it cannot spend a recovery code or a
  TOTP step on a change that can never happen.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import invites, passwords, reset
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class _RecoveryBetweenBurnAndInsert:
    """A connection that runs the owner's recovery right after the claim's
    burn commits — the window the claim's second transaction opens."""

    def __init__(self, conn, owner_id):
        self._conn, self._owner, self._n = conn, owner_id, 0

    def __getattr__(self, k):
        return getattr(self._conn, k)

    def transaction(self):
        self._n += 1
        if self._n == 2:
            admin = tenancy.admin_connect()
            try:
                with admin.transaction():
                    reset.revoke_everything(admin, self._owner)
            finally:
                admin.close()
        return self._conn.transaction()


class RecoveryOrdering(unittest.TestCase):

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
        self.email = f"ro-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(admin, self.email)
            self.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s, %s, %s, 'owner', now()) "
                "RETURNING id",
                (self.tid, self.email,
                 passwords.hash_password(PW))).fetchone()["id"]
        finally:
            admin.close()

    def test_a_claim_straddling_a_recovery_creates_no_member(self):
        admin = tenancy.admin_connect()
        try:
            inv = invites.create(admin, self.tid, self.uid, role="member")
        finally:
            admin.close()
        conn = tenancy.admin_connect()
        conn.autocommit = True
        try:
            with self.assertRaises(ValueError):
                invites.claim(_RecoveryBetweenBurnAndInsert(conn, self.uid),
                              inv["token"], f"m-{uuid.uuid4().hex[:6]}@x.dev",
                              "member-pass-phrase-1", mark_verified=True)
            n = conn.execute("SELECT count(*) AS n FROM users "
                             "WHERE tenant_id=%s", (self.tid,)).fetchone()["n"]
        finally:
            conn.close()
        self.assertEqual(n, 1)

    def test_an_owner_target_is_refused_before_the_step_up(self):
        admin = tenancy.admin_connect()
        try:
            second = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s, %s, 'x', 'owner', now()) "
                "RETURNING id",
                (self.tid, f"o2-{uuid.uuid4().hex[:6]}@x.dev")).fetchone()["id"]
        finally:
            admin.close()
        c = TestClient(self.appmod.app)
        r = c.post("/login", data={"email": self.email, "password": PW},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        # no step-up offered at all: the refusal must not need one
        r = c.post(f"/api/users/{second}/role", json={"role": "viewer"})
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
