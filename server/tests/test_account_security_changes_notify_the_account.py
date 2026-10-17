"""A change to how an account signs in is told to the account.

The login email is the identifier: a change made from a hijacked session
revokes everything the old login reached, so without a notice the owner is
simply signed out and their address names no account. The OLD address is
therefore mailed, with a single-use link that puts the address back, signs
every session and device out and starts a password reset there — and
touches no factor, because reading the old inbox must not amount to owning
the account. A password change and every factor change (authenticator,
passkeys, recovery codes) send a shorter notice, with no link, to the
account's own address.
"""

import os
import time
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


def _wait_for(seen, n=1):
    deadline = time.time() + 5
    while len(seen) < n and time.time() < deadline:
        time.sleep(0.02)
    return seen


class AccountSecurityChangesNotifyTheAccount(unittest.TestCase):
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
        store = security._shared_store()
        if store is not None:
            try:
                store._r.flushdb()
            except Exception:
                pass

    def _fresh(self):
        c = TestClient(self.appmod.app)
        email = f"sn-{uuid.uuid4().hex[:10]}@example.dev"
        r = c.post("/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return c, email

    def _change(self, c, new):
        """Change the login email, returning the revert notice's args."""
        seen = []
        with mock.patch.object(self.appmod, "_deliver_email_change_notice",
                               lambda *a: seen.append(a)):
            r = c.post("/api/email/change",
                       data={"new_email": new, "password": PW})
            self.assertEqual(r.status_code, 200, r.text)
            _wait_for(seen)
        self.assertEqual(len(seen), 1)
        return seen[0]

    def _user(self, email):
        admin = tenancy.admin_connect()
        try:
            return admin.execute("SELECT id, email FROM users WHERE email=%s",
                                 (email,)).fetchone()
        finally:
            admin.close()

    def _revert(self, token):
        return TestClient(self.appmod.app).post(
            "/account/email-revert", data={"token": token})

    def test_email_change_notifies_the_old_address(self):
        c, old = self._fresh()
        new = f"sn-new-{uuid.uuid4().hex[:8]}@example.dev"
        old_addr, new_addr, tid, token = self._change(c, new)
        self.assertEqual(old_addr, old)
        self.assertEqual(new_addr, new)
        self.assertEqual(str(tid), c.get("/api/me").json()["tenant_id"])
        # the token is stored hashed, never as the link carries it
        admin = tenancy.admin_connect()
        try:
            rows = admin.execute(
                "SELECT token_hash FROM email_change_reverts r JOIN users u "
                "ON u.id=r.user_id WHERE u.email=%s", (new,)).fetchall()
        finally:
            admin.close()
        self.assertEqual(len(rows), 1)
        self.assertNotEqual(rows[0]["token_hash"], token)

    def test_the_old_address_mail_masks_the_new_one_and_carries_the_link(
            self):
        sent = []
        with mock.patch.object(self.appmod, "_smtp_for",
                               return_value={"configured": True}), \
                mock.patch("oikonome.web.report.send",
                           lambda subj, plain, html, to, **kw:
                           sent.append((subj, plain, to))), \
                mock.patch.object(self.appmod, "_security_fan_out"), \
                mock.patch.dict(os.environ,
                                {"OIKONOME_BASE_URL": "https://oik.test"}):
            self.appmod._deliver_email_change_notice(
                "owner@example.dev", "attacker@evil.test", None, "TOK")
        self.assertEqual(len(sent), 1)
        _subj, plain, to = sent[0]
        self.assertEqual(to, ["owner@example.dev"])
        self.assertIn("a***@evil.test", plain)
        self.assertNotIn("attacker@", plain)
        self.assertIn("https://oik.test/account/email-revert?token=TOK",
                      plain)

    def test_revert_link_restores_the_old_login_email_and_revokes_sessions(
            self):
        c, old = self._fresh()
        new = f"sn-new-{uuid.uuid4().hex[:8]}@example.dev"
        token = self._change(c, new)[3]
        uid = self._user(new)["id"]
        # the GET is a confirm page only — a mail scanner changes nothing
        page = TestClient(self.appmod.app).get(
            f"/account/email-revert?token={token}")
        self.assertEqual(page.status_code, 200)
        self.assertIsNotNone(self._user(new))
        resets = []
        with mock.patch.object(self.appmod, "_deliver_reset",
                               lambda *a: resets.append(a)), \
                mock.patch.object(self.appmod,
                                  "_deliver_email_reverted_notice",
                                  lambda *a: None):
            r = self._revert(token)
            self.assertEqual(r.status_code, 200, r.text)
            _wait_for(resets)
        self.assertEqual(self._user(old)["id"], uid)
        self.assertIsNone(self._user(new))
        # the session that made the change is gone with every other one
        self.assertEqual(c.get("/api/me").status_code, 401)
        admin = tenancy.admin_connect()
        try:
            n = admin.execute("SELECT count(*) AS n FROM sessions "
                              "WHERE user_id=%s", (uid,)).fetchone()["n"]
            live = admin.execute(
                "SELECT count(*) AS n FROM password_resets WHERE user_id=%s "
                "AND used_at IS NULL", (uid,)).fetchone()["n"]
        finally:
            admin.close()
        self.assertEqual(n, 0)
        # and a reset is on its way to the restored address
        self.assertEqual(live, 1)
        self.assertEqual([a[0] for a in resets], [old])
        # the old login works again with the password it had
        self.assertEqual(TestClient(self.appmod.app).post(
            "/login", data={"email": old, "password": PW},
            follow_redirects=False).status_code, 303)

    def test_a_revert_link_works_once(self):
        c, old = self._fresh()
        token = self._change(
            c, f"sn-new-{uuid.uuid4().hex[:8]}@example.dev")[3]
        with mock.patch.object(self.appmod, "_deliver_reset",
                               lambda *a: None), \
                mock.patch.object(self.appmod,
                                  "_deliver_email_reverted_notice",
                                  lambda *a: None):
            self.assertEqual(self._revert(token).status_code, 200)
            self.assertEqual(self._revert(token).status_code, 400)
        self.assertEqual(self._revert("not-a-token").status_code, 400)

    def test_a_second_change_does_not_disarm_the_first_link(self):
        c, old = self._fresh()
        first = f"sn-a-{uuid.uuid4().hex[:8]}@example.dev"
        second = f"sn-b-{uuid.uuid4().hex[:8]}@example.dev"
        token = self._change(c, first)[3]
        # the session survives its own change; elevate again for a second
        self._change(c, second)
        with mock.patch.object(self.appmod, "_deliver_reset",
                               lambda *a: None), \
                mock.patch.object(self.appmod,
                                  "_deliver_email_reverted_notice",
                                  lambda *a: None):
            self.assertEqual(self._revert(token).status_code, 200)
        self.assertIsNotNone(self._user(old))
        self.assertIsNone(self._user(second))

    def test_a_revert_refuses_an_address_another_account_now_holds(self):
        c, old = self._fresh()
        token = self._change(
            c, f"sn-new-{uuid.uuid4().hex[:8]}@example.dev")[3]
        # someone signs up on the address the account left
        r = TestClient(self.appmod.app).post(
            "/api/signup", data={"email": old, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        other = self._user(old)["id"]
        self.assertEqual(self._revert(token).status_code, 409)
        self.assertEqual(self._user(old)["id"], other)

    def _quiet_revert(self, token):
        with mock.patch.object(self.appmod, "_deliver_reset",
                               lambda *a: None), \
                mock.patch.object(self.appmod,
                                  "_deliver_email_reverted_notice",
                                  lambda *a: None):
            return self._revert(token)

    def test_earliest_revert_link_wins_over_later_ones(self):
        """A hijacker who moves the address twice holds the second link
        (it went to the inbox they moved it to). Once the owner spends the
        first, nothing minted after it may undo that — or the hijacker
        moves the account back and voids the owner's reset with it."""
        c, old = self._fresh()
        first = f"sn-a-{uuid.uuid4().hex[:8]}@example.dev"
        second = f"sn-b-{uuid.uuid4().hex[:8]}@example.dev"
        owners = self._change(c, first)[3]
        hijackers = self._change(c, second)[3]
        self.assertEqual(self._quiet_revert(owners).status_code, 200)
        self.assertEqual(self._quiet_revert(hijackers).status_code, 400)
        self.assertIsNotNone(self._user(old))
        self.assertIsNone(self._user(first))

    def test_a_later_link_spent_first_leaves_the_earlier_one_live(self):
        """The rule is earliest-wins, not first-click-wins: the hijacker
        spending their own later link must not disarm the owner's."""
        c, old = self._fresh()
        first = f"sn-a-{uuid.uuid4().hex[:8]}@example.dev"
        second = f"sn-b-{uuid.uuid4().hex[:8]}@example.dev"
        owners = self._change(c, first)[3]
        hijackers = self._change(c, second)[3]
        self.assertEqual(self._quiet_revert(hijackers).status_code, 200)
        self.assertIsNotNone(self._user(first))
        self.assertEqual(self._quiet_revert(owners).status_code, 200)
        self.assertIsNotNone(self._user(old))

    def test_a_reminted_revert_link_keeps_its_place_before_later_ones(self):
        """Changing the address back and away again re-mints the owner's
        link. The fresh link must stay ahead of the hijacker's from the
        change in between (it went to the inbox they read), or spending
        the owner's link leaves theirs live and the account moves back."""
        c, owner_addr = self._fresh()
        theirs_addr = f"sn-b-{uuid.uuid4().hex[:8]}@example.dev"
        burnt = self._change(c, theirs_addr)[3]       # owner's first link
        hijackers = self._change(c, owner_addr)[3]    # mailed to their inbox
        owners = self._change(c, theirs_addr)[3]      # owner's, re-minted
        self.assertIsNotNone(hijackers)
        self.assertIsNotNone(owners)
        self.assertEqual(self._quiet_revert(burnt).status_code, 400)
        self.assertEqual(self._quiet_revert(owners).status_code, 200)
        self.assertIsNotNone(self._user(owner_addr))
        self.assertEqual(self._quiet_revert(hijackers).status_code, 400)
        self.assertIsNotNone(self._user(owner_addr))
        self.assertIsNone(self._user(theirs_addr))

    def test_unverified_old_address_gets_no_revert_link(self):
        """An address that never proved its mailbox (a typo at signup, a
        recycled inbox) must not be able to take the account back; it is
        still told the account left it."""
        c, old = self._fresh()
        uid = self._user(old)["id"]
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET verified_at=NULL WHERE id=%s",
                          (uid,))
            admin.commit()
        finally:
            admin.close()
        new = f"sn-new-{uuid.uuid4().hex[:8]}@example.dev"
        old_addr, new_addr, _tid, token = self._change(c, new)
        self.assertEqual((old_addr, new_addr), (old, new))
        self.assertIsNone(token)
        admin = tenancy.admin_connect()
        try:
            n = admin.execute(
                "SELECT count(*) AS n FROM email_change_reverts "
                "WHERE user_id=%s", (uid,)).fetchone()["n"]
        finally:
            admin.close()
        self.assertEqual(n, 0)

    def test_the_notice_without_a_link_carries_no_link(self):
        sent = []
        with mock.patch.object(self.appmod, "_smtp_for",
                               return_value={"configured": True}), \
                mock.patch("oikonome.web.report.send",
                           lambda subj, plain, html, to, **kw:
                           sent.append((plain, html, to))), \
                mock.patch.object(self.appmod, "_security_fan_out"), \
                mock.patch.dict(os.environ,
                                {"OIKONOME_BASE_URL": "https://oik.test"}):
            self.appmod._deliver_email_change_notice(
                "owner@example.dev", "attacker@evil.test", None, None)
        self.assertEqual(len(sent), 1)
        plain, html, to = sent[0]
        self.assertEqual(to, ["owner@example.dev"])
        self.assertIn("a***@evil.test", plain)
        self.assertNotIn("email-revert", plain + html)

    def test_old_address_is_told_even_without_a_public_url(self):
        """A LAN-only instance has no public address to build the revert
        link from. The old inbox must still hear that the account left
        it — silence there is what a hijacker wants — while the link
        itself goes only to the log, for whoever runs the instance."""
        sent, logged = [], []
        with mock.patch.object(self.appmod, "_smtp_for",
                               return_value={"configured": True}), \
                mock.patch("oikonome.web.report.send",
                           lambda subj, plain, html, to, **kw:
                           sent.append((plain, html, to))), \
                mock.patch.object(self.appmod, "_log_link",
                                  lambda *a: logged.append(a)), \
                mock.patch.object(self.appmod, "_security_fan_out"), \
                mock.patch.dict(os.environ, {"OIKONOME_BASE_URL": ""}):
            self.appmod._deliver_email_change_notice(
                "owner@example.dev", "attacker@evil.test", None, "TOK")
        self.assertEqual(len(sent), 1)
        plain, html, to = sent[0]
        self.assertEqual(to, ["owner@example.dev"])
        self.assertIn("a***@evil.test", plain)
        self.assertNotIn("TOK", plain + html)
        self.assertIn("Ask them for the link", plain)
        self.assertEqual(len(logged), 1)
        self.assertIn("token=TOK", " ".join(map(str, logged[0])))

    def _notice_through_a_relay(self, fake_smtp):
        """Run the email-change notice through the real send path against
        `fake_smtp`, returning what it delivered to the old address."""
        smtp = {"configured": True, "host": "smtp.example.dev", "port": 587,
                "user": None, "password": None,
                "sender": "noreply@example.dev", "starttls": False}
        with mock.patch.object(self.appmod, "_smtp_for", return_value=smtp), \
                mock.patch("smtplib.SMTP", fake_smtp), \
                mock.patch("oikonome.web.netguard.pinned_smtp_host",
                           lambda h: h), \
                mock.patch.object(self.appmod, "_log_link", lambda *a: None), \
                mock.patch.object(self.appmod, "_security_fan_out"), \
                mock.patch.dict(os.environ,
                                {"OIKONOME_BASE_URL": "https://oik.test"}):
            self.appmod._deliver_email_change_notice(
                "owner@example.dev", "attacker@evil.test", None, "TOK")
        return fake_smtp.delivered

    def test_old_address_gets_one_mail_when_quit_fails_after_delivery(self):
        """The relay accepted the link mail, then answered QUIT badly
        (smtplib raises that while closing the session). The mail went
        out: the old inbox must get exactly that one, not a second,
        link-less notice saying the link could not be sent."""
        import smtplib

        class QuitFails:
            delivered = []

            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                raise smtplib.SMTPResponseException(451, b"closing")

            def starttls(self, **k):
                pass

            def login(self, *a):
                pass

            def send_message(self, msg):
                QuitFails.delivered.append(msg.as_string())

        delivered = self._notice_through_a_relay(QuitFails)
        self.assertEqual(len(delivered), 1)
        self.assertIn("email-revert", delivered[0])

    def test_a_refused_link_mail_still_leaves_the_plain_notice(self):
        """A relay that refuses the link mail outright delivered nothing,
        so the old inbox is still told, by the plain notice."""
        import smtplib

        class RefusesTheLink:
            delivered = []

            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self, **k):
                pass

            def login(self, *a):
                pass

            def send_message(self, msg):
                body = msg.as_string()
                if "email-revert" in body:
                    raise smtplib.SMTPDataError(554, b"rejected")
                RefusesTheLink.delivered.append(body)

        delivered = self._notice_through_a_relay(RefusesTheLink)
        self.assertEqual(len(delivered), 1)
        self.assertNotIn("email-revert", delivered[0])

    def test_a_reset_from_the_new_address_does_not_disarm_the_revert_link(
            self):
        """Whoever moved the account can reset its password from the
        mailbox they moved it to. If completing a reset (or changing the
        password) burnt the revert links, that reset would be the
        hijacker's way to disarm the owner's only road back."""
        from oikonome.auth import reset as reset_mod
        c, old = self._fresh()
        new = f"sn-new-{uuid.uuid4().hex[:8]}@example.dev"
        token = self._change(c, new)[3]
        uid = self._user(new)["id"]
        admin = tenancy.admin_connect()
        try:
            rtok = reset_mod.create_reset(admin, uid)
            admin.commit()
            row = reset_mod.lookup_reset(admin, rtok)
            admin.commit()
        finally:
            admin.close()
        with tenancy.control_connect() as conn:
            self.assertTrue(reset_mod.consume(conn, row["id"], uid, "x"))
        self.assertEqual(self._quiet_revert(token).status_code, 200)
        self.assertEqual(self._user(old)["id"], uid)

    def test_password_change_notifies_the_account(self):
        c, email = self._fresh()
        seen = []
        with mock.patch.object(self.appmod, "_deliver_security_notice",
                               lambda *a: seen.append(a)):
            r = c.post("/api/password/change",
                       json={"current_password": PW,
                             "new_password": "another-long-passphrase-9"})
            self.assertEqual(r.status_code, 200, r.text)
            _wait_for(seen)
        self.assertEqual([(a[0], a[2]) for a in seen], [(email, "password")])

    def test_a_security_notice_is_mailed_to_the_account_without_a_link(self):
        sent = []
        with mock.patch.object(self.appmod, "_smtp_for",
                               return_value={"configured": True}), \
                mock.patch("oikonome.web.report.send",
                           lambda subj, plain, html, to, **kw:
                           sent.append((plain, to))), \
                mock.patch.object(self.appmod, "_security_fan_out"):
            for change in ("password", "totp_enrolled", "totp_replaced",
                           "totp_disabled", "passkey_added",
                           "passkey_removed", "recovery_codes"):
                self.appmod._deliver_security_notice(
                    "owner@example.dev", None, change)
        self.assertEqual(len(sent), 7)
        for plain, to in sent:
            self.assertEqual(to, ["owner@example.dev"])
            self.assertNotIn("http", plain)


if __name__ == "__main__":
    unittest.main()
