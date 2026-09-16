"""The last-strong-factor guard on passkey delete must hold under
concurrency, not just in a single request read top to bottom.

Counting passkeys on one connection, running an argon2 step-up, then
deleting on another lets two deletes of two different keys be in flight
together: each sees "two passkeys", each passes, and the account drops to
zero strong factors — which is what re-enables password-only login for a
passkey-only account. The count and the delete share one transaction under
a per-user lock, so the second delete sees the first.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import recovery
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


def _signup(email):
    admin = tenancy.admin_connect()
    try:
        tid = tenancy.create_tenant(admin, email)
        from oikonome.auth import passwords
        uid = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, verified_at) "
            "VALUES (%s, %s, %s, now()) RETURNING id",
            (tid, email, passwords.hash_password(PW))).fetchone()["id"]
    finally:
        admin.close()
    return uid


def _add_passkey(uid):
    admin = tenancy.admin_connect()
    try:
        pk = admin.execute(
            "INSERT INTO passkeys (user_id, credential_id, public_key, "
            "sign_count) VALUES (%s, %s, %s, 0) RETURNING id",
            (uid, f"cred-{uuid.uuid4().hex}", "x")).fetchone()["id"]
    finally:
        admin.close()
    return str(pk)


def _delete_passkey_directly(pk_id):
    admin = tenancy.admin_connect()
    try:
        admin.execute("DELETE FROM passkeys WHERE id=%s::uuid", (pk_id,))
    finally:
        admin.close()


class LastPasskeyDeleteRaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        os.environ["OIKONOME_HOSTED"] = "1"
        self.email = f"pkrace-{uuid.uuid4().hex[:8]}@x.dev"
        self.uid = _signup(self.email)
        admin = tenancy.admin_connect()
        try:
            self.codes = recovery.issue(admin, self.uid)
        finally:
            admin.close()
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/login", data={"email": self.email,
                                             "password": PW},
                             follow_redirects=False)
        assert r.status_code == 303, r.text

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def _count(self):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT count(*) AS n FROM passkeys WHERE user_id=%s",
                (self.uid,)).fetchone()["n"]
        finally:
            admin.close()

    def test_a_concurrent_delete_cannot_strip_the_last_passkey(self):
        """Two keys; a rival delete lands while this request is inside its
        step-up (the gap between the pre-check and the delete). The
        request must then refuse — one key has to remain."""
        pk1, pk2 = _add_passkey(self.uid), _add_passkey(self.uid)
        real = self.appmod._step_up

        def rival_lands_during_step_up(*a, **k):
            _delete_passkey_directly(pk2)      # the other request won
            return real(*a, **k)

        with mock.patch.object(self.appmod, "_step_up",
                               side_effect=rival_lands_during_step_up):
            r = self.client.request("DELETE", f"/api/passkeys/{pk1}", json={
                "password": PW, "recovery_code": self.codes[0]})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("last_factor", r.text)
        self.assertEqual(self._count(), 1, "the account lost its last key")

    def test_deleting_one_of_two_still_works(self):
        pk1, _pk2 = _add_passkey(self.uid), _add_passkey(self.uid)
        r = self.client.request("DELETE", f"/api/passkeys/{pk1}", json={
            "password": PW, "recovery_code": self.codes[0]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._count(), 1)


if __name__ == "__main__":
    unittest.main()
