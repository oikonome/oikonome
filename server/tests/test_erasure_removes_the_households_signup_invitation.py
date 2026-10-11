"""Erasure removes the operator's signup invitation for the household: the
person's address, the operator's note about them and the id of the tenant
it minted. signup_invites is control-plane and keyed by email, so the
tenant_id-column sweep never reaches it. An invite to an address another
household still signs in with is that household's, and stays. So does a
live, unused invite to the household's own address: it is the operator's
standing offer for a household not yet created — a signup reclaim wipes
the old, unverified household and then consumes exactly that invite, so
erasing it would sign the person up without the terms it carried."""
import hashlib
import secrets
import unittest
import uuid

from oikonome.db import tenancy

from .util import _ensure_db


def _addr(tag):
    return f"inv-{tag}-{uuid.uuid4().hex[:8]}@example.dev"


class ErasureRemovesSignupInvitation(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect()
        self.addCleanup(self.admin.close)

    def _invite(self, email, used_by=None, expires="1 day"):
        iid = self.admin.execute(
            "INSERT INTO signup_invites (token_hash, email, note, expires_at,"
            " used_at, used_by_tenant) VALUES (%s, %s, 'referred by a friend',"
            " now() + %s::interval, CASE WHEN %s::uuid IS NULL THEN NULL"
            " ELSE now() END, %s) RETURNING id",
            (hashlib.sha256(secrets.token_bytes(16)).hexdigest(), email,
             expires, used_by, used_by)).fetchone()["id"]
        self.addCleanup(self.admin.execute,
                        "DELETE FROM signup_invites WHERE id=%s", (iid,))
        return iid

    def _exists(self, iid):
        return self.admin.execute(
            "SELECT 1 FROM signup_invites WHERE id=%s", (iid,)).fetchone()

    def _tenant_with(self, email):
        tid = tenancy.create_tenant(self.admin, f"inv-{uuid.uuid4().hex[:8]}")
        self.admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, role) "
            "VALUES (%s, %s, 'x', 'owner')", (tid, email))
        return tid

    def test_the_households_invitation_goes_with_it(self):
        me = _addr("me")
        tid = self._tenant_with(me)
        used = self._invite(me.upper(), used_by=tid)
        lapsed = self._invite(me, expires="-1 day")
        tenancy.delete_tenant_rows(self.admin, tid)
        self.assertIsNone(self._exists(used))
        self.assertIsNone(self._exists(lapsed))

    def test_a_live_unused_invite_survives_for_the_signup_it_belongs_to(self):
        # the reclaim road: an unverified household holds the address, the
        # operator's invite for it is parked unused, the wipe runs first
        me = _addr("reclaim")
        tid = self._tenant_with(me)
        offer = self._invite(me)
        tenancy.delete_tenant_rows(self.admin, tid)
        self.assertIsNotNone(self._exists(offer))

    def test_an_invite_to_another_households_user_stays(self):
        shared = _addr("shared")
        tid = self._tenant_with(shared)
        other = self._tenant_with(shared.upper())
        self.addCleanup(tenancy.delete_tenant_rows, self.admin, other)
        theirs = self._invite(shared, used_by=other)
        tenancy.delete_tenant_rows(self.admin, tid)
        self.assertIsNotNone(self._exists(theirs))


if __name__ == "__main__":
    unittest.main()
