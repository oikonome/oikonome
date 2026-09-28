"""The continuity mail's recipient is account-supplied text. Its length
must be refused BEFORE its shape is tested: the shape regex backtracks
quadratically on a long run of dots, and one owner sending a 40k-character
"address" six times an hour would hold a request thread for minutes each on
a shared instance. A too-long recipient is a 400 in well under a second."""

import os
import time
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class ContinuityRecipientLengthBeforeShape(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.owner = TestClient(appmod.app)
        email = f"cl-{uuid.uuid4().hex[:8]}@example.dev"
        data = {"email": email, "password": PW}
        if os.environ.get("OIKONOME_HOSTED"):
            from oikonome.auth import signup_invites
            admin = tenancy.admin_connect()
            try:
                data["invite"] = signup_invites.mint(admin, email)
            finally:
                admin.close()
        assert cls.owner.post("/api/signup", data=data).status_code == 200

    def test_pathological_recipient_is_refused_fast(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        evil = "a@" + "." * 20000 + "@"
        with mock.patch.dict(os.environ, {"OIKONOME_SMTP_HOST": "smtp.test",
                                          "OIKONOME_HOSTED": ""}), \
                mock.patch("oikonome.web.report.send") as send:
            t0 = time.monotonic()
            r = self.owner.post("/api/continuity/email",
                                json={"to": evil, "password": PW})
            took = time.monotonic() - t0
        self.assertEqual(r.status_code, 400, r.text)
        self.assertLess(took, 1.0, f"recipient check took {took:.1f}s")
        send.assert_not_called()

    def test_ordinary_overlong_recipient_is_still_refused(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        with mock.patch.dict(os.environ, {"OIKONOME_SMTP_HOST": "smtp.test",
                                          "OIKONOME_HOSTED": ""}), \
                mock.patch("oikonome.web.report.send"):
            r = self.owner.post("/api/continuity/email",
                                json={"to": "x" * 250 + "@example.dev",
                                      "password": PW})
        self.assertEqual(r.status_code, 400, r.text)


if __name__ == "__main__":
    unittest.main()
