"""A sign-in and a credential rotation are ordered by the factor lock.

A sign-in verifies its password (argon2, slow by design) with nothing
held, and only then writes its session and device-mint ticket. A password
change, reset or factor clear that commits in that gap revokes every
session and ticket it can see — which does not yet include the one about
to be written. So the sign-in writes under the same per-user lock and
re-checks there that the credential it verified is still the account's;
otherwise a hijacker's sign-in already in flight would come out of the
owner's recovery holding a fresh session and a ticket for a 90-day device
token.

Cancelling a sign-in at the phone's device picker spends its ticket and
closes its session; it takes the same lock before it touches either row,
or it would take them in the opposite order from every rotation.

A rotation that pauses the household's SMS number writes the settings row
before the users row, so it never waits on the settings row while holding
the users row a settings save's step-up needs.
"""

import threading
import time

from oikonome.auth import device_tokens, passwords
from oikonome.auth import reset as reset_mod
from oikonome.db import tenancy
from oikonome.web import security

from .test_credential_rotation_evicts_persistence import PW, _Base


class SignInAndRotationTakeOneOrder(_Base):
    def _count(self, sql):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(sql, (self.uid,)).fetchone()["n"]
        finally:
            admin.close()

    def _rotate(self, *, new_password=None):
        """What a reset (new password) or a factor clear (none) commits."""
        admin = tenancy.admin_connect()
        try:
            with admin.transaction():
                reset_mod.revoke_everything(admin, self.uid)
                if new_password:
                    admin.execute(
                        "UPDATE users SET password_hash=%s WHERE id=%s",
                        (passwords.hash_password(new_password), self.uid))
        finally:
            admin.close()

    def _login_with_rotation_in_the_gap(self, **rotation):
        """Sign in, with the rotation committing after the password has
        verified and before the session is written."""
        real = security.clear_challenge_risk
        fired = []

        def gap(*a, **kw):
            out = real(*a, **kw)
            if not fired:
                fired.append(1)
                self._rotate(**rotation)
            return out
        from unittest import mock
        from fastapi.testclient import TestClient
        with mock.patch.object(security, "clear_challenge_risk", gap):
            r = TestClient(self.app).post(
                "/api/login", data={"email": self.email, "password": PW})
        self.assertTrue(fired, "the gap was never reached")
        return r

    def test_a_sign_in_overtaken_by_a_reset_gets_no_session(self):
        r = self._login_with_rotation_in_the_gap(
            new_password="a-whole-new-passphrase-77")
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM sessions WHERE user_id=%s"), 0)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM webauthn_challenges "
            "WHERE purpose='device-mint-ticket' AND user_id=%s"), 0)

    def test_a_sign_in_with_nothing_rotated_still_works(self):
        c, ticket = self._login()
        self.assertTrue(ticket)
        self.assertEqual(c.get("/api/me").status_code, 200)

    def test_abandoning_a_sign_in_waits_for_the_factor_lock(self):
        _, ticket = self._login()
        out = {}
        hold = tenancy.admin_connect()
        try:
            with hold.transaction():
                reset_mod.lock_user_factors(hold, self.uid)
                t = threading.Thread(target=lambda: out.setdefault(
                    "r", self.client.post("/api/devices/abandon",
                                          json={"mint_ticket": ticket})))
                t.start()
                time.sleep(1.0)
                # still waiting on the lock, so it has touched no row yet
                self.assertTrue(t.is_alive(),
                                "abandon ran without the factor lock")
                self.assertIsNotNone(
                    device_tokens.mint_ticket_owner_hint(hold, ticket))
            t.join(30)
        finally:
            hold.close()
        self.assertEqual(out["r"].status_code, 200, out["r"].text)
        check = tenancy.admin_connect()
        try:
            self.assertIsNone(
                device_tokens.mint_ticket_owner_hint(check, ticket))
        finally:
            check.close()


class RotationWritesSettingsBeforeTheUsersRow(_Base):
    def _verified_number(self):
        admin = tenancy.admin_connect()
        try:
            tid = admin.execute("SELECT tenant_id FROM users WHERE id=%s",
                                (self.uid,)).fetchone()["tenant_id"]
            admin.execute(
                "UPDATE tenant_settings SET config = config || "
                "jsonb_build_object('notify_phone', jsonb_build_object("
                "'number', '+15550100', 'verified', true)) "
                "WHERE tenant_id=%s", (tid,))
            return tid
        finally:
            admin.close()

    def _users_row_free_while_settings_held(self, run):
        """Hold the household's settings row, start `run`, and report
        whether the user's row was free while `run` waited on it."""
        tid = self._verified_number()
        out = {}
        hold = tenancy.admin_connect()
        probe = tenancy.admin_connect()
        try:
            with hold.transaction():
                hold.execute("SELECT 1 FROM tenant_settings "
                             "WHERE tenant_id=%s FOR UPDATE", (tid,))
                t = threading.Thread(target=lambda: out.setdefault(
                    "r", run()))
                t.start()
                time.sleep(1.0)
                self.assertTrue(t.is_alive(),
                                "the rotation never waited on the settings")
                with probe.transaction():
                    free = probe.execute(
                        "SELECT 1 FROM users WHERE id=%s "
                        "FOR UPDATE SKIP LOCKED", (self.uid,)
                    ).fetchone() is not None
            t.join(30)
        finally:
            hold.close()
            probe.close()
        return free, out["r"]

    def test_password_change(self):
        free, r = self._users_row_free_while_settings_held(
            lambda: self.client.post("/api/password/change", json={
                "current_password": PW,
                "new_password": "another-long-passphrase-42"}))
        self.assertTrue(free, "the users row was held while the rotation "
                              "waited on the settings row")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["sms_number_needs_verify"])

    def test_password_reset(self):
        def reset():
            admin = tenancy.admin_connect()
            try:
                token = reset_mod.create_reset(admin, self.uid)
                row = reset_mod.lookup_reset(admin, token)
                return reset_mod.consume(
                    admin, row["id"], self.uid,
                    passwords.hash_password("another-long-passphrase-42"))
            finally:
                admin.close()
        free, ok = self._users_row_free_while_settings_held(reset)
        self.assertTrue(free, "the users row was held while the reset "
                              "waited on the settings row")
        self.assertTrue(ok)


if __name__ == "__main__":
    import unittest
    unittest.main()
