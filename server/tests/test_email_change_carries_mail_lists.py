"""Changing the login email moves the person's place on the household's
mail lists with them.

A mute is the person's own "do not mail me": it follows them to the new
address, or the opt-out is lost and the household's mail starts arriving
at the new mailbox. A recipient-list entry under the OLD address is
dropped: that mailbox is one the person just moved away from (and may not
control any more), and the new address never said yes to the household's
mail, so neither may be mailed on the strength of the old consent. The
reply says the entry went, so the client can tell the person.

Neither a drop on an email change nor one on a member's removal may leave
the configured list EMPTY: an absent list means "every verified user", so
an emptied one would widen the household's mail to people the owner left
off. It is left as the owner's own address, which is how a household keeps
its mail to the owner alone.

The lists are the owner's. A member or viewer may not change their login
onto an address already on either list, or they would hold that entry and
could drop it (by changing away) or mute it.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.jobs import worker

from .util import _ensure_db, accept_recipient_invite

PW = "correct-horse-battery"


class EmailChangeCarriesMailLists(unittest.TestCase):
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
        self.c = TestClient(self.app)
        self.old = f"mv-{uuid.uuid4().hex[:10]}@example.dev"
        r = self.c.post("/api/signup",
                        data={"email": self.old, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.c.get("/api/me").json()["tenant_id"]
        self.new = f"mv2-{uuid.uuid4().hex[:10]}@example.dev"
        self.partner = f"partner-{uuid.uuid4().hex[:6]}@example.dev"

    def _cfg(self, **over):
        conn = tenancy.tenant_connect(self.tid)
        try:
            if over:
                cfg = budget.load_config(conn)
                cfg.update(over)
                budget.save_config(conn, cfg)
            return budget.load_config(conn)
        finally:
            conn.close()

    def _mailed(self) -> set[str]:
        conn = tenancy.tenant_connect(self.tid)
        try:
            return {e.lower() for e in worker._recipients_raw(conn, self.tid)}
        finally:
            conn.close()

    def _change(self):
        r = self.c.post("/api/email/change",
                        data={"new_email": self.new, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def test_a_mute_follows_the_person_to_the_new_address(self):
        self._cfg(email_muted=[self.old.upper()])
        self.assertNotIn(self.old, self._mailed())
        self._change()
        self.assertNotIn(self.new, self._mailed())
        muted = [m.lower() for m in self._cfg()["email_muted"]]
        self.assertIn(self.new, muted)
        self.assertNotIn(self.old, muted)

    def test_the_old_address_leaves_the_recipient_list(self):
        self._cfg(email_recipients=[self.old, self.partner])
        accept_recipient_invite(self.tid, self.old, self.partner)
        self.assertIn(self.old, self._mailed())
        body = self._change()
        self.assertTrue(body.get("mail_list_entry_dropped"))
        mailed = self._mailed()
        self.assertNotIn(self.old, mailed)
        # an owner's email change is a credential rotation: the extra
        # recipient is paused until the owner confirms it is theirs
        self.assertNotIn(self.partner, mailed)
        paused = self.c.get("/api/settings").json()["outbound_paused"]
        self.assertEqual(paused["recipients"], [self.partner])
        # resuming is a step-up act: the legacy proof in the body
        r = self.c.post("/api/settings/outbound/resume",
                        json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(self.partner, self._mailed())
        recips = [r.lower() for r in self._cfg()["email_recipients"]]
        self.assertEqual(recips, [self.partner])

    def test_a_display_form_entry_is_matched_by_its_mailbox(self):
        self._cfg(email_recipients=[f"Me <{self.old}>", self.partner])
        accept_recipient_invite(self.tid, f"me <{self.old}>", self.partner)
        self._change()
        self.assertNotIn(self.old, self._mailed())

    def test_nothing_listed_reports_nothing_dropped(self):
        body = self._change()
        self.assertFalse(body.get("mail_list_entry_dropped"))


class ADropNeverWidensAndOnlyMovesYourOwnEntry(unittest.TestCase):
    """A household of an owner, a member and a viewer."""

    MEMBER_PW = "family-member-pass"

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
        r = self.owner.post("/api/signup", data={"email": self.owner_email,
                                                 "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.owner.post("/api/auth/elevate", json={"password": PW})
        self.tid = self.owner.get("/api/me").json()["tenant_id"]
        self.member, self.member_email = self._claim("member", "partner")
        self.viewer, self.viewer_email = self._claim("viewer", "reader")
        self.outside = f"accountant-{uuid.uuid4().hex[:6]}@example.dev"

    def _claim(self, role, label):
        r = self.owner.post("/api/invites", json={
            "label": label, "role": role, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["url"].rsplit("token=", 1)[1]
        client = TestClient(self.app)
        email = f"{role}-{uuid.uuid4().hex[:8]}@example.dev"
        got = client.post("/api/invite/claim", json={
            "token": token, "email": email, "password": self.MEMBER_PW})
        self.assertEqual(got.status_code, 200, got.text)
        return client, email

    def _cfg(self, **over):
        conn = tenancy.tenant_connect(self.tid)
        try:
            if over:
                cfg = budget.load_config(conn)
                cfg.update(over)
                budget.save_config(conn, cfg)
            return budget.load_config(conn)
        finally:
            conn.close()

    def _mailed(self) -> set[str]:
        conn = tenancy.tenant_connect(self.tid)
        try:
            return {e.lower() for e in worker._recipients_raw(conn, self.tid)}
        finally:
            conn.close()

    def _change(self, client, new, pw):
        return client.post("/api/email/change",
                           data={"new_email": new, "password": pw})

    def test_an_email_change_that_empties_the_list_keeps_it_owner_only(self):
        self._cfg(email_recipients=[self.member_email])
        accept_recipient_invite(self.tid, self.member_email)
        r = self._change(self.member,
                         f"m2-{uuid.uuid4().hex[:8]}@example.dev",
                         self.MEMBER_PW)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["mail_list_entry_dropped"])
        self.assertEqual(
            [x.lower() for x in self._cfg()["email_recipients"]],
            [self.owner_email])
        self.assertEqual(self._mailed(), {self.owner_email})

    def test_removing_the_only_listed_member_keeps_it_owner_only(self):
        self._cfg(email_recipients=[self.member_email])
        accept_recipient_invite(self.tid, self.member_email)
        admin = tenancy.admin_connect()
        try:
            uid = admin.execute("SELECT id::text AS id FROM users "
                                "WHERE email=%s", (self.member_email,)
                                ).fetchone()["id"]
        finally:
            admin.close()
        r = self.owner.request("DELETE", f"/api/users/{uid}",
                               json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            [x.lower() for x in self._cfg()["email_recipients"]],
            [self.owner_email])
        self.assertNotIn(self.viewer_email, self._mailed())

    def test_a_viewer_cannot_take_an_address_on_the_recipient_list(self):
        self._cfg(email_recipients=[self.outside])
        r = self._change(self.viewer, self.outside, self.MEMBER_PW)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("mail list", r.json()["detail"])
        self.assertEqual(self._cfg()["email_recipients"], [self.outside])
        me = self.viewer.get("/api/me").json()
        self.assertEqual(me["email"], self.viewer_email)

    def test_a_member_cannot_take_an_address_on_the_mute_list(self):
        self._cfg(email_muted=[self.outside])
        r = self._change(self.member, self.outside.upper(), self.MEMBER_PW)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(self._cfg()["email_muted"], [self.outside])

    def test_a_viewer_still_changes_to_an_address_on_no_list(self):
        self._cfg(email_recipients=[self.outside])
        new = f"v2-{uuid.uuid4().hex[:8]}@example.dev"
        r = self._change(self.viewer, new, self.MEMBER_PW)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._cfg()["email_recipients"], [self.outside])


if __name__ == "__main__":
    unittest.main()
