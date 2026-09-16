"""A mail scanner must not be able to STRAND a one-time link.

Corporate and consumer mail security (Defender Safe Links, Proofpoint,
Mimecast, Barracuda) fetches every URL in a message before the human sees it,
and chat unfurlers and browser prefetchers do the same. A token consumed
inside a GET handler is burned by the scan, and the Turnstile escape hatch —
whose whole audience is people behind corporate proxies, i.e. exactly the
mail that gets prescanned — would expire before they click. /unlock peeks on
GET and acts on POST, like /reset.

/verify-email is the deliberate exception — the click in the mail is the
whole task, with no confirm button. Its link IS spent on the
GET, because all it proves is that the mailbox receives our mail and a
scanner's prefetch proves the same thing; what must never recur is the human
then being told "already used" — a spent, unexpired link for a verified user
shows the same confirmed page.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import email_verify, login_unlock
from oikonome.db import tenancy

from .util import _ensure_db


def _user_row(email):
    admin = tenancy.admin_connect()
    try:
        return admin.execute("SELECT * FROM users WHERE email=%s",
                             (email,)).fetchone()
    finally:
        admin.close()


class LinkPrefetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def _account(self):
        client = TestClient(self.appmod.app)
        email = f"pf-{uuid.uuid4().hex[:8]}@example.dev"
        client.post("/api/signup",
                    data={"email": email, "password": "correct-horse-battery"})
        return email, client

    def test_verification_click_is_the_whole_task(self):
        email, client = self._account()
        admin = tenancy.admin_connect()
        try:
            token = email_verify.create(admin, _user_row(email)["id"])
        finally:
            admin.close()
        # the scanner fetches first — that spends the link and verifies
        # the mailbox (which is all the link ever proved)
        bare = TestClient(self.appmod.app)
        r = bare.get(f"/verify-email?token={token}", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Email confirmed", r.text)
        # and forwards itself to sign-in — no inline script under the CSP,
        # so the Refresh header carries it
        self.assertIn("/login?verified=1", r.headers.get("refresh", ""))
        with self.appmod._control_conn() as conn:
            self.assertIsNone(email_verify.lookup(conn, token))  # spent
        # the human, clicking afterwards, is NOT told "already used": the
        # same confirmed page, the same forward
        r = client.get(f"/verify-email?token={token}", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Email confirmed", r.text)
        self.assertIn("/login?verified=1", r.headers.get("refresh", ""))
        # the POST form behaves the same way
        r = client.post("/verify-email", data={"token": token},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Email confirmed", r.text)
        # sign-in acknowledges the forward
        self.assertIn("Email confirmed",
                      bare.get("/login?verified=1").text)

    def test_spent_verification_link_still_refuses_when_unverified(self):
        # a link burned by minting a NEWER one (resend) belongs to a user
        # who is not verified — that one must not read as confirmed
        email, client = self._account()
        admin = tenancy.admin_connect()
        try:
            uid = _user_row(email)["id"]
            admin.execute("UPDATE users SET verified_at=NULL WHERE id=%s",
                          (uid,))
            old = email_verify.create(admin, uid)
            email_verify.create(admin, uid)          # burns `old`
        finally:
            admin.close()
        r = TestClient(self.appmod.app).get(f"/verify-email?token={old}")
        self.assertEqual(r.status_code, 400)

    def test_scanner_get_does_not_burn_the_unlock_link(self):
        email, client = self._account()
        with self.appmod._control_conn() as conn:
            token = login_unlock.create(conn, _user_row(email)["id"])
        bare = TestClient(self.appmod.app)
        self.assertEqual(bare.get(f"/unlock?token={token}").status_code, 200)
        # not consumed, and the exemption window has NOT started
        with self.appmod._control_conn() as conn:
            self.assertIsNotNone(login_unlock.peek(conn, token))
        # the human's explicit action is what spends it
        r = client.post("/unlock/confirm", data={"token": token},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        with self.appmod._control_conn() as conn:
            self.assertIsNone(login_unlock.peek(conn, token))

    def test_spent_links_still_refuse(self):
        """A link spent by the POST stays spent on both methods."""
        email, client = self._account()
        with self.appmod._control_conn() as conn:
            token = login_unlock.create(conn, _user_row(email)["id"])
        client.post("/unlock/confirm", data={"token": token},
                    follow_redirects=False)
        self.assertEqual(
            client.post("/unlock/confirm", data={"token": token}).status_code,
            400)
        self.assertEqual(client.get(f"/unlock?token={token}").status_code, 400)


if __name__ == "__main__":
    unittest.main()
