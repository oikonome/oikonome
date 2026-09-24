"""A hosted signup nobody ever confirms does not live forever.

Day 7: one reminder per unconfirmed person, with a fresh link, once. Day
30: the household is frozen under its own lockout status with a week's
grace, the owner is mailed the date and a fresh link, and the ordinary
purge finishes it when the week lapses. A click on any link confirms the
address; the button that click lands on is what reopens a frozen
household, in the transaction that takes its row lock. Households where
anyone EVER confirmed (an email change clears verified_at, not
first_verified_at), households with money attached, and every self-host
instance are never touched; the freeze itself waits for the notice.
"""

import datetime as dt
import os
import re
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome import ext
from oikonome.db import tenancy
from oikonome.jobs import unverified, worker
from oikonome.notify import delivery

from .util import _ensure_db

PW = "a-long-enough-password-1"
_RELEASED = {"plaid_items": 0, "released": "none", "mx_user": "none"}


def _admin():
    return tenancy.admin_connect()


def _age(email: str, days: int) -> None:
    admin = _admin()
    try:
        admin.execute(
            "UPDATE users SET created_at = now() - make_interval(days => %s) "
            "WHERE email=%s", (days, email))
    finally:
        admin.close()


def _row(email: str):
    admin = _admin()
    try:
        return admin.execute(
            """SELECT u.id, u.tenant_id, u.verified_at, u.first_verified_at,
                      u.verify_reminded_at, t.status, t.delete_after,
                      t.status_before_delete
                 FROM users u JOIN tenants t ON t.id = u.tenant_id
                WHERE u.email=%s""", (email,)).fetchone()
    finally:
        admin.close()


def _token_in(plain: str) -> str:
    return re.search(r"token=([A-Za-z0-9_-]+)", plain).group(1)


class UnconfirmedSignupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        self._env = mock.patch.dict(os.environ, {
            "OIKONOME_HOSTED": "1", "OIKONOME_OPEN_SIGNUP": "1",
            "OIKONOME_BASE_URL": "https://app.example.test",
            "OIKONOME_UNVERIFIED_REAP_DAYS": "30"})
        self._env.start()
        self._dev = mock.patch.object(self.appmod, "DEV_MODE", False)
        self._dev.start()
        self._vmail = mock.patch.object(self.appmod, "_deliver_verification")
        self._vmail.start()
        # every mail the sweep sends: (email, subject, plain)
        self.sent: list[tuple[str, str, str]] = []
        self._mail = mock.patch.object(
            unverified.account_mail, "send",
            side_effect=lambda email, subject, plain, html, **kw:
                self.sent.append((email, subject, plain)) or True)
        self._mail.start()
        from oikonome.web import security
        security._limiter._hits.clear()

    def tearDown(self):
        for p in (self._mail, self._vmail, self._dev, self._env):
            p.stop()

    def _mail_to(self, email: str) -> list[tuple[str, str, str]]:
        """This test's mail only: the sweep is global, and the shared test
        database holds other suites' aged households too."""
        return [m for m in self.sent if m[0] == email]

    def _signup(self, email=None):
        email = email or f"uc-{uuid.uuid4().hex[:8]}@x.dev"
        c = TestClient(self.appmod.app)
        r = c.post("/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return email, c

    # ---- the reminder -----------------------------------------------------------

    def test_day_seven_reminder_is_sent_once_with_a_fresh_link(self):
        email, _ = self._signup()
        _age(email, 8)
        out = unverified.sweep()
        mine = self._mail_to(email)
        self.assertEqual(len(mine), 1, mine)
        self.assertIn("/verify-email?token=", mine[0][2])
        self.assertNotIn(str(_row(email)["tenant_id"]), out["scheduled"])
        self.assertIsNotNone(_row(email)["verify_reminded_at"])
        self.assertEqual(_row(email)["status"], "active")
        unverified.sweep()
        self.assertEqual(len(self._mail_to(email)), 1)   # once, not nightly

    def test_a_fresh_signup_is_left_alone(self):
        email, _ = self._signup()
        _age(email, 3)
        out = unverified.sweep()
        self.assertEqual(self._mail_to(email), [])
        r = _row(email)
        self.assertNotIn(str(r["id"]), out["reminded"])
        self.assertNotIn(str(r["tenant_id"]), out["scheduled"])
        self.assertIsNone(r["verify_reminded_at"])

    # ---- the freeze ---------------------------------------------------------------

    def test_day_thirty_freezes_the_household_with_a_week_and_a_link(self):
        email, _ = self._signup()
        _age(email, 31)
        out = unverified.sweep()
        r = _row(email)
        self.assertIn(str(r["tenant_id"]), out["scheduled"])
        self.assertEqual(r["status"], unverified.STATUS)
        self.assertEqual(r["status_before_delete"], "active")
        left = r["delete_after"] - dt.datetime.now(dt.timezone.utc)
        self.assertAlmostEqual(left.total_seconds(), 7 * 86400, delta=120)
        subjects = [s for _, s, _ in self._mail_to(email)]
        self.assertTrue(any("will be deleted on" in s for s in subjects),
                        subjects)
        final = next(p for _, s, p in self._mail_to(email)
                     if "deleted on" in s)
        self.assertIn("/verify-email?token=", final)
        admin = _admin()
        try:
            audit = admin.execute(
                "SELECT detail FROM admin_audit WHERE "
                "action='tenant_delete_scheduled' AND target=%s",
                (str(r["tenant_id"]),)).fetchone()
        finally:
            admin.close()
        self.assertIsNotNone(audit)
        self.assertIn("reason=unverified", str(audit["detail"]))

    def test_a_household_that_ever_confirmed_is_never_a_candidate(self):
        """An email change clears verified_at on a household that once
        proved a mailbox; first_verified_at is the permanent record."""
        email, _ = self._signup()
        _age(email, 40)
        admin = _admin()
        try:
            admin.execute(
                "UPDATE users SET first_verified_at = now() - interval "
                "'20 days' WHERE email=%s", (email,))
        finally:
            admin.close()
        unverified.sweep()
        self.assertEqual(self._mail_to(email), [])
        self.assertEqual(_row(email)["status"], "active")

    def test_money_attached_ends_the_question(self):
        email, _ = self._signup()
        _age(email, 40)
        with mock.patch.object(ext.gate._resolve(), "money_attached",
                               return_value=True):
            unverified.sweep()
        self.assertEqual(self._mail_to(email), [])
        self.assertEqual(_row(email)["status"], "active")

    def test_no_freeze_without_the_notice(self):
        """The mail that says why and carries the way out goes first; a
        send that fails holds the freeze for another night."""
        email, _ = self._signup()
        _age(email, 40)
        self._mail.stop()
        with mock.patch.object(unverified.account_mail, "send",
                               return_value=False):
            out = unverified.sweep()
        self._mail.start()
        r = _row(email)
        self.assertEqual(r["status"], "active")
        self.assertIn(str(r["tenant_id"]), out["held"])

    def test_a_bounced_owner_is_frozen_without_a_send(self):
        email, _ = self._signup()
        _age(email, 40)
        delivery.record_send_failure(
            email, "550 5.1.1 recipient rejected: unknown user")
        try:
            unverified.sweep()
        finally:
            delivery.clear(email)
        self.assertEqual(self._mail_to(email), [])
        self.assertEqual(_row(email)["status"], unverified.STATUS)

    def test_self_host_and_zero_are_off(self):
        email, _ = self._signup()
        _age(email, 40)
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""}):
            self.assertEqual(unverified.sweep()["scheduled"], [])
        with mock.patch.dict(os.environ,
                             {"OIKONOME_UNVERIFIED_REAP_DAYS": "0"}):
            self.assertEqual(unverified.sweep()["scheduled"], [])
        self.assertEqual(self._mail_to(email), [])
        self.assertEqual(_row(email)["status"], "active")

    # ---- the way out ------------------------------------------------------------

    def test_the_frozen_session_sees_the_reason_and_may_resend(self):
        email, c = self._signup()
        _age(email, 31)
        unverified.sweep()
        r = c.get("/api/me")
        self.assertEqual(r.status_code, 403)
        self.assertIn("never confirmed", r.text)
        r = c.post("/api/verify-email/resend")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "verified": False})

    def test_the_click_confirms_and_the_button_on_it_reopens(self):
        """The click proves the mailbox; the button on the page it lands
        on is what brings the household back, because only a person
        presses buttons."""
        email, c = self._signup()
        _age(email, 31)
        unverified.sweep()
        token = _token_in(next(p for _, s, p in self._mail_to(email)
                               if "deleted on" in s))
        browser = TestClient(self.appmod.app)
        r = browser.get(f"/verify-email?token={token}",
                        follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Reopen this account", r.text)
        self.assertEqual(_row(email)["status"], unverified.STATUS)
        r = browser.post("/verify-email", data={"token": token},
                         follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Account reopened", r.text)
        row = _row(email)
        self.assertEqual(row["status"], "active")
        self.assertIsNone(row["delete_after"])
        self.assertIsNone(row["status_before_delete"])
        self.assertIsNotNone(row["verified_at"])
        self.assertIsNotNone(row["first_verified_at"])
        self.assertEqual(c.get("/api/me").status_code, 200)
        # and it stays a household: a later sweep finds nothing
        self.sent.clear()
        unverified.sweep()
        self.assertEqual(self._mail_to(email), [])
        self.assertEqual(_row(email)["status"], "active")

    def test_the_reminder_link_works_too(self):
        email, _ = self._signup()
        _age(email, 8)
        unverified.sweep()
        token = _token_in(self._mail_to(email)[0][2])
        r = TestClient(self.appmod.app).get(f"/verify-email?token={token}",
                                            follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(_row(email)["verified_at"])

    def test_the_console_restore_set_and_the_purge_set_carry_the_status(self):
        from oikonome.web import adminconsole
        self.assertIn(unverified.STATUS, adminconsole._frozen_statuses())
        self.assertIn(unverified.STATUS, self.appmod.LOCKOUT_STATUSES)

    # ---- the end --------------------------------------------------------------------

    def test_the_ordinary_purge_finishes_a_lapsed_freeze(self):
        email, _ = self._signup()
        _age(email, 31)
        unverified.sweep()
        r = _row(email)
        admin = _admin()
        try:
            admin.execute(
                "UPDATE tenants SET delete_after = now() - interval '1 minute' "
                "WHERE id=%s", (r["tenant_id"],))
        finally:
            admin.close()
        with mock.patch("oikonome.erasure.release_external",
                        return_value=_RELEASED), \
             mock.patch("oikonome.notify.account_mail.account_deleted",
                        return_value=True) as receipt:
            purged = worker.purge_scheduled_deletions()
        self.assertIn(str(r["tenant_id"]), purged)
        self.assertIsNone(_row(email))
        self.assertEqual([c.args[0] for c in receipt.call_args_list], [email])


if __name__ == "__main__":
    unittest.main()
