"""A recipient entry stored before addresses were checked one by one
(`a@x;b@y`, `Name <a@x>`) is the mailboxes it names — everywhere the list
is read, written, or pruned.

The sender splits such an entry into its mailboxes. Every other door has to
agree, or the list quietly stops meaning what the household agreed to:

* a Settings save re-posts the stored list, which the save splits; compared
  against the raw stored strings, each split mailbox looks new and a
  non-member is dropped without a word;
* removing or leaving a member must take their mailbox out of whatever entry
  holds it, or the split keeps mailing the person who left;
* a consent recorded under `Name <a>` is `a`'s, and survives the save that
  rewrites the entry to `a`; one recorded under `a;b` is not two people's;
* on hosted, the members-only rule reads mailboxes, so `Name <member>` is
  still a member;
* leaving takes the address off the lists only after the account is gone,
  so a settings page saved in between cannot put it back.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import (TEST_DB, _admin_dsn, _ensure_db, accept_recipient_invite,
                   seed_accounts, write_config)

PW = "correct-horse-battery"
MEMBER_PW = "member-horse-battery"


def _u(tag):
    return f"{tag}-{uuid.uuid4().hex[:8]}@example.dev"


class _Household(unittest.TestCase):
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
        self.owner_email = _u("own")
        self.owner.post("/api/signup", data={"email": self.owner_email,
                                             "password": PW})
        self.tid = self.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(self.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()

    def _store(self, recips):
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["email_recipients"] = list(recips)
        finally:
            conn.close()

    def _recipients(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return [str(x).lower() for x in
                    budget.load_config(conn).get("email_recipients") or []]
        finally:
            conn.close()

    def _invite_state(self, email):
        from oikonome.notify import recipient_invites
        return (recipient_invites.states(self.tid, [email])
                .get(email.lower()) or {}).get("state")

    def _member(self):
        r = self.owner.post("/api/invites", json={
            "label": "partner", "role": "member", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        email = _u("member")
        client = TestClient(self.app)
        got = client.post("/api/invite/claim", json={
            "token": r.json()["url"].rsplit("token=", 1)[1],
            "email": email, "password": MEMBER_PW})
        self.assertEqual(got.status_code, 200, got.text)
        uid = next(u["id"] for u in
                   self.owner.get("/api/users").json()["users"]
                   if u["email"] == email)
        return email, uid, client


class SettingsSaveKeepsLegacyEntries(_Household):
    def setUp(self):
        super().setUp()
        self.a, self.b, self.c = _u("a"), _u("b"), _u("c")
        self.stored = [self.owner_email, f"{self.a};{self.b}",
                       f"Some One <{self.c}>"]
        self._store(self.stored)

    def test_unrelated_save_keeps_every_mailbox(self):
        """The client echoes the stored list on a mute toggle, naming no
        adds; every mailbox those entries name must still be on the list."""
        r = self.owner.post("/api/settings", json={
            "email_recipients": self.stored, "email_muted": [],
            "email_recipients_added": []})
        self.assertEqual(r.status_code, 200, r.text)
        got = self._recipients()
        for m in (self.a, self.b, self.c):
            self.assertIn(m, got, "a legacy-form recipient was dropped")

    def test_legacy_form_save_keeps_every_mailbox(self):
        r = self.owner.post("/settings/email", data={
            "email_recipients": ", ".join(self.stored),
            "email_send_hour_utc": "14"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        got = self._recipients()
        for m in (self.a, self.b, self.c):
            self.assertIn(m, got)

    def test_display_entry_consent_moves_to_its_mailbox(self):
        accept_recipient_invite(self.tid, f"Some One <{self.c}>")
        r = self.owner.post("/api/settings", json={
            "email_recipients": self.stored, "email_recipients_added": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._invite_state(self.c), "accepted",
                         "the save lost a recipient's yes")

    def test_joined_entry_consent_does_not_cover_both_mailboxes(self):
        accept_recipient_invite(self.tid, f"{self.a};{self.b}")
        with mock.patch("oikonome.web.mailguard.invite_added") as inv:
            r = self.owner.post("/api/settings", json={
                "email_recipients": self.stored,
                "email_recipients_added": []})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotEqual(self._invite_state(self.a), "accepted")
        self.assertNotEqual(self._invite_state(self.b), "accepted")
        # asked one by one instead; the display entry's person is not
        invited = [e.lower() for e in inv.call_args.args[1]]
        self.assertEqual(sorted(invited), sorted([self.a, self.b]))

    def test_mailbox_inside_a_stored_entry_can_be_reinvited(self):
        """The sender mails each mailbox of a joined entry, so each is a
        saved recipient the owner can re-send an invite to."""
        with mock.patch("oikonome.web.mailguard.invite_added") as inv:
            r = self.owner.post("/api/settings/recipients/resend",
                                json={"email": self.b})
        self.assertEqual(r.status_code, 200, r.text)
        inv.assert_called_once()

    def test_bare_mailbox_decline_is_not_overwritten(self):
        accept_recipient_invite(self.tid, f"Some One <{self.c}>")
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        try:
            admin.execute(
                "INSERT INTO recipient_invites (tenant_id, email, "
                "declined_at) VALUES (%s, %s, now())", (self.tid, self.c))
        finally:
            admin.close()
        self.owner.post("/api/settings", json={
            "email_recipients": self.stored, "email_recipients_added": []})
        self.assertEqual(self._invite_state(self.c), "declined")


class RemovalReachesLegacyEntries(_Household):
    def test_removed_member_leaves_display_and_joined_entries(self):
        m, uid, _ = self._member()
        other = _u("other")
        self._store([self.owner_email, f"Pat <{m}>", f"{m};{other}"])
        r = self.owner.request("DELETE", f"/api/users/{uid}",
                               json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        got = self._recipients()
        self.assertFalse(any(m in e for e in got),
                         f"removed member still on the list: {got}")
        self.assertIn(other, got, "the joined entry's other person was lost")

    def test_leaver_leaves_display_entry(self):
        m, _uid, client = self._member()
        self._store([self.owner_email, f"Pat <{m}>"])
        r = client.post("/api/account/leave", data={"password": MEMBER_PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(any(m in e for e in self._recipients()))

    def test_leaver_is_dropped_only_after_the_account_is_gone(self):
        """Dropped while still a member, a stale settings save in between
        passes the stale-save rule (it only refuses non-members) and puts
        them back for good."""
        import oikonome.web.app as appmod
        m, uid, client = self._member()
        self._store([self.owner_email, m])
        real = appmod._drop_from_mail_lists
        seen = {}

        def spy(tenant_id, email):
            admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
            try:
                seen["row"] = admin.execute(
                    "SELECT 1 FROM users WHERE id = %s", (uid,)).fetchone()
            finally:
                admin.close()
            return real(tenant_id, email)
        with mock.patch.object(appmod, "_drop_from_mail_lists", spy):
            r = client.post("/api/account/leave",
                            data={"password": MEMBER_PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("row", seen, "the leaver was never dropped")
        self.assertIsNone(seen["row"],
                          "mail lists were edited while the leaver was "
                          "still a member")
        self.assertNotIn(m, self._recipients())


class SenderChecksEachMailbox(unittest.TestCase):
    """worker._recipients_raw: acceptance and membership per mailbox."""

    def _run(self, stored, states, hosted=False, members=()):
        from oikonome.jobs import worker
        admin = mock.MagicMock()

        def execute(sql, params=None):
            res = mock.MagicMock()
            if "role='owner'" in sql:
                res.fetchall.return_value = [{"email": "owner@example.dev"}]
            else:
                res.fetchall.return_value = [{"email": e} for e in members]
            return res
        admin.execute.side_effect = execute
        with mock.patch.dict(os.environ,
                             {"OIKONOME_HOSTED": "1" if hosted else "",
                              "OIKONOME_HOSTED_FREEFORM_RECIPIENTS": ""}), \
             mock.patch("oikonome.engine.budget.load_config",
                        return_value={"email_recipients": stored}), \
             mock.patch.object(worker.tenancy, "admin_connect",
                               return_value=admin), \
             mock.patch("oikonome.notify.recipient_invites.states",
                        side_effect=lambda t, es: {
                            e.lower(): {"state": states[e.lower()]}
                            for e in es if e.lower() in states}):
            return worker._recipients_raw(None, "t")

    def test_one_consent_on_a_joined_entry_mails_nobody_else(self):
        out = self._run(["a@x.example;b@y.example"],
                        {"a@x.example;b@y.example": "accepted",
                         "a@x.example": "accepted"})
        self.assertEqual(out, ["owner@example.dev", "a@x.example"])

    def test_display_entry_consent_is_its_mailboxs(self):
        out = self._run(["Some One <c@z.example>"],
                        {"some one <c@z.example>": "accepted"})
        self.assertEqual(out, ["owner@example.dev", "c@z.example"])

    def test_mailboxs_own_decline_beats_the_entrys_consent(self):
        out = self._run(["Some One <c@z.example>"],
                        {"some one <c@z.example>": "accepted",
                         "c@z.example": "declined"})
        self.assertEqual(out, ["owner@example.dev"])

    def test_hosted_member_in_display_form_is_still_mailed(self):
        out = self._run(["Mem Ber <mem@x.example>"],
                        {"mem@x.example": "accepted"},
                        hosted=True, members=("mem@x.example",))
        self.assertEqual(out, ["owner@example.dev", "mem@x.example"])


if __name__ == "__main__":
    unittest.main()
