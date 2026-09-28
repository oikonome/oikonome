"""Deleting a passkey is itself a step-up action — a session plus a password
is not enough.

Exempt passkey_delete from the passkey-only recovery-code step-up and a
session+password thief (no recovery code) can delete the owner's only
passkey → the account drops to zero strong factors →
_passkey_only_login_blocked goes False → password-only login is re-enabled →
durable takeover. So there is no exemption (delete needs a recovery code for
passkey-only accounts) AND the last strong factor cannot be removed while
forced-2FA is in effect.

The second invariant here: SimpleFIN's access token is a credentialed URL
(user:pass@host) and httpx exception text embeds it, so writing that text
verbatim into sync_log.error would defeat encryption at rest. URL userinfo
is redacted at the log sink.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import recovery
from oikonome.db import tenancy
from oikonome.sync.base import redact_url_credentials

from .util import _ensure_db


def _signup(email):
    admin = tenancy.admin_connect()
    try:
        tid = tenancy.create_tenant(admin, email)
        from oikonome.auth import passwords
        uid = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, verified_at) "
            "VALUES (%s, %s, %s, now()) RETURNING id",
            (tid, email, passwords.hash_password("correct-horse-battery"))
            ).fetchone()["id"]
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


class PasskeyDeleteTakeoverTests(unittest.TestCase):
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
        self.email = f"pkd-{uuid.uuid4().hex[:8]}@x.dev"
        self.uid = _signup(self.email)
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/login", data={
            "email": self.email, "password": "correct-horse-battery"},
            follow_redirects=False)
        assert r.status_code == 303, r.text

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def test_password_only_cannot_delete_last_passkey(self):
        """The takeover linchpin: a thief with the password but no recovery
        code must not be able to strip the sole passkey.

        Prefer last_factor (400) over recovery_required (401) when this is
        the only strong factor — both block the wipe, and last_factor does
        not burn a recovery code."""
        pk = _add_passkey(self.uid)
        r = self.client.request("DELETE", f"/api/passkeys/{pk}",
                                json={"password": "correct-horse-battery"})
        self.assertIn(r.status_code, (400, 401), r.text)
        body = r.text.lower()
        self.assertTrue("last_factor" in body or "recovery_required" in body,
                        r.text)
        # the key is still there
        self.assertEqual(
            len(self.client.get("/api/passkeys").json()["passkeys"]), 1)

    def test_last_factor_delete_refused_even_with_recovery(self):
        """Even a legit owner (valid recovery code) can't drop to zero strong
        factors while forced-2FA is on — must keep at least one."""
        admin = tenancy.admin_connect()
        try:
            codes = recovery.issue(admin, self.uid)
        finally:
            admin.close()
        pk = _add_passkey(self.uid)
        r = self.client.request("DELETE", f"/api/passkeys/{pk}", json={
            "password": "correct-horse-battery", "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("last_factor", r.text)

    def test_non_last_passkey_delete_needs_recovery(self):
        """With two passkeys, deleting one is allowed — but still needs a
        recovery code (passkey-only account); password alone is refused."""
        admin = tenancy.admin_connect()
        try:
            codes = recovery.issue(admin, self.uid)
        finally:
            admin.close()
        pk1, _pk2 = _add_passkey(self.uid), _add_passkey(self.uid)
        # password only → refused
        r = self.client.request("DELETE", f"/api/passkeys/{pk1}",
                                json={"password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("recovery_required", r.text)
        # with a recovery code → allowed (still one passkey left)
        r2 = self.client.request("DELETE", f"/api/passkeys/{pk1}", json={
            "password": "correct-horse-battery", "recovery_code": codes[0]})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(
            len(self.client.get("/api/passkeys").json()["passkeys"]), 1)

    def test_totp_account_cannot_delete_passkey_with_password_alone(self):
        """With TOTP enrolled, passkey-only recovery is a no-op — a
        live authenticator code is the extra factor, same as enroll."""
        from oikonome.auth import totp as totp_mod
        from .util import clear_totp_burn
        secret = totp_mod.new_secret()
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": totp_mod.code_now(secret),
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        pk1, _pk2 = _add_passkey(self.uid), _add_passkey(self.uid)
        r = self.client.request("DELETE", f"/api/passkeys/{pk1}",
                                json={"password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("totp_required", r.text)
        clear_totp_burn()
        r = self.client.request("DELETE", f"/api/passkeys/{pk1}", json={
            "password": "correct-horse-battery",
            "totp_code": totp_mod.code_now(secret)})
        self.assertEqual(r.status_code, 200, r.text)


class EmailChangeStepupTests(unittest.TestCase):
    """email_change is a posture change (the email is the login id +
    verdict-email/recovery channel) — a passkey-only account must present a
    recovery code, like password_change/account_delete."""

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
        self.email = f"emc-{uuid.uuid4().hex[:8]}@x.dev"
        self.uid = _signup(self.email)
        self.client = TestClient(self.appmod.app)
        r = self.client.post("/login", data={
            "email": self.email, "password": "correct-horse-battery"},
            follow_redirects=False)
        assert r.status_code == 303, r.text

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def test_passkey_only_email_change_needs_recovery(self):
        _add_passkey(self.uid)
        r = self.client.post("/api/email/change", data={
            "new_email": "attacker@evil.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("recovery_required", r.text)

    def test_passkey_only_email_change_with_recovery(self):
        admin = tenancy.admin_connect()
        try:
            codes = recovery.issue(admin, self.uid)
        finally:
            admin.close()
        _add_passkey(self.uid)
        r = self.client.post("/api/email/change", data={
            "new_email": f"newlegit-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery", "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 200, r.text)


class SimplefinRedactionTests(unittest.TestCase):
    def test_redacts_userinfo_credential(self):
        leaked = ("HTTPStatusError: Client error '403 Forbidden' for url "
                  "'https://theuser:sup3rsecret@bridge.simplefin.org/accounts'")
        out = redact_url_credentials(leaked)
        self.assertNotIn("sup3rsecret", out)
        self.assertNotIn("theuser", out)
        self.assertIn("https://***@bridge.simplefin.org", out)

    def test_passthrough_when_no_credential(self):
        self.assertEqual(redact_url_credentials("ConnectTimeout: timed out"),
                         "ConnectTimeout: timed out")
        self.assertIsNone(redact_url_credentials(None))

    def test_log_sync_redacts_at_the_sink(self):
        """The sink itself scrubs, so any syncer is covered."""
        _ensure_db()
        from .util import make_db
        conn = make_db()
        from oikonome.sync import base
        base.upsert_item(conn, "sf-item", "simplefin-org", "SimpleFIN", "tok")
        base.log_sync(conn, "sf-item", 0,
                      error="ConnectError for url "
                            "'https://u:leakedsecret@bridge.simplefin.org/x'")
        row = conn.execute(
            "SELECT error FROM sync_log WHERE item_id='sf-item'").fetchone()
        self.assertNotIn("leakedsecret", row["error"])
        self.assertIn("***@bridge.simplefin.org", row["error"])


if __name__ == "__main__":
    unittest.main()
