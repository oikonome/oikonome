"""Removing a person from the household ends the invites and webhooks they
made, not only their session and script tokens.

`invites.created_by` and `webhooks.created_by_user` are ON DELETE SET NULL,
so once the users row is gone no rotation door can match those rows to
anyone. The invariant: after an owner removes a second owner, that person's
unclaimed share link cannot be claimed (it would let them rejoin and undo
the removal) and every hook they created is switched off (it would keep
posting the household's rows to their URL). Hooks made by the remaining
owner are untouched, and an id from another household removes nothing.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import invites
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


def _admin():
    return tenancy.admin_connect()


class RemovedMemberInvitesAndWebhooksEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.owner = TestClient(self.app)
        self.owner_email = f"own-{uuid.uuid4().hex[:8]}@example.dev"
        self.owner.post("/api/signup", data={"email": self.owner_email,
                                             "password": PW})
        me = self.owner.get("/api/me").json()
        self.tid = me["tenant_id"]
        admin = _admin()
        try:
            self.owner_id = admin.execute(
                "SELECT id FROM users WHERE email=%s",
                (self.owner_email,)).fetchone()["id"]
            self.b_email = f"own2-{uuid.uuid4().hex[:6]}@example.dev"
            self.b = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,'h','owner',now()) RETURNING id",
                (self.tid, self.b_email)).fetchone()["id"]
        finally:
            admin.close()

    def _hook(self, maker_id, maker_email) -> str:
        admin = _admin()
        try:
            return str(admin.execute(
                "INSERT INTO webhooks (tenant_id, url, secret, events, "
                "created_by, created_by_user) "
                "VALUES (%s, %s, 'sealed', ARRAY['*'], %s, %s) RETURNING id",
                (self.tid, f"https://hooks.example/{uuid.uuid4().hex}",
                 maker_email, maker_id)).fetchone()["id"])
        finally:
            admin.close()

    def _enabled(self, hook_id) -> bool:
        admin = _admin()
        try:
            return admin.execute("SELECT enabled FROM webhooks WHERE id=%s",
                                 (hook_id,)).fetchone()["enabled"]
        finally:
            admin.close()

    def _remove(self, uid):
        return self.owner.request("DELETE", f"/api/users/{uid}",
                                  json={"password": PW})

    def test_removed_owners_pending_invite_cannot_be_claimed(self):
        admin = _admin()
        try:
            token = invites.create(admin, self.tid, self.b,
                                   role="member")["token"]
        finally:
            admin.close()
        r = self._remove(self.b)
        self.assertEqual(r.status_code, 200, r.text)
        got = TestClient(self.app).post("/api/invite/claim", json={
            "token": token,
            "email": f"back-{uuid.uuid4().hex[:6]}@example.dev",
            "password": "rejoin-horse-battery"})
        self.assertNotEqual(got.status_code, 200,
                            "a removed owner's invite still claims a seat")

    def test_removed_owners_webhooks_are_switched_off(self):
        theirs = self._hook(self.b, self.b_email)
        mine = self._hook(self.owner_id, self.owner_email)
        r = self._remove(self.b)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self._enabled(theirs),
                         "a removed owner's webhook keeps posting")
        self.assertTrue(self._enabled(mine),
                        "the remaining owner's webhook was switched off")

    def test_an_id_from_another_household_touches_nothing(self):
        other = TestClient(self.app)
        other_email = f"oth-{uuid.uuid4().hex[:8]}@example.dev"
        other.post("/api/signup", data={"email": other_email,
                                        "password": PW})
        admin = _admin()
        try:
            row = admin.execute("SELECT id, tenant_id FROM users "
                                "WHERE email=%s", (other_email,)).fetchone()
            hook = str(admin.execute(
                "INSERT INTO webhooks (tenant_id, url, secret, events, "
                "created_by, created_by_user) "
                "VALUES (%s, 'https://hooks.example/x', 'sealed', "
                "ARRAY['*'], %s, %s) RETURNING id",
                (row["tenant_id"], other_email, row["id"])
            ).fetchone()["id"])
        finally:
            admin.close()
        r = self._remove(row["id"])
        self.assertEqual(r.status_code, 404, r.text)
        self.assertTrue(self._enabled(hook),
                        "another household's webhook was switched off")

    def test_leaving_ends_invites_and_webhooks_left_from_an_earlier_role(self):
        r = self.owner.post("/api/invites", json={
            "label": "fam", "role": "member", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        person = TestClient(self.app)
        email = f"fam-{uuid.uuid4().hex[:8]}@example.dev"
        r = person.post("/api/invite/claim", json={
            "token": r.json()["url"].rsplit("token=", 1)[1],
            "email": email, "password": "family-member-pass"})
        self.assertEqual(r.status_code, 200, r.text)
        admin = _admin()
        try:
            uid = admin.execute("SELECT id FROM users WHERE email=%s",
                                (email,)).fetchone()["id"]
            # made while this person was an owner, before a demotion
            token = invites.create(admin, self.tid, uid,
                                   role="member")["token"]
        finally:
            admin.close()
        hook = self._hook(uid, email)
        r = person.post("/api/account/leave",
                        data={"password": "family-member-pass"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self._enabled(hook),
                         "a departed person's webhook keeps posting")
        got = TestClient(self.app).post("/api/invite/claim", json={
            "token": token,
            "email": f"back-{uuid.uuid4().hex[:6]}@example.dev",
            "password": "rejoin-horse-battery"})
        self.assertNotEqual(got.status_code, 200,
                            "a departed person's invite still claims a seat")


if __name__ == "__main__":
    unittest.main()
