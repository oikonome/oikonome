"""The connections bundle is a second-factor door, like every other
durable-credential door.

Export decrypts EVERY tenant credential — provider keys, LLM and SMTP
secrets, live bank access tokens — and re-seals them under a passphrase the
CALLER chooses, so the encryption protects nothing against the requester.
Import is its twin: it plants credentials that keep syncing money data. A
stolen session plus a phished password must not be enough for either, or the
whole account's secrets leave behind the second factor's back — exactly the
reasoning that already puts a live TOTP code on the strictly lower-value
script-token and support-access doors.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"
PASSPHRASE = "open-sesame-open-sesame"


def _fresh_code(secret):
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)


class BundleSecondFactorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client = TestClient(appmod.app)
        cls.client.post("/api/signup", data={
            "email": f"bundle-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        # enroll 2FA the way the SPA does
        cls.secret = cls.client.post(
            "/api/totp/enroll", data={"password": PW}).json()["secret"]
        r = cls.client.post("/api/totp/confirm", data={
            "secret": cls.secret, "code": _fresh_code(cls.secret),
            "password": PW})
        assert r.status_code == 200, r.text

    def test_export_and_import_demand_a_live_code_from_a_totp_account(self):
        r = self.client.post("/api/connections/export",
                             json={"password": PW, "passphrase": PASSPHRASE})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("totp", r.text.lower())
        r = self.client.post(
            "/api/connections/import",
            data={"passphrase": PASSPHRASE, "password": PW},
            files={"file": ("c.oikx", b"{}", "application/octet-stream")})
        self.assertEqual(r.status_code, 401, r.text)

    def test_a_live_code_opens_the_door(self):
        """The gate must not lock the owner out of their own migration."""
        r = self.client.post("/api/connections/export",
                             json={"password": PW, "passphrase": PASSPHRASE,
                                   "totp_code": _fresh_code(self.secret)})
        self.assertEqual(r.status_code, 200, r.text)
        blob = r.content
        r = self.client.post(
            "/api/connections/import",
            data={"passphrase": PASSPHRASE, "password": PW,
                  "totp_code": _fresh_code(self.secret)},
            files={"file": ("c.oikx", blob, "application/octet-stream")})
        self.assertEqual(r.status_code, 200, r.text)


if __name__ == "__main__":
    unittest.main()
