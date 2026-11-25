"""Who receives the household's daily mail is the owner's roster.

The recipient list names every mailbox the household's money is sent to
each morning — people a member may never have been introduced to. The
owner reads the whole list; since the household's setup became the
owner's alone to read, anyone else reads none of it — not
even their own entry — and learns what their card needs from their own
status row and the two list facts below. The write side
stays coherent: a member's save cannot touch the list at all (the keys
are the owner's, dropped from a member's body), so a member saving the
one entry they were shown never deletes the addresses they were not."""

import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db

PW = "correct-horse-battery"
MEMBER_PW = "family-member-pass"


class MembersSeeOnlyTheirOwnRecipientAddress(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.owner = TestClient(cls.app)
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
        cls.member = TestClient(cls.app)
        cls.member_email = f"member-{uuid.uuid4().hex[:8]}@example.dev"
        got = cls.member.post("/api/invite/claim", json={
            "token": token, "email": cls.member_email,
            "password": MEMBER_PW})
        assert got.status_code == 200, got.text
        cls.others = [f"aunt-{uuid.uuid4().hex[:6]}@example.dev",
                      f"sitter-{uuid.uuid4().hex[:6]}@example.dev"]

    def setUp(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["email_recipients"] = [self.member_email] + self.others
        finally:
            conn.close()

    def _stored(self) -> list:
        conn = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(conn).get("email_recipients") or []
        finally:
            conn.close()

    def test_the_owner_sees_every_address(self):
        got = self.owner.get("/api/settings").json()["email_recipients"]
        self.assertEqual(sorted(got),
                         sorted([self.member_email] + self.others))

    def test_a_member_sees_only_their_own(self):
        got = self.member.get("/api/settings").json()
        self.assertNotIn("email_recipients", got)
        for row in got.get("email_recipient_status") or []:
            self.assertEqual(row["email"].lower(), self.member_email)

    def test_a_member_save_reply_shows_only_their_own(self):
        r = self.member.post("/api/settings", json={"food_monthly": 321})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("email_recipients", r.json())

    def test_a_member_round_trip_keeps_the_addresses_it_was_not_shown(self):
        # what a member is shown, posted back as the clients do: the list is
        # not in it, and an older client's copy of their own entry is ignored
        shown = self.member.get("/api/settings").json()
        r = self.member.post("/api/settings", json={
            **{k: v for k, v in shown.items() if k == "custom_buckets"},
            "email_recipients": [self.member_email],
            "email_recipients_added": [], "food_monthly": 322})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(self._stored()),
                         sorted([self.member_email] + self.others))

    def test_a_member_cannot_remove_someone_elses_address(self):
        r = self.member.post("/api/settings", json={
            "email_recipients_removed": [self.others[0]]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(self.others[0], self._stored())
        r = self.member.post("/api/settings", json={
            "email_recipients": [self.member_email]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sorted(self._stored()),
                         sorted([self.member_email] + self.others))

    def _store(self, value) -> None:
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                if value is None:
                    cfg.pop("email_recipients", None)
                else:
                    cfg["email_recipients"] = value
        finally:
            conn.close()

    # Who the worker mails is a configured list's mailboxes (plus the
    # owner), or every verified login when nothing is configured. A member
    # no longer sees the roster, so the view states the two facts their
    # card needs, worked out on the server from the whole stored list:
    #   nothing configured           -> configured False, on list False
    #   a list without the member    -> configured True,  on list False
    #   a list naming the member     -> configured True,  on list True
    #     (alone, or inside an older joined entry the worker splits)

    def test_a_member_left_off_a_list_is_told_the_list_is_set_without_them(self):
        self._store(list(self.others))
        got = self.member.get("/api/settings").json()
        self.assertNotIn("email_recipients", got)
        self.assertIs(got["email_list_configured"], True)
        self.assertIs(got["email_on_list"], False)
        r = self.member.post("/api/settings", json={"food_monthly": 323})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(r.json()["email_list_configured"], True)
        self.assertIs(r.json()["email_on_list"], False)

    def test_a_member_named_inside_a_joined_entry_is_on_the_list(self):
        self._store([f"{self.others[0]}; {self.member_email.upper()}",
                     self.others[1]])
        got = self.member.get("/api/settings").json()
        self.assertIs(got["email_list_configured"], True)
        self.assertIs(got["email_on_list"], True)
        # the list itself stays the owner's
        self.assertNotIn("email_recipients", got)

    def test_a_member_on_the_list_is_on_it(self):
        got = self.member.get("/api/settings").json()
        self.assertIs(got["email_list_configured"], True)
        self.assertIs(got["email_on_list"], True)

    def test_with_nothing_configured_no_list_decides_who_is_mailed(self):
        self._store(None)
        got = self.member.get("/api/settings").json()
        self.assertIs(got["email_list_configured"], False)
        self.assertIs(got["email_on_list"], False)


if __name__ == "__main__":
    unittest.main()
