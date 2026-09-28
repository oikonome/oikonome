"""A credential rotation pauses the webhooks the rotated user created.

A webhook sends the household's rows to a URL with no session at all, so
it is persistence of the same kind as a script token: whoever held the
session when it was made keeps the feed after the session is gone. Every
rotation door — password change, password reset, email change, the
operator's factor clear, the first strong factor enrolled — revokes the
user's tokens, so each must also switch those webhooks off, or the one
foothold that leaves with the ledger survives the owner's recovery.
Paused rather than deleted: the owner's own receiver is usually among
them, and it goes back on with one switch. A webhook someone else in the
household made is not theirs to lose.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import passwords
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


def _hook(email: str, created_by: str | None = None) -> str:
    admin = tenancy.admin_connect()
    try:
        tid = admin.execute("SELECT tenant_id FROM users WHERE email=%s",
                            (email,)).fetchone()["tenant_id"]
        return str(admin.execute(
            "INSERT INTO webhooks (tenant_id, url, secret, events, "
            "created_by) VALUES (%s, %s, 'sealed', ARRAY['*'], %s) "
            "RETURNING id",
            (tid, f"https://hooks.example/{uuid.uuid4().hex}",
             created_by if created_by is not None else email.upper())
        ).fetchone()["id"])
    finally:
        admin.close()


def _enabled(hook_id: str) -> tuple[bool, str | None]:
    admin = tenancy.admin_connect()
    try:
        r = admin.execute("SELECT enabled, disabled_reason FROM webhooks "
                          "WHERE id=%s", (hook_id,)).fetchone()
        return r["enabled"], r["disabled_reason"]
    finally:
        admin.close()


class RotationPausesWebhooks(unittest.TestCase):
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

    def _signup(self):
        client = TestClient(self.app)
        email = f"hookrot-{uuid.uuid4().hex[:10]}@example.dev"
        r = client.post("/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return client, email

    def _assert_paused(self, hook_id):
        on, reason = _enabled(hook_id)
        self.assertFalse(on)
        self.assertTrue(reason)

    def test_password_change_pauses_the_users_webhooks(self):
        c, email = self._signup()
        mine = _hook(email)
        theirs = _hook(email, created_by="someone-else@example.dev")
        r = c.post("/api/password/change", json={
            "current_password": PW, "new_password": "another-long-phrase-9"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["webhooks_paused"], 1)
        self._assert_paused(mine)
        self.assertEqual(_enabled(theirs), (True, None))

    def test_email_change_pauses_webhooks_made_under_the_old_address(self):
        c, email = self._signup()
        mine = _hook(email)
        r = c.post("/api/email/change", data={
            "new_email": f"moved-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_paused(mine)

    def test_a_hook_made_before_an_email_change_is_paused_by_later_rotations(
            self):
        """The maker is the account, not the address it had that day. A
        hook made, paused by an email change and switched back on must
        still be paused by the next password change — matched by address,
        it would match nothing and keep posting the household's rows."""
        c, email = self._signup()
        r = c.post("/api/webhooks", json={
            "url": "http://127.0.0.1:9/hook", "events": ["test.ping"],
            "name": "made-before-the-move", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        hook_id = r.json()["id"]
        self.assertNotIn("created_by_user", r.json())
        r = c.post("/api/email/change", data={
            "new_email": f"moved-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_paused(hook_id)
        r = c.post(f"/api/webhooks/{hook_id}",
                   json={"enabled": True, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(_enabled(hook_id), (True, None))
        r = c.post("/api/password/change", json={
            "current_password": PW, "new_password": "another-long-phrase-9"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["webhooks_paused"], 1)
        self._assert_paused(hook_id)

    def test_switching_a_paused_hook_back_on_needs_elevation(self):
        """The pause exists to cut off whoever held a hijacked session.
        If that session could switch the hook straight back on — or had an
        enable request in flight while the rotation committed — the feed
        would survive the owner's recovery. So switching on is a step-up
        act, and a refused one leaves the pause and its reason in place."""
        c, email = self._signup()
        mine = _hook(email)
        r = c.post("/api/password/change", json={
            "current_password": PW, "new_password": "another-long-phrase-9"})
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_paused(mine)
        r = c.post(f"/api/webhooks/{mine}", json={"enabled": True})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"]["error"], "elevation_required")
        self._assert_paused(mine)
        r = c.post(f"/api/webhooks/{mine}",
                   json={"enabled": True,
                         "password": "another-long-phrase-9"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(_enabled(mine), (True, None))

    def test_switching_on_asks_whatever_the_stored_state(self):
        """A check on the stored state would be read before a concurrent
        rotation's pause commits; only asking on every switch-on closes
        that window. Switching off and renaming never ask."""
        c, email = self._signup()
        mine = _hook(email)
        r = c.post(f"/api/webhooks/{mine}", json={"enabled": True})
        self.assertEqual(r.status_code, 403, r.text)
        r = c.post(f"/api/webhooks/{mine}", json={"enabled": False})
        self.assertEqual(r.status_code, 200, r.text)
        r = c.post(f"/api/webhooks/{mine}", json={"name": "renamed"})
        self.assertEqual(r.status_code, 200, r.text)
        r = c.post(f"/api/webhooks/{mine}", json={"enabled": True})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(_enabled(mine)[0])

    def test_the_backfill_ties_existing_hooks_to_their_maker(self):
        """Hooks made before the id column existed get it from the address
        they were made under, when that still names a member of the same
        household; one whose maker's address has moved stays unowned and
        falls back to the address match."""
        from pathlib import Path
        _, email = self._signup()
        mine = _hook(email)
        stranger = _hook(email, created_by="nobody-here@example.dev")
        sql = (Path(__file__).resolve().parents[1] / "oikonome" / "db" /
               "migrations" / "143_webhooks_created_by_user.sql").read_text()
        admin = tenancy.admin_connect()
        try:
            admin.execute(sql)
            admin.commit()
            got = {str(r["id"]): r["created_by_user"] for r in admin.execute(
                "SELECT id, created_by_user FROM webhooks WHERE id IN "
                "(%s, %s)", (mine, stranger)).fetchall()}
            uid = admin.execute("SELECT id FROM users WHERE email=%s",
                                (email,)).fetchone()["id"]
        finally:
            admin.close()
        self.assertEqual(got[mine], uid)
        self.assertIsNone(got[stranger])

    def test_enrolling_the_first_factor_pauses_the_users_webhooks(self):
        from oikonome.auth import totp as totp_mod
        c, email = self._signup()
        mine = _hook(email)
        secret = c.post("/api/totp/enroll",
                        data={"password": PW}).json()["secret"]
        r = c.post("/api/totp/confirm", data={
            "secret": secret, "code": totp_mod.code_now(secret),
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_paused(mine)

    def test_password_reset_pauses_the_users_webhooks(self):
        from oikonome.auth import reset
        _, email = self._signup()
        mine = _hook(email)
        admin = tenancy.admin_connect()
        try:
            uid = admin.execute("SELECT id FROM users WHERE email=%s",
                                (email,)).fetchone()["id"]
            rid = admin.execute(
                "INSERT INTO password_resets (user_id, token_hash, "
                "expires_at) VALUES (%s, %s, now() + interval '1 hour') "
                "RETURNING id", (uid, uuid.uuid4().hex)).fetchone()["id"]
            admin.commit()
        finally:
            admin.close()
        conn = tenancy.control_connect()
        try:
            self.assertTrue(reset.consume(
                conn, rid, uid, passwords.hash_password("y" * 14)))
        finally:
            conn.close()
        self._assert_paused(mine)

    def test_operator_factor_clear_pauses_the_users_webhooks(self):
        from oikonome.auth import factor_reset
        _, email = self._signup()
        mine = _hook(email)
        conn = tenancy.control_connect()
        try:
            uid = conn.execute("SELECT id FROM users WHERE email=%s",
                               (email,)).fetchone()["id"]
            factor_reset.clear_second_factor(conn, uid)
        finally:
            conn.close()
        self._assert_paused(mine)

    def test_the_tenant_scope_does_not_outlive_the_rotation(self):
        """The pause scopes a pooled control connection to the tenant;
        that scope must end with the transaction, or the next borrower
        of the connection would read one household's rows."""
        from oikonome.notify import webhooks
        _, email = self._signup()
        _hook(email)
        conn = tenancy.control_connect()
        try:
            uid = conn.execute("SELECT id FROM users WHERE email=%s",
                               (email,)).fetchone()["id"]
            self.assertEqual(webhooks.pause_for_rotation(conn, uid), 1)
            t = conn.execute("SELECT current_setting('app.tenant_id', true) "
                             "AS t").fetchone()["t"]
            self.assertFalse(t)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
