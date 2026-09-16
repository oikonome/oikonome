"""An invite creates the account it was addressed to, and no other.

The claim door is pre-auth: nothing in it proves anything about the person
typing into the form. An invite that names nobody therefore creates a user
row bearing whatever address is typed — and on hosted, where every
household shares ONE global `users.email` namespace, that let any account
holder plant a stranger's address inside their own household. The row
occupies the address against the stranger's own signup ("taken", for an
account they never made) and stands in the planter's household wearing the
stranger's name.

Two halves close it, and neither works without the other: an invite that
names an address may be claimed ONLY by that address, and on hosted the
mint door requires an address and hands the link to that mailbox alone —
never back to the minter. Delivery is then the mailbox proof this door
never had. Self-host keeps the bearer share-link: one household, no shared
namespace, and often no SMTP to deliver through at all.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import invites
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


def _control():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                           autocommit=True)


class _Household(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.app = appmod.app
        cls.owner = TestClient(cls.app)
        cls.owner_email = f"invbind-{uuid.uuid4().hex[:8]}@example.dev"
        r = cls.owner.post("/api/signup", data={
            "email": cls.owner_email, "password": PW})
        assert r.status_code == 200, r.text
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        cc = _control()
        try:
            cls.uid = cc.execute("SELECT id FROM users WHERE email=%s",
                                 (cls.owner_email,)).fetchone()["id"]
        finally:
            cc.close()

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.cc = _control()
        self.addCleanup(self.cc.close)

    def _has_user(self, email: str) -> bool:
        return self.cc.execute("SELECT 1 FROM users WHERE email=%s",
                               (email,)).fetchone() is not None


class NamedInviteBindsToItsAddress(_Household):
    def test_a_named_invite_refuses_every_other_address(self):
        alice = f"alice-{uuid.uuid4().hex[:8]}@example.dev"
        mallory = f"mallory-{uuid.uuid4().hex[:8]}@example.dev"
        token = invites.create(self.cc, self.tid, self.uid,
                               label=alice)["token"]
        r = TestClient(self.app).post("/api/invite/claim", json={
            "token": token, "email": mallory, "password": "family-member-pass"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("different email address", r.text)
        self.assertFalse(self._has_user(mallory),
                         "the refused claim must plant no account")
        # and the invitee's one link survives the attempt — the refusal
        # reads the invite, never the users table, so it need not be paid
        # for with the invite the way the taken-address answer is
        self.assertEqual(
            self.owner.get(f"/api/invite/peek?token={token}").status_code, 200)
        good = TestClient(self.app).post("/api/invite/claim", json={
            "token": token, "email": alice, "password": "family-member-pass"})
        self.assertEqual(good.status_code, 200, good.text)
        self.assertTrue(self._has_user(alice))

    def test_the_claim_page_is_told_which_address_to_use(self):
        """The binding the server enforces is the one the person sees —
        otherwise a locked-down door reads as a broken link."""
        alice = f"alice2-{uuid.uuid4().hex[:8]}@example.dev"
        token = invites.create(self.cc, self.tid, self.uid,
                               label=alice)["token"]
        peek = self.owner.get(f"/api/invite/peek?token={token}").json()
        self.assertEqual(peek["email"], alice)

    def test_an_address_is_matched_case_insensitively(self):
        mixed = f"Alice3-{uuid.uuid4().hex[:8]}@Example.Dev"
        token = invites.create(self.cc, self.tid, self.uid,
                               label=mixed)["token"]
        r = TestClient(self.app).post("/api/invite/claim", json={
            "token": token, "email": mixed.upper(),
            "password": "family-member-pass"})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_share_link_naming_nobody_still_creates_the_member(self):
        """Self-host's bearer link is unchanged: the label is a display
        name there, and binding nothing is what "share it any way you
        like" means."""
        token = invites.create(self.cc, self.tid, self.uid,
                               label="the tutor")["token"]
        email = f"tutor-{uuid.uuid4().hex[:8]}@example.dev"
        r = TestClient(self.app).post("/api/invite/claim", json={
            "token": token, "email": email, "password": "family-member-pass"})
        self.assertEqual(r.status_code, 200, r.text)


class HostedInvitesAreAddressedAndDeliveredOnly(_Household):
    """On hosted the link IS the mailbox proof, so it must reach the named
    mailbox and nowhere else."""

    def setUp(self):
        super().setUp()
        # the hosted write gate wants a second factor; this suite is about
        # the invite door, not enrollment
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET second_factor_waived=TRUE "
                          "WHERE id=%s", (self.uid,))
        finally:
            admin.close()
        os.environ["OIKONOME_HOSTED"] = "1"
        os.environ["OIKONOME_BASE_URL"] = "https://app.example.dev"
        self.addCleanup(os.environ.pop, "OIKONOME_HOSTED", None)
        self.addCleanup(os.environ.pop, "OIKONOME_BASE_URL", None)
        from oikonome.web import report
        self.sent = []
        real_resolve, real_send = report.resolve_smtp, report.send
        report.resolve_smtp = lambda conn: {"configured": True}
        report.send = lambda *a, **kw: self.sent.append((a, kw))
        self.addCleanup(setattr, report, "resolve_smtp", real_resolve)
        self.addCleanup(setattr, report, "send", real_send)

    def _pending(self):
        return self.owner.get("/api/invites").json()["invites"]

    def test_an_invite_naming_nobody_is_refused(self):
        before = len(self._pending())
        r = self.owner.post("/api/invites",
                            json={"label": "spouse", "password": PW})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("email address this invite is for", r.text)
        self.assertEqual(len(self._pending()), before,
                         "a refused mint must leave no invite behind")

    def test_the_link_reaches_the_mailbox_and_not_the_minter(self):
        alice = f"alice4-{uuid.uuid4().hex[:8]}@example.dev"
        r = self.owner.post("/api/invites",
                            json={"label": alice, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["emailed"])
        self.assertIsNone(body["url"],
                          "handing the link back to the minter would let "
                          "them claim the address themselves")
        self.assertEqual(len(self.sent), 1)
        args, _ = self.sent[0]
        self.assertEqual(args[3], [alice])
        self.assertIn("/app/invite?token=", args[1])

    def test_an_invite_that_could_not_be_emailed_is_destroyed(self):
        """An undeliverable invite is a live token nobody can be shown —
        it must not sit in the table waiting to be found."""
        from oikonome.web import report

        def boom(*a, **kw):
            raise RuntimeError("relay refused")

        report.send = boom
        before = len(self._pending())
        r = self.owner.post(
            "/api/invites",
            json={"label": f"alice5-{uuid.uuid4().hex[:8]}@example.dev",
                  "password": PW})
        self.assertEqual(r.status_code, 502, r.text)
        self.assertEqual(len(self._pending()), before)


if __name__ == "__main__":
    unittest.main()
