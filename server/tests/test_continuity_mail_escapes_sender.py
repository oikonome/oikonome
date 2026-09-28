"""The continuity-packet mail names its sender, and the sender's address is
account-supplied text. In the HTML part it must read as text: an address
stored with markup in it (from before addresses were checked for shape, or
a restore) must not render as a link or a fake sign-in prompt in the
recipient's mail client."""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, seed_accounts, write_config

PW = "correct-horse-battery"


class ContinuityMailEscapesSender(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.owner = TestClient(appmod.app)
        email = f"cm-{uuid.uuid4().hex[:8]}@example.dev"
        data = {"email": email, "password": PW}
        if os.environ.get("OIKONOME_HOSTED"):
            from oikonome.auth import signup_invites
            admin = tenancy.admin_connect()
            try:
                data["invite"] = signup_invites.mint(admin, email)
            finally:
                admin.close()
        assert cls.owner.post("/api/signup", data=data).status_code == 200
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            seed_accounts(conn)
            write_config(conn)
        finally:
            conn.close()
        cls.marked = (f'<a href="https://evil.example/{uuid.uuid4().hex[:6]}">'
                      'verify</a>@x.example')
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET email=%s WHERE email=%s",
                          (cls.marked, email))
        finally:
            admin.close()

    def test_sender_address_is_escaped_in_the_html_part(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        with mock.patch.dict(os.environ, {"OIKONOME_SMTP_HOST": "smtp.test",
                                          "OIKONOME_HOSTED": ""}), \
                mock.patch("oikonome.web.report.send") as send:
            r = self.owner.post("/api/continuity/email",
                                json={"to": "sam@example.dev",
                                      "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        html = send.call_args.args[2]
        self.assertNotIn("<a href", html)
        self.assertIn("&lt;a href=&quot;https://evil.example/", html)


if __name__ == "__main__":
    unittest.main()
