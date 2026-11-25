"""A member's settings read builds their own mail row on ONE admin
connection.

The row is the mailer's own answer (`worker._recipient_lists`) plus the
grid's facts for the member's address (`_recipient_status`), and each of
those used to open its own unpooled admin connection — the recipient list
one, the invitation read nested in it another, the grid's invitation read a
third, and the entry-consent read a fourth. The app shell fetches settings
on every mount and focus, and an unpooled admin connect is a fresh TCP and
auth round trip, so the row now takes one connection and hands it down.
The owner's read is not touched by this, and is pinned so it stays so."""

import contextlib
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.notify import recipient_invites

from .util import _ensure_db

PW = "correct-horse-battery"
MEMBER_PW = "family-member-pass"


class MemberMailRowTakesOneAdminConnection(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        from oikonome.web import security
        security._limiter._hits.clear()
        appmod.DEV_MODE = True
        cls.owner = TestClient(appmod.app)
        r = cls.owner.post("/api/signup", data={
            "email": f"own-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        assert r.status_code == 200, r.text
        cls.owner.post("/api/auth/elevate", json={"password": PW})
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        r = cls.owner.post("/api/invites", json={
            "label": "partner", "role": "member", "password": PW})
        assert r.status_code == 200, r.text
        token = r.json()["url"].rsplit("token=", 1)[1]
        cls.member = TestClient(appmod.app)
        cls.me = f"member-{uuid.uuid4().hex[:8]}@example.dev"
        got = cls.member.post("/api/invite/claim", json={
            "token": token, "email": cls.me, "password": MEMBER_PW})
        assert got.status_code == 200, got.text

    def _store(self, entries, accepted=()):
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM recipient_invites WHERE tenant_id=%s",
                          (self.tid,))
        finally:
            admin.close()
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["email_recipients"] = entries
        finally:
            conn.close()
        for addr in accepted:
            t = recipient_invites.invite(self.tid, addr)
            self.assertIsNotNone(recipient_invites.accept(t))

    @contextlib.contextmanager
    def _counting_admin_connects(self):
        n = {"calls": 0}
        real = tenancy.admin_connect

        def counted(*a, **kw):
            n["calls"] += 1
            return real(*a, **kw)
        with mock.patch.object(tenancy, "admin_connect", counted):
            yield n

    def test_a_member_on_the_list_costs_one_admin_connection(self):
        self._store([self.me], accepted=[self.me])
        with self._counting_admin_connects() as n:
            r = self.member.get("/api/settings")
        self.assertEqual(r.status_code, 200, r.text)
        rows = r.json()["email_recipient_status"]
        self.assertEqual([x["email"].lower() for x in rows], [self.me])
        self.assertTrue(rows[0]["mailed"])
        self.assertEqual(n["calls"], 1)

    def test_a_member_whose_consent_sits_on_the_entry_costs_one(self):
        # not mailed yet asked on the display-form entry: the path that
        # reads the entry's consent as well
        entry = f"Kid <{self.me}>"
        self._store([entry])
        recipient_invites.invite(self.tid, entry)
        with self._counting_admin_connects() as n:
            r = self.member.get("/api/settings")
        self.assertEqual(r.status_code, 200, r.text)
        rows = r.json()["email_recipient_status"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["not_mailed"], "invited")
        self.assertEqual(n["calls"], 1)

    def test_the_owners_read_is_unchanged(self):
        # the owner's grid: one invitation read for the whole list, no more
        self._store([self.me], accepted=[self.me])
        with self._counting_admin_connects() as n:
            r = self.owner.get("/api/settings")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["email_recipient_status"]), 2)
        self.assertEqual(n["calls"], 1)


if __name__ == "__main__":
    unittest.main()
