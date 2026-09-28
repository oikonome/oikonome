"""Every address the app stores and later mails must be ONE mailbox.

report.send puts the recipient in the To header and smtplib derives the
SMTP envelope by parsing that header, so a stored login email of
"me@mine.example,other@elsewhere.example" is delivered to both — household
mail reaching a mailbox nobody verified. Checking only for an "@" let that
through. Each door that stores such an address refuses it, and the send
path refuses it whatever wrote the value."""

import os
import unittest
import uuid
from unittest import mock

from fastapi import HTTPException
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.web import mailguard, report

from .util import _ensure_db

PW = "correct-horse-battery"
PAIR = "me-{}@mine.example,other@elsewhere.example"


class ValidAddressShape(unittest.TestCase):
    def test_one_mailbox_passes(self):
        for a in ("me@mine.example", "a.b+tag@x.example", "o'neil@x.example",
                  "ops@lan"):
            self.assertTrue(mailguard.valid_address(a), a)

    def test_lists_and_display_forms_are_refused(self):
        for a in (PAIR.format(1), "a@x.example;b@y.example",
                  "Name <a@x.example>", "a b@x.example", "a@@x.example",
                  "@x.example", "a@", "a@x..example", "", None,
                  "a" * 250 + "@x.example"):
            self.assertFalse(mailguard.valid_address(a), a)


class SendRefusesAMultiMailboxRecipient(unittest.TestCase):
    def test_send_refuses_before_any_connection(self):
        smtp_cls = mock.MagicMock()
        with mock.patch("smtplib.SMTP", smtp_cls):
            with self.assertRaises(ValueError):
                report.send("s", "p", "<p>h</p>", [PAIR.format(2)],
                            smtp={"host": "relay.example.dev", "port": 587,
                                  "user": None, "password": None,
                                  "sender": "o@example.dev",
                                  "starttls": False, "configured": True},
                            bcc=False)
        smtp_cls.assert_not_called()

    def test_recipient_list_entry_is_refused_on_every_instance(self):
        with mock.patch.object(mailguard, "members_only", return_value=False):
            with self.assertRaises(HTTPException) as cm:
                mailguard.check_recipients(None, [PAIR.format(3)])
        self.assertEqual(cm.exception.status_code, 400)


class DoorsRefuseAMultiMailboxLogin(unittest.TestCase):
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
        store = security._shared_store()
        if store is not None:
            try:
                store._r.flushdb()
            except Exception:
                pass

    def _signup(self, c, email):
        data = {"email": email, "password": PW}
        if os.environ.get("OIKONOME_HOSTED"):
            from oikonome.auth import signup_invites
            admin = tenancy.admin_connect()
            try:
                data["invite"] = signup_invites.mint(admin, email)
            finally:
                admin.close()
        return c.post("/api/signup", data=data)

    def _stored(self, email):
        admin = tenancy.admin_connect()
        try:
            return admin.execute("SELECT 1 FROM users WHERE email=%s",
                                 (email,)).fetchone() is not None
        finally:
            admin.close()

    def test_email_change_refuses_a_pair(self):
        c = TestClient(self.app)
        mine = f"em-{uuid.uuid4().hex[:10]}@example.dev"
        self.assertEqual(self._signup(c, mine).status_code, 200)
        pair = PAIR.format(uuid.uuid4().hex[:8])
        r = c.post("/api/email/change",
                   data={"new_email": pair, "password": PW})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self._stored(pair))

    def test_signup_refuses_a_pair(self):
        pair = PAIR.format(uuid.uuid4().hex[:8])
        r = self._signup(TestClient(self.app), pair)
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self._stored(pair))

    def test_invite_claim_refuses_a_pair_before_burning(self):
        from oikonome.auth import invites
        owner = f"em-{uuid.uuid4().hex[:10]}@example.dev"
        self.assertEqual(self._signup(TestClient(self.app), owner).status_code,
                         200)
        pair = PAIR.format(uuid.uuid4().hex[:8])
        admin = tenancy.admin_connect()
        try:
            u = admin.execute("SELECT id, tenant_id FROM users WHERE email=%s",
                              (owner,)).fetchone()
            # an unbound share link: the claimer types the address
            with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""}):
                inv = invites.create(admin, u["tenant_id"], u["id"])
            with self.assertRaises(ValueError):
                invites.claim(admin, inv["token"], pair, PW,
                              mark_verified=True)
            # refused before the burn: the link still works for a real one
            self.assertIsNotNone(invites.peek(admin, inv["token"]))
        finally:
            admin.close()
        self.assertFalse(self._stored(pair))

    def test_setup_refuses_a_pair_before_any_user(self):
        from oikonome.web import setup
        with self.assertRaises(ValueError) as cm:
            setup.bootstrap(PAIR.format(uuid.uuid4().hex[:8]), PW)
        self.assertIn("valid email", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
