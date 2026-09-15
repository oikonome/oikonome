"""The no-JS sign-in form re-arms a spent emailed unlock for a passkey-only
account exactly as it does for a TOTP account.

Sign-in is two posts: password first, then the second factor. When the
human check was satisfied by an emailed unlock, the password post spends
it, so the form refunds the unlock whenever the password verified but the
flow could not finish for want of a code. The TOTP branch did; the
passkey-only branch (whose second post is a recovery code) 401'd and left
the unlock spent, so a locked-out passkey owner without JS reached the
recovery-code step only to be challenged again with nothing left to pass it.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class HtmlPasskeyOnlyRefundTests(unittest.TestCase):
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
        os.environ["OIKONOME_HOSTED"] = "1"
        self.email = f"pkhtml-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, self.email)
            from oikonome.auth import passwords
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (tid, self.email, passwords.hash_password(PW))
            ).fetchone()["id"]
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, %s, 0)",
                (uid, f"cred-{uuid.uuid4().hex}", "x"))
        finally:
            admin.close()

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def _post(self, **fields):
        return TestClient(self.app).post(
            "/login", data={"email": self.email, "password": PW, **fields},
            follow_redirects=False)

    def test_password_step_without_a_code_refunds_the_unlock(self):
        with mock.patch("oikonome.web.turnstile.refund_unlock") as refund:
            r = self._post()
        self.assertEqual(r.status_code, 401, r.text)
        refund.assert_called_once_with(self.email)

    def test_a_wrong_recovery_code_keeps_the_spend_spent(self):
        with mock.patch("oikonome.web.turnstile.refund_unlock") as refund:
            r = self._post(totp_code="ABCD-EFGH-IJKL-MNOP-QRST")
        self.assertEqual(r.status_code, 401, r.text)
        refund.assert_not_called()


if __name__ == "__main__":
    unittest.main()
