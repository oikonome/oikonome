"""A request holds at most one control-pool connection at a time, and none
across password hashing or mail.

The control pool is small and every request's session lookup draws from
it. A door that keeps one connection open while a helper checks out a
second (the step-up, the passkey check, the code check) needs two at once:
four such requests take all four and then wait on each other until the
checkout timeout, and for that time every other request on the instance is
shed with a 503. A door that keeps a connection across an argon2 hash or
an SMTP conversation holds it for the length of that work.

Each test runs a door against a control pool of ONE connection with a
short checkout timeout: a nested checkout cannot be served and fails, and
work that probes the pool (the stand-in hash, the stand-in mail) finds the
one connection free only if the door let go of it first.
"""

import contextlib
import os
from unittest import mock

from oikonome.db import tenancy

from .test_credential_rotation_evicts_persistence import (PW, _Base,
                                                          _fresh_code)


@contextlib.contextmanager
def _one_connection_pool():
    saved = dict(tenancy._control_pools)
    saved_timeout = tenancy.CONTROL_POOL_TIMEOUT
    tenancy._control_pools.clear()
    tenancy.CONTROL_POOL_TIMEOUT = 1.0
    with mock.patch.dict(os.environ, {"OIKONOME_CONTROL_POOL_MAX": "1"}):
        try:
            yield
        finally:
            for p in tenancy._control_pools.values():
                p.close()
            tenancy._control_pools.clear()
            tenancy._control_pools.update(saved)
            tenancy.CONTROL_POOL_TIMEOUT = saved_timeout


def _probe_pool():
    """Stand-in for slow work: it needs the pool's one connection free."""
    with tenancy.control_connect() as c:
        c.execute("SELECT 1")


class ARequestHoldsOneControlConnection(_Base):
    def test_sign_out_everywhere_else(self):
        with _one_connection_pool():
            r = self.client.post("/api/sessions/revoke", json={
                "all_others": True, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_revoke_another_session(self):
        other, _ = self._login()
        sid = [s["id"] for s in self.client.get("/api/sessions").json()[
            "sessions"] if not s["current"]][0]
        with _one_connection_pool():
            r = self.client.post("/api/sessions/revoke", json={
                "id": sid, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["revoked"], 1)

    def test_totp_disable(self):
        secret = self._enroll_totp()
        with _one_connection_pool():
            r = self.client.post("/api/totp/disable", data={
                "code": _fresh_code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_passkey_register(self):
        with _one_connection_pool():
            out = self._enroll_passkey()
        self.assertTrue(out)

    def test_password_reset_hashes_with_no_connection_held(self):
        from oikonome.auth import passwords
        real = passwords.hash_password

        def hashing(pw):
            _probe_pool()
            return real(pw)
        with _one_connection_pool(), \
                mock.patch.object(passwords, "hash_password", hashing):
            self._reset_password()

    def test_invite_mail_goes_out_with_no_connection_held(self):
        from oikonome.web import report
        sent = []

        def send(*a, **kw):
            _probe_pool()
            sent.append(a)
        with mock.patch.dict(os.environ, {
                "OIKONOME_BASE_URL": "https://oikonome.example"}), \
                mock.patch.object(report, "resolve_smtp",
                                  lambda conn: {"configured": True}), \
                mock.patch.object(report, "send", send), \
                _one_connection_pool():
            r = self.client.post("/api/invites", json={
                "label": "invitee@example.dev", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(sent)
