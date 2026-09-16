"""A malformed path id answers 404 before any step-up proof is consumed.

A passkey- or member-delete route that fed the raw path string to a
`%s::uuid` cast would raise a 500 inside Postgres — after `_step_up` had
already redeemed the caller's single-use recovery code or passkey ticket
against an operation that could never happen. The handlers parse the id
first: garbage is a plain 404, and the proof survives for
the request the caller actually meant to make.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import recovery
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class BadIdBeforeStepupTests(unittest.TestCase):
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
        self.email = f"bid-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(admin, self.email)
            from oikonome.auth import passwords
            self.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (self.tid, self.email,
                 passwords.hash_password(PW))).fetchone()["id"]
        finally:
            admin.close()
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/login", data={"email": self.email,
                                             "password": PW},
                             follow_redirects=False)
        assert r.status_code == 303, r.text

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def _add_passkey(self):
        admin = tenancy.admin_connect()
        try:
            pk = admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, %s, 0) RETURNING id",
                (self.uid, f"cred-{uuid.uuid4().hex}", "x")).fetchone()["id"]
        finally:
            admin.close()
        return str(pk)

    def _issue_codes(self):
        admin = tenancy.admin_connect()
        try:
            return recovery.issue(admin, self.uid)
        finally:
            admin.close()

    def test_passkey_delete_bad_id_is_404_and_spares_the_recovery_code(self):
        """Two passkeys so a real delete is legal; the garbage id must 404
        and the recovery code offered with it must still open the real
        delete afterwards."""
        pk1, _pk2 = self._add_passkey(), self._add_passkey()
        codes = self._issue_codes()
        r = self.client.request("DELETE", "/api/passkeys/not-a-uuid", json={
            "password": PW, "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 404, r.text)
        r2 = self.client.request("DELETE", f"/api/passkeys/{pk1}", json={
            "password": PW, "recovery_code": codes[0]})
        self.assertEqual(r2.status_code, 200, r2.text)

    def test_passkey_delete_unknown_uuid_still_404s(self):
        """A well-formed id that names nothing keeps its existing 404 —
        parsing first must not change the honest-miss answer."""
        self._add_passkey(); self._add_passkey()
        codes = self._issue_codes()
        r = self.client.request(
            "DELETE", f"/api/passkeys/{uuid.uuid4()}", json={
                "password": PW, "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 404, r.text)

    def test_member_remove_bad_id_is_404_and_spares_the_recovery_code(self):
        """Owner evicting a member: garbage id → 404 before step-up, and
        the same code then evicts the real member."""
        self._add_passkey(); self._add_passkey()   # passkey-only step-up
        codes = self._issue_codes()
        admin = tenancy.admin_connect()
        try:
            from oikonome.auth import passwords
            member = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s, %s, %s, 'viewer', now()) "
                "RETURNING id",
                (self.tid, f"m-{uuid.uuid4().hex[:8]}@x.dev",
                 passwords.hash_password(PW))).fetchone()["id"]
        finally:
            admin.close()
        r = self.client.request("DELETE", "/api/users/not-a-uuid", json={
            "password": PW, "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 404, r.text)
        r2 = self.client.request("DELETE", f"/api/users/{member}", json={
            "password": PW, "recovery_code": codes[0]})
        self.assertEqual(r2.status_code, 200, r2.text)

    def test_member_remove_of_yourself_spares_the_recovery_code(self):
        """Removing yourself is refused up front, like a malformed id —
        the 400 must come before the step-up so the recovery code offered
        with the doomed request still opens the eviction the owner
        actually meant."""
        self._add_passkey(); self._add_passkey()
        codes = self._issue_codes()
        admin = tenancy.admin_connect()
        try:
            from oikonome.auth import passwords
            member = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s, %s, %s, 'viewer', now()) "
                "RETURNING id",
                (self.tid, f"m-{uuid.uuid4().hex[:8]}@x.dev",
                 passwords.hash_password(PW))).fetchone()["id"]
        finally:
            admin.close()
        r = self.client.request("DELETE", f"/api/users/{self.uid}", json={
            "password": PW, "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 400, r.text)
        r2 = self.client.request("DELETE", f"/api/users/{member}", json={
            "password": PW, "recovery_code": codes[0]})
        self.assertEqual(r2.status_code, 200, r2.text)


if __name__ == "__main__":
    unittest.main()
