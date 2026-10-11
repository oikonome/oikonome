"""Erasing one household never deletes the signup invitation another
household was born from. A login address can move between households (an
owner changes their email, the old address later joins someone else), so
the invite's address alone does not say whose record it is: the household
that spent it does. If the sweep matched on address only, erasing the
household that now holds the address would destroy the other, live
household's invitation — the operator's note and the options it was
created with."""
import hashlib
import secrets
import unittest
import uuid

from oikonome.db import tenancy

from .util import _ensure_db


class ErasureKeepsAnotherHouseholdsSpentInvite(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect()
        self.addCleanup(self.admin.close)

    def _tenant(self, email=None):
        tid = tenancy.create_tenant(self.admin, f"inv-{uuid.uuid4().hex[:8]}")
        if email:
            self.admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'owner')", (tid, email))
        return tid

    def _spent_invite(self, email, used_by):
        iid = self.admin.execute(
            "INSERT INTO signup_invites (token_hash, email, note, expires_at,"
            " used_at, used_by_tenant) VALUES (%s, %s, 'a note',"
            " now() + interval '1 day', now(), %s) RETURNING id",
            (hashlib.sha256(secrets.token_bytes(16)).hexdigest(), email,
             used_by)).fetchone()["id"]
        self.addCleanup(self.admin.execute,
                        "DELETE FROM signup_invites WHERE id=%s", (iid,))
        return iid

    def _exists(self, iid):
        return self.admin.execute(
            "SELECT 1 FROM signup_invites WHERE id=%s", (iid,)).fetchone()

    def test_an_address_that_moved_households_keeps_the_first_ones_invite(self):
        moved = f"inv-moved-{uuid.uuid4().hex[:8]}@example.dev"
        # the household that spent the invite now signs in elsewhere
        first = self._tenant(f"inv-new-{uuid.uuid4().hex[:8]}@example.dev")
        self.addCleanup(tenancy.delete_tenant_rows, self.admin, first)
        theirs = self._spent_invite(moved, used_by=first)
        # the address now belongs to a user of a different household
        holder = self._tenant(moved)
        tenancy.delete_tenant_rows(self.admin, holder)
        self.assertIsNotNone(self._exists(theirs))

    def test_the_erased_households_own_spent_invite_still_goes(self):
        me = f"inv-own-{uuid.uuid4().hex[:8]}@example.dev"
        tid = self._tenant(me)
        mine = self._spent_invite(me, used_by=tid)
        tenancy.delete_tenant_rows(self.admin, tid)
        self.assertIsNone(self._exists(mine))


if __name__ == "__main__":
    unittest.main()
