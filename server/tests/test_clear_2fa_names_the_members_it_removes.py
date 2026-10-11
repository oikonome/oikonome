"""The operator's clear-2fa names every login it deletes.

Clearing an owner's factors runs the same eviction as an emailed reset,
and that removes the members who joined through the owner's recent
invites (a stolen session could have planted them). The operator is the
one person who knowingly triggers it, so the confirmation lists those
members before anything happens (`clear-2fa --preview`, which changes
nothing), and the CLI's output and the admin audit row name the ones that
were removed. A removal nobody was told about is the failure this guards.

The confirmation can wait minutes for the operator, so the clear is handed
the list the prompt showed and refuses, changing nothing, when the set it
would remove is any other. And what is reported is what the eviction
deleted, never a before/after roster diff that would also count someone
who left (or miss someone who joined) while it ran.
"""

import contextlib
import io
import os
import unittest
import uuid
from unittest import mock

import psycopg
from psycopg.rows import dict_row

from oikonome import cli
from oikonome.auth import invites
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class ClearTwoFactorNamesRemovedMembersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self._env = os.environ.pop("OIKONOME_RECOVERY_MEMBER_WINDOW_DAYS", None)
        self.addCleanup(self._restore_env)
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(admin, f"c2fa-{uuid.uuid4().hex[:8]}")
            self.email = f"own-{uuid.uuid4().hex[:8]}@example.dev"
            self.owner = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "totp_secret) VALUES (%s, %s, 'x', 'owner', "
                "'JBSWY3DPEHPK3PXP') RETURNING id",
                (self.tid, self.email)).fetchone()["id"]
            admin.commit()
        finally:
            admin.close()
        self.cc = psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                                  autocommit=True)
        self.addCleanup(self.cc.close)
        self.member = f"m-{uuid.uuid4().hex[:8]}@example.dev"
        token = invites.create(self.cc, self.tid, self.owner, role="member",
                               label=self.member)["token"]
        self.member_id = invites.claim(self.cc, token, self.member, PW)["user_id"]

    def _restore_env(self):
        if self._env is not None:
            os.environ["OIKONOME_RECOVERY_MEMBER_WINDOW_DAYS"] = self._env

    def _exists(self, uid):
        return self.cc.execute("SELECT 1 FROM users WHERE id=%s",
                               (uid,)).fetchone() is not None

    def test_the_preview_lists_the_member_and_changes_nothing(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.clear_2fa_preview(self.email)
        self.assertIn(self.member, out.getvalue())
        self.assertTrue(self._exists(self.member_id))
        self.assertIsNotNone(self.cc.execute(
            "SELECT totp_secret FROM users WHERE id=%s",
            (self.owner,)).fetchone()["totp_secret"])

    def test_the_output_and_the_audit_row_name_the_removed_member(self):
        import oikonome.web.app as appmod
        err = io.StringIO()
        with mock.patch.object(appmod, "_deliver_factor_cleared_notice"), \
                contextlib.redirect_stderr(err):
            cli.clear_2fa(self.email)
        self.assertFalse(self._exists(self.member_id))
        self.assertIn(self.member, err.getvalue())
        admin = tenancy.admin_connect()
        try:
            row = admin.execute(
                "SELECT detail FROM admin_audit WHERE target=%s AND "
                "action='clear_second_factor' ORDER BY id DESC LIMIT 1",
                (self.email,)).fetchone()
        finally:
            admin.close()
        self.assertIn("members_removed=1", str(row["detail"]))
        self.assertIn(self.member, str(row["detail"]))

    def _preview(self) -> list[str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.clear_2fa_preview(self.email)
        return [ln.split()[0] for ln in out.getvalue().splitlines() if ln]

    def _totp(self):
        return self.cc.execute("SELECT totp_secret FROM users WHERE id=%s",
                               (self.owner,)).fetchone()["totp_secret"]

    def test_a_member_who_joins_after_the_preview_stops_the_clear(self):
        shown = self._preview()
        self.assertEqual(shown, [self.member])
        late = f"late-{uuid.uuid4().hex[:8]}@example.dev"
        token = invites.create(self.cc, self.tid, self.owner, role="member",
                               label=late)["token"]
        late_id = invites.claim(self.cc, token, late, PW)["user_id"]
        import oikonome.web.app as appmod
        err = io.StringIO()
        with mock.patch.object(appmod, "_deliver_factor_cleared_notice") as d, \
                contextlib.redirect_stderr(err), \
                self.assertRaises(SystemExit):
            cli.clear_2fa(self.email, expect_members=shown)
        d.assert_not_called()
        self.assertIn(late, err.getvalue())
        self.assertTrue(self._exists(self.member_id))
        self.assertTrue(self._exists(late_id))
        self.assertIsNotNone(self._totp(), "the refused clear changed factors")

    def test_the_confirmed_list_lets_the_clear_through(self):
        import oikonome.web.app as appmod
        with mock.patch.object(appmod, "_deliver_factor_cleared_notice"), \
                contextlib.redirect_stderr(io.StringIO()):
            cli.clear_2fa(self.email, expect_members=self._preview())
        self.assertFalse(self._exists(self.member_id))
        self.assertIsNone(self._totp())

    def test_only_rows_the_eviction_deleted_are_reported(self):
        # a member the recovery set out to remove, but whose row was
        # already gone by the time the delete ran (they left meanwhile):
        # the recovery did not remove them and must not say it did
        from oikonome.auth import factor_reset
        from oikonome.auth import reset as reset_mod
        real = reset_mod._recent_invitees

        def with_a_ghost(conn, owner_id):
            return real(conn, owner_id) + [{
                "id": uuid.uuid4(), "email": "ghost@example.dev",
                "role": "member", "tenant_id": self.tid,
                "joined": "2026-01-01T00:00:00"}]
        admin = tenancy.admin_connect()
        try:
            with mock.patch.object(reset_mod, "_recent_invitees",
                                   with_a_ghost):
                removed = factor_reset.clear_second_factor(admin, self.owner)
            admin.commit()
        finally:
            admin.close()
        self.assertEqual(removed["members"], [self.member])

    def test_the_console_door_names_the_removed_member_too(self):
        """The admin console's clear runs the same eviction, so its notice
        and its audit row must name the removed member just as the CLI
        does."""
        from fastapi.testclient import TestClient

        import oikonome.web.app as appmod
        from oikonome.web import security
        token = "test-admin-token-" + "y" * 32
        security._limiter._hits.clear()
        with mock.patch.dict(os.environ, {"OIKONOME_ADMIN_TOKEN": token}), \
                mock.patch.object(appmod, "_deliver_factor_cleared_notice"):
            console = TestClient(appmod.app)
            r = console.post("/admin/console/login", data={"token": token},
                             follow_redirects=False)
            self.assertEqual(r.status_code, 303)
            r = console.post("/admin/console/clear-2fa",
                             data={"user_id": str(self.owner),
                                   "confirm": self.email},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Second factor cleared", r.text)
        self.assertFalse(self._exists(self.member_id))
        self.assertIn(self.member, r.text)
        admin = tenancy.admin_connect()
        try:
            row = admin.execute(
                "SELECT detail FROM admin_audit WHERE target=%s AND "
                "action='clear_second_factor' ORDER BY id DESC LIMIT 1",
                (self.email,)).fetchone()
        finally:
            admin.close()
        self.assertIn("members_removed=1", str(row["detail"]))
        self.assertIn(self.member, str(row["detail"]))


class HostDoorHandsTheShownListBackTests(unittest.TestCase):
    """./oikonome.sh clear-2fa shows the preview, waits for the typed
    confirmation, and then runs the clear with exactly the addresses it
    showed, so a member who joined during the prompt stops the clear."""

    def test_the_clear_is_run_with_the_previewed_addresses(self):
        from . import lifecycle_stub as ls
        t = ls.tempdir(self)
        inst = ls.stage(t, "COMPOSE_PROJECT_NAME=acme\nOIKONOME_PORT=8999\n")
        binp = ls.make_bin(t)
        log = t / "log"
        r = ls.bash(
            f'bash "{inst}/oikonome.sh" clear-2fa own@example.dev '
            '<<< own@example.dev',
            env=ls.run_env(
                binp, log, STUB_APP_CONTAINER="acme-app-1",
                STUB_PREVIEW="a@example.dev (member, joined 2026-01-01)\\n"
                             "b@example.dev (viewer, joined 2026-01-02)"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("a@example.dev", r.stdout + r.stderr)
        clears = [ln for ln in log.read_text().splitlines()
                  if "clear-2fa" in ln and "--preview" not in ln]
        self.assertEqual(len(clears), 1, log.read_text())
        self.assertIn("--expect-members a@example.dev,b@example.dev "
                      "own@example.dev", clears[0])


if __name__ == "__main__":
    unittest.main()
