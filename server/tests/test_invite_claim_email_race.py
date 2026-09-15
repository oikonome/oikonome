"""Claiming an invite must not race another writer for the email address.

The taken-email check and the member INSERT are separate statements, so two
concurrent claims for one address (or a claim racing /api/signup) could both
pass the exists-check; the loser then hit the users.email UNIQUE index and
surfaced as a 500 instead of the one generic claim-failed answer. Claim
serializes per email under the same advisory key signup uses, and maps a
unique-violation to the generic failure as a backstop.

The race is pinned open with a barrier: both claimers are parked right
after the taken-email read. Serialized, only one can be inside that window,
so the barrier times out; unserialized, both arrive together and interleave
exactly like two concurrent requests.
"""

import contextlib
import threading
import unittest
import uuid

import psycopg
from psycopg.rows import dict_row

from oikonome.auth import invites
from oikonome.db import tenancy

from .util import _ensure_db

_WAIT = 3.0


def _control():
    return psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                           autocommit=True)


class _PauseAfterEmailCheck:
    """Connection proxy that parks the caller at a barrier right after the
    taken-email read — after the check, before the INSERT."""

    def __init__(self, conn, barrier):
        self._conn = conn
        self._barrier = barrier

    def execute(self, sql, *a, **kw):
        cur = self._conn.execute(sql, *a, **kw)
        if "SELECT 1 FROM users WHERE email" in sql:
            with contextlib.suppress(threading.BrokenBarrierError):
                self._barrier.wait(timeout=_WAIT)
        return cur

    def __getattr__(self, name):
        return getattr(self._conn, name)


class InviteClaimEmailRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(
                admin, f"claimrace-{uuid.uuid4().hex[:8]}")
            self.owner = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, %s, 'owner') RETURNING id",
                (self.tid, f"own-{uuid.uuid4().hex[:8]}@example.dev",
                 "x")).fetchone()["id"]
        finally:
            admin.close()

    def test_two_claims_for_one_email_create_one_member(self):
        """Exactly one racer wins; the loser gets the generic claim-failed
        answer, never an unhandled unique-violation, and exactly one user
        row exists for the address."""
        email = f"claimee-{uuid.uuid4().hex[:8]}@example.dev"
        cc = _control()
        self.addCleanup(cc.close)
        toks = [invites.create(cc, self.tid, self.owner)["token"]
                for _ in range(2)]

        barrier = threading.Barrier(2)
        results, errors = [], []

        def claim(token):
            conn = _control()
            try:
                results.append(invites.claim(
                    _PauseAfterEmailCheck(conn, barrier), token, email,
                    "correct-horse-battery"))
            except Exception as e:               # noqa: BLE001
                errors.append(e)
            finally:
                conn.close()

        threads = [threading.Thread(target=claim, args=(t,)) for t in toks]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(results), 1,
                         f"expected exactly one winning claim, got "
                         f"{len(results)} ({errors})")
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], ValueError,
                              "the losing claim must surface the generic "
                              "claim-failed answer, not a database error")
        self.assertIn("couldn't create an account", str(errors[0]))
        rows = cc.execute("SELECT id FROM users WHERE email=%s",
                          (email,)).fetchall()
        self.assertEqual(len(rows), 1,
                         "the email-uniqueness race let both claims insert")


if __name__ == "__main__":
    unittest.main()
