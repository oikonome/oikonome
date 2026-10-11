"""An owner's recovery removes members who joined through that owner's
own recent invites.

A stolen owner session can mint an invite to an address the thief holds
and claim it: a second account with its own password and its own second
factor, which nothing about the owner's credential reaches. Recovery
already cancels the owner's unclaimed invites; a claimed one inside the
recovery window must go too, or the thief keeps reading the household's
ledger as a member after the owner has taken the account back.

What it must NOT touch: members who joined before the window, members
someone else invited, the owner, and anything at all when it is a
member's own recovery rather than the owner's.
"""

import json
import os
import unittest
import uuid

import psycopg
from psycopg.rows import dict_row

from oikonome.auth import invites, reset as reset_mod
from oikonome.db import tenancy
from oikonome.notify import outbound_pause

from .util import _ensure_db

PW = "correct-horse-battery"


def _control():
    return psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                           autocommit=True)


class RecoveryRemovesRecentInviteesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self._env = {k: os.environ.pop(k, None) for k in (
            "OIKONOME_HOSTED", "OIKONOME_RECOVERY_MEMBER_WINDOW_DAYS")}
        self.addCleanup(self._restore_env)
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(
                admin, f"recmem-{uuid.uuid4().hex[:8]}")
            self.owner = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, %s, 'owner') RETURNING id",
                (self.tid, f"own-{uuid.uuid4().hex[:8]}@example.dev",
                 "x")).fetchone()["id"]
        finally:
            admin.close()
        self.cc = _control()
        self.addCleanup(self.cc.close)

    def _restore_env(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _join(self, minted_by, role="member"):
        email = f"m-{uuid.uuid4().hex[:8]}@example.dev"
        token = invites.create(self.cc, self.tid, minted_by, role=role,
                               label=email)["token"]
        return invites.claim(self.cc, token, email, PW)["user_id"]

    def _recover(self, user_id):
        admin = tenancy.admin_connect()
        try:
            with admin.transaction():
                reset_mod.revoke_everything(admin, user_id)
        finally:
            admin.close()

    def _age(self, user_id):
        """Move a member's join, and the owner's invites, a month back."""
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "UPDATE users SET created_at = created_at - interval "
                "'30 days' WHERE id = %s", (user_id,))
            admin.execute(
                "UPDATE invites SET used_at = used_at - interval '30 days' "
                "WHERE created_by = %s", (self.owner,))
            admin.commit()
        finally:
            admin.close()

    def _exists(self, user_id):
        return self.cc.execute("SELECT 1 FROM users WHERE id = %s",
                               (user_id,)).fetchone() is not None

    def test_a_member_from_the_owners_recent_invite_is_removed(self):
        planted = self._join(self.owner)
        viewer = self._join(self.owner, role="viewer")
        self.cc.execute(
            "INSERT INTO sessions (token_hash, user_id, tenant_id, "
            "expires_at) VALUES (%s, %s, %s, now() + interval '1 day')",
            (uuid.uuid4().hex, planted, self.tid))
        self._recover(self.owner)
        self.assertFalse(self._exists(planted))
        self.assertFalse(self._exists(viewer))
        self.assertTrue(self._exists(self.owner))
        self.assertIsNone(self.cc.execute(
            "SELECT 1 FROM sessions WHERE user_id = %s",
            (planted,)).fetchone())

    def test_an_emailed_reset_on_the_app_connection_removes_them(self):
        planted = self._join(self.owner)
        token = reset_mod.create_reset(self.cc, self.owner)
        row = reset_mod.lookup_reset(self.cc, token)
        self.assertTrue(reset_mod.consume(self.cc, row["id"], self.owner,
                                          "new-hash"))
        self.assertFalse(self._exists(planted))

    def _set_config(self, cfg):
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO tenant_settings (tenant_id, config) "
                "VALUES (%s, %s) ON CONFLICT (tenant_id) DO UPDATE "
                "SET config = EXCLUDED.config", (self.tid, json.dumps(cfg)))
            admin.commit()
        finally:
            admin.close()

    def _config(self):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT config FROM tenant_settings WHERE tenant_id = %s",
                (self.tid,)).fetchone()["config"]
        finally:
            admin.close()

    def _email(self, user_id):
        return self.cc.execute("SELECT email FROM users WHERE id = %s",
                               (user_id,)).fetchone()["email"]

    def test_the_owner_is_told_who_was_removed(self):
        planted = self._join(self.owner)
        email = self._email(planted)
        self._recover(self.owner)
        rec = outbound_pause.paused(self._config())
        self.assertIsNotNone(rec, "a removal alone must still raise the banner")
        self.assertEqual([m["email"] for m in rec["removed_members"]], [email])
        self.assertEqual(rec["removed_members"][0]["role"], "member")
        self.assertTrue(rec["removed_members"][0]["joined"])

    def test_resume_never_mails_a_removed_member(self):
        planted = self._join(self.owner)
        email = self._email(planted)
        friend = "friend@example.dev"
        self._set_config({"email_recipients": [friend, email],
                          "email_muted": [email]})
        self._recover(self.owner)
        cfg = self._config()
        self.assertEqual(cfg["email_recipients"], [friend])
        self.assertNotIn(email, cfg.get("email_muted") or [])
        rec = outbound_pause.paused(cfg)
        # the friend is still held for the owner to confirm; the removed
        # member is on no list a Resume would reopen
        self.assertEqual(rec["recipients"], [friend])

    def test_a_list_emptied_by_the_removal_falls_back_to_the_owner(self):
        planted = self._join(self.owner)
        self._set_config({"email_recipients": [self._email(planted)]})
        owner_email = self._email(self.owner)
        self._recover(self.owner)
        self.assertEqual(self._config()["email_recipients"], [owner_email])

    def test_a_member_who_joined_before_the_window_stays(self):
        older = self._join(self.owner)
        self._age(older)
        self._recover(self.owner)
        self.assertTrue(self._exists(older))

    def test_a_member_someone_else_invited_stays(self):
        inviter = self._join(self.owner)
        # the window would catch the inviter; age them out so only the
        # second-hand invitee is in question
        self._age(inviter)
        theirs = self._join(inviter)
        self._recover(self.owner)
        self.assertTrue(self._exists(theirs))

    def test_a_members_own_recovery_removes_nobody(self):
        member = self._join(self.owner)
        other = self._join(self.owner)
        self._recover(member)
        self.assertTrue(self._exists(member))
        self.assertTrue(self._exists(other))

    def test_a_zero_window_turns_the_removal_off(self):
        os.environ["OIKONOME_RECOVERY_MEMBER_WINDOW_DAYS"] = "0"
        planted = self._join(self.owner)
        self._recover(self.owner)
        self.assertTrue(self._exists(planted))


class TheSharedLoginNeverSeesWhoWasRemoved(unittest.TestCase):
    """The settings view's pause record names the members a recovery
    removed; the shared demonstration login is shown only its own row,
    like every other list of the household's people."""

    def test_removed_members_are_cut_to_the_callers_own(self):
        from oikonome.web import api
        view = {"outbound_paused": {
            "smtp": False, "llm": False, "recipients": [],
            "removed_members": [
                {"email": "member@example.test", "role": "member",
                 "joined": "2026-10-01"}]}}
        demo = {"email": "demo@example.test", "second_factor_waived": True}
        out = api._hide_people(json.loads(json.dumps(view)), demo)
        self.assertNotIn("member@example.test", json.dumps(out))
        owner = {"email": "owner@example.test", "role": "owner"}
        out = api._hide_people(json.loads(json.dumps(view)), owner)
        self.assertIn("member@example.test", json.dumps(out))


if __name__ == "__main__":
    unittest.main()
