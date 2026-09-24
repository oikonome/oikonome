"""The button on the confirmed page, and the narrow thing it may do.

A household frozen for never confirming comes back only when somebody
presses "Reopen this account" — a POST carrying a link that mailbox was
sent. Holding such a link is the proof, so the door is exactly as wide
as the mailbox: it lifts the freeze on the token's OWN household, only
while that household is frozen, only once the address is verified, and
only while the token is inside its week. Any unexpired link counts,
including one a later resend superseded, because minting burns the older
link and that shape cannot be told from a link a scanner spent. An
expired or foreign token reopens nothing, an unproved address reopens
nothing, and a household nobody presses the button for is erased on the
date its notice named.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome import ext
from oikonome.auth import email_verify
from oikonome.db import tenancy
from oikonome.jobs import unverified

from .util import _ensure_db

PW = "correct-horse-battery"


def _admin():
    return tenancy.admin_connect()


def _user(email):
    admin = _admin()
    try:
        return admin.execute(
            "SELECT id, tenant_id, verified_at FROM users WHERE email=%s",
            (email,)).fetchone()
    finally:
        admin.close()


def _tenant(email):
    admin = _admin()
    try:
        return admin.execute(
            "SELECT t.id, t.status, t.delete_after, t.status_before_delete "
            "FROM tenants t JOIN users u ON u.tenant_id = t.id "
            "WHERE u.email=%s", (email,)).fetchone()
    finally:
        admin.close()


class ReopenButtonTests(unittest.TestCase):
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

    def _frozen_household(self):
        """A signup nobody confirmed, frozen with a week to go and a
        fresh link in its owner's inbox."""
        email = f"reopen-{uuid.uuid4().hex[:8]}@example.dev"
        r = TestClient(self.appmod.app).post(
            "/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        admin = _admin()
        try:
            uid = _user(email)["id"]
            admin.execute("UPDATE users SET verified_at=NULL, "
                          "first_verified_at=NULL WHERE id=%s", (uid,))
            admin.execute(
                "UPDATE tenants SET status=%s, status_before_delete='active', "
                "delete_after = now() + interval '7 days' WHERE id=%s",
                (unverified.STATUS, _user(email)["tenant_id"]))
            token = email_verify.create(admin, uid)
        finally:
            admin.close()
        return email, token

    def test_the_human_who_arrives_after_a_scanner_can_still_reopen(self):
        """The fetch spent the token; the person clicking afterwards sees
        the same page, with the button still on it."""
        email, token = self._frozen_household()
        scanner = TestClient(self.appmod.app)
        scanner.get(f"/verify-email?token={token}", follow_redirects=False)
        person = TestClient(self.appmod.app)
        page = person.get(f"/verify-email?token={token}",
                          follow_redirects=False)
        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn("Reopen this account", page.text)
        # the page waits for the press rather than forwarding out from
        # under it
        self.assertEqual(page.headers.get("refresh", ""), "")
        r = person.post("/verify-email", data={"token": token},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        row = _tenant(email)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["delete_after"])
        self.assertIsNone(row["status_before_delete"])

    def test_one_post_confirms_and_reopens_for_a_client_that_never_gets(self):
        """The no-JS twin: a POST carrying a link nothing has fetched
        yet does both halves at once."""
        email, token = self._frozen_household()
        r = TestClient(self.appmod.app).post(
            "/verify-email", data={"token": token}, follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(_user(email)["verified_at"])
        self.assertEqual(_tenant(email)["status"], "active")

    def test_a_link_for_one_household_reopens_no_other(self):
        mine_email, mine_token = self._frozen_household()
        other_email, _ = self._frozen_household()
        TestClient(self.appmod.app).post(
            "/verify-email", data={"token": mine_token},
            follow_redirects=False)
        self.assertEqual(_tenant(mine_email)["status"], "active")
        self.assertEqual(_tenant(other_email)["status"], unverified.STATUS)

    def test_an_expired_link_reopens_nothing(self):
        email, token = self._frozen_household()
        admin = _admin()
        try:
            admin.execute(
                "UPDATE email_verifications SET expires_at = now() - "
                "interval '1 minute' WHERE user_id=%s", (_user(email)["id"],))
        finally:
            admin.close()
        r = TestClient(self.appmod.app).post(
            "/verify-email", data={"token": token}, follow_redirects=False)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(_tenant(email)["status"], unverified.STATUS)

    def test_a_link_superseded_before_the_address_was_proved_reopens_nothing(
            self):
        """Spent-but-unexpired is not enough on its own: minting a link
        burns the older one, so that shape also belongs to a link nobody
        ever opened. The address is what has to be proved, and here it
        never was."""
        email, token = self._frozen_household()
        admin = _admin()
        try:
            email_verify.create(admin, _user(email)["id"])   # burns `token`
        finally:
            admin.close()
        r = TestClient(self.appmod.app).post(
            "/verify-email", data={"token": token}, follow_redirects=False)
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(_tenant(email)["status"], unverified.STATUS)

    def test_any_unexpired_link_reopens_once_the_address_is_proved(self):
        """A person who presses Resend twice and then opens the FIRST of
        the two mails is not turned away.

        Minting burns the older link, so by the time the second mail
        arrives the first reads as spent — the same shape a scanner's
        fetch leaves. The door is deliberately as wide as the mailbox and
        no wider: every one of these links was sent to that address, none
        outlives its week, and the address must already be verified
        before any of them lifts anything. Narrowing it to the newest
        link would strand whoever opens the mail that arrived first."""
        email, first = self._frozen_household()
        admin = _admin()
        try:
            uid = _user(email)["id"]
            # the address is proved (a scanner fetched an earlier link,
            # or the person confirmed and closed the page)
            admin.execute("UPDATE users SET verified_at=now(), "
                          "first_verified_at=now() WHERE id=%s", (uid,))
            email_verify.create(admin, uid)          # the second mail
        finally:
            admin.close()
        r = TestClient(self.appmod.app).post(
            "/verify-email", data={"token": first}, follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(_tenant(email)["status"], "active")

    def test_the_reopen_is_audited_and_tells_the_billing_gate(self):
        email, token = self._frozen_household()
        with mock.patch.object(ext.gate._resolve(),
                               "on_tenant_restored") as resumed:
            TestClient(self.appmod.app).post(
                "/verify-email", data={"token": token},
                follow_redirects=False)
        tid = str(_tenant(email)["id"])
        self.assertEqual([c.args[0] for c in resumed.call_args_list], [tid])
        admin = _admin()
        try:
            audit = admin.execute(
                "SELECT detail, ip FROM admin_audit WHERE "
                "action='tenant_delete_restored' AND target=%s",
                (tid,)).fetchone()
        finally:
            admin.close()
        self.assertIsNotNone(audit)
        self.assertIn("reason=email_confirmed", str(audit["detail"]))

    def test_an_ordinary_confirmation_is_untouched_by_the_button(self):
        """A household that was never frozen has nothing to reopen: the
        POST is the plain confirmed page, and no restore is audited."""
        email = f"plain-{uuid.uuid4().hex[:8]}@example.dev"
        TestClient(self.appmod.app).post(
            "/api/signup", data={"email": email, "password": PW})
        admin = _admin()
        try:
            uid = _user(email)["id"]
            admin.execute("UPDATE users SET verified_at=NULL WHERE id=%s",
                          (uid,))
            token = email_verify.create(admin, uid)
        finally:
            admin.close()
        r = TestClient(self.appmod.app).post(
            "/verify-email", data={"token": token}, follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("Reopen this account", r.text)
        self.assertIn("/login?verified=1", r.headers.get("refresh", ""))
        self.assertEqual(_tenant(email)["status"], "active")
        admin = _admin()
        try:
            n = admin.execute(
                "SELECT count(*) AS n FROM admin_audit WHERE "
                "action='tenant_delete_restored' AND target=%s",
                (str(_tenant(email)["id"]),)).fetchone()["n"]
        finally:
            admin.close()
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
