"""The way back in when password, authenticator AND recovery codes are gone.

The mailed reset strips every factor, so on an account with a strong factor
it must also take a recovery code — unconditionally, or "I control the
inbox" equals "I own the account". That rule stays. Two doors are added
beside it: a self-serve reset that lands only after a cooling-off period the
account is told about on every channel and can cancel with one click or any
sign-in, and an audited operator clear for the person who cannot wait.

Pinned here: the immediate reset still refuses without a code; the cooled
reset works only after it has landed and never after a cancel; a live
sign-in cancels it; the operator door evicts and audits; both sign-in
screens name the door and the support inbox.
"""

import os
import unittest
import uuid
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import factor_reset, recovery
from oikonome.auth import reset as reset_mod
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"
NEW_PW = "a-brand-new-passphrase"
TOKEN = "test-admin-token-" + "x" * 32


def _control():
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                           autocommit=True)


class _Fixture(unittest.TestCase):
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
        self.client = TestClient(self.appmod.app)
        self.conn = _control()
        self.addCleanup(self.conn.close)
        admin = tenancy.admin_connect()
        self.addCleanup(admin.close)
        self.admin = admin
        self.tid = tenancy.create_tenant(admin, f"lost-{uuid.uuid4().hex[:8]}")
        self.email = f"lost-{uuid.uuid4().hex[:6]}@example.dev"
        from oikonome.auth import passwords
        self.uid = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, totp_secret) "
            "VALUES (%s,%s,%s,'JBSWY3DPEHPK3PXP') RETURNING id",
            (self.tid, self.email, passwords.hash_password(PW))).fetchone()["id"]
        self.codes = recovery.issue(admin, self.uid)
        # the notices are out-of-band; capture what would have gone out
        self._mail = mock.patch.object(self.appmod, "_deliver_factor_reset_notice")
        self.mail = self._mail.start()
        self.addCleanup(self._mail.stop)

    def _link(self) -> str:
        return reset_mod.create_reset(self.conn, self.uid)

    def _reset(self, token, code=""):
        return self.client.post("/reset", data={
            "token": token, "password": NEW_PW, "recovery_code": code},
            follow_redirects=False)

    def _factors(self):
        return self.conn.execute(
            "SELECT (totp_secret IS NOT NULL) AS totp FROM users WHERE id=%s",
            (self.uid,)).fetchone()["totp"]

    def _land(self):
        self.admin.execute("UPDATE factor_resets SET lands_at = now() - "
                           "interval '1 second' WHERE user_id=%s", (self.uid,))


class CoolingOffResetTests(_Fixture):
    def test_the_immediate_reset_still_refuses_without_a_code(self):
        r = self._reset(self._link())
        self.assertEqual(r.status_code, 401)
        self.assertIn("recovery code", r.text)
        self.assertTrue(self._factors())

    def test_the_reset_page_names_the_door_and_the_support_inbox(self):
        r = self.client.get(f"/reset?token={self._link()}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Lost your recovery codes too?", r.text)
        self.assertIn('action="/recover"', r.text)
        self.assertNotIn("mailto:", r.text)   # no address configured → none named
        with mock.patch.dict(os.environ, {"OIKONOME_SUPPORT_EMAIL": "help@example.org"}):
            r = self.client.get(f"/reset?token={self._link()}")
        self.assertIn("mailto:help@example.org", r.text)

    def test_a_request_records_the_clock_and_tells_the_account(self):
        token = self._link()
        r = self.client.post("/recover", data={"token": token})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Recovery requested", r.text)
        row = factor_reset.pending(self.conn, self.uid)
        self.assertIsNotNone(row)
        # the reset link is left live: the password is not held hostage
        self.assertIsNotNone(reset_mod.lookup_reset(self.conn, token))
        self.mail.assert_called_once()
        email, tenant_id, lands_at, cancel = self.mail.call_args.args
        self.assertEqual(email, self.email)
        self.assertEqual(lands_at, row["lands_at"])
        self.assertTrue(cancel)
        # a request that has not landed changes nothing about the rule
        r = self._reset(self._link())
        self.assertEqual(r.status_code, 401)
        self.assertTrue(self._factors())

    def test_a_landed_request_lets_the_reset_through_and_clears_factors(self):
        self.client.post("/recover", data={"token": self._link()})
        self._land()
        page = self.client.get(f"/reset?token={self._link()}")
        self.assertNotIn('name="recovery_code"', page.text)
        r = self._reset(self._link())
        self.assertEqual(r.status_code, 303, r.text)
        self.assertFalse(self._factors())
        self.assertEqual(recovery.remaining(self.conn, self.uid), 0)
        self.assertIsNone(factor_reset.pending(self.conn, self.uid))
        spent = self.conn.execute(
            "SELECT consumed_at FROM factor_resets WHERE user_id=%s",
            (self.uid,)).fetchone()
        self.assertIsNotNone(spent["consumed_at"])
        # and the new password signs in
        r = self.client.post("/api/login",
                             data={"email": self.email, "password": NEW_PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_cancel_link_stops_it_for_good(self):
        self.client.post("/recover", data={"token": self._link()})
        cancel = self.mail.call_args.args[3]
        page = self.client.get(f"/recover/cancel?token={cancel}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Cancel the recovery", page.text)
        # the GET is side-effect-free: still pending after a scanner follows it
        self.assertIsNotNone(factor_reset.pending(self.conn, self.uid))
        r = self.client.post("/recover/cancel", data={"token": cancel})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Cancelled", r.text)
        self.assertIsNone(factor_reset.pending(self.conn, self.uid))
        # even once the date has passed, the cancelled request never lands
        self._land()
        r = self._reset(self._link())
        self.assertEqual(r.status_code, 401)
        self.assertTrue(self._factors())
        # the link works once — and "once" has to mean it against a clock
        # that starts AFTER it was clicked, or a link is not a one-shot
        # cancel at all but a standing veto on every future recovery
        r = self.client.post("/recover/cancel", data={"token": cancel})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/recover", data={"token": self._link()})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Recovery requested", r.text)
        self.assertIsNotNone(factor_reset.pending(self.conn, self.uid))
        r = self.client.post("/recover/cancel", data={"token": cancel})
        self.assertEqual(r.status_code, 400)
        self.assertIsNotNone(factor_reset.pending(self.conn, self.uid))
        # the link that new request mailed is the one that stops it
        fresh = self.mail.call_args.args[3]
        self.assertNotEqual(fresh, cancel)
        r = self.client.post("/recover/cancel", data={"token": fresh})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(factor_reset.pending(self.conn, self.uid))

    def test_any_live_sign_in_cancels_the_clock(self):
        self.client.post("/recover", data={"token": self._link()})
        from oikonome.auth import totp as totp_mod
        from .util import clear_totp_burn
        clear_totp_burn()
        r = self.client.post("/api/login", data={
            "email": self.email, "password": PW,
            "totp_code": totp_mod.code_now("JBSWY3DPEHPK3PXP")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(factor_reset.pending(self.conn, self.uid))
        row = self.conn.execute(
            "SELECT cancel_reason FROM factor_resets WHERE user_id=%s",
            (self.uid,)).fetchone()
        self.assertEqual(row["cancel_reason"], "sign-in")

    def test_asking_again_restarts_the_wait_rather_than_shortening_it(self):
        self.client.post("/recover", data={"token": self._link()})
        # six days in: a second ask must not leave the nearer clock running
        self.admin.execute("UPDATE factor_resets SET lands_at = now() + "
                           "interval '1 day' WHERE user_id=%s", (self.uid,))
        self.client.post("/recover", data={"token": self._link()})
        live = factor_reset.pending(self.conn, self.uid)
        self.assertIsNotNone(live)
        import datetime as dt
        self.assertGreater(live["lands_at"] - dt.datetime.now(dt.timezone.utc),
                           dt.timedelta(days=6, hours=23))
        n = self.conn.execute(
            "SELECT count(*) AS n FROM factor_resets WHERE user_id=%s "
            "AND cancel_reason='superseded'", (self.uid,)).fetchone()["n"]
        self.assertEqual(n, 1)

    def test_asking_after_it_landed_does_not_restart_the_clock(self):
        self.client.post("/recover", data={"token": self._link()})
        self._land()
        r = self.client.post("/recover", data={"token": self._link()})
        self.assertEqual(r.status_code, 200)
        self.assertIn("waiting period is over", r.text)
        self.assertIsNotNone(factor_reset.matured(self.conn, self.uid))

    def test_a_coded_reset_cancels_a_request_still_cooling(self):
        self.client.post("/recover", data={"token": self._link()})
        r = self._reset(self._link(), code=self.codes[0])
        self.assertEqual(r.status_code, 303, r.text)
        self.assertIsNone(factor_reset.pending(self.conn, self.uid))

    def test_an_account_without_factors_has_nothing_to_cool_off(self):
        self.admin.execute("UPDATE users SET totp_secret=NULL WHERE id=%s",
                           (self.uid,))
        r = self.client.post("/recover", data={"token": self._link()})
        self.assertEqual(r.status_code, 200)
        self.assertIn("no second factor to clear", r.text)
        self.assertIsNone(factor_reset.pending(self.conn, self.uid))
        self.mail.assert_not_called()

    def test_a_dead_reset_link_cannot_start_a_request(self):
        r = self.client.post("/recover", data={"token": "nope"})
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(factor_reset.pending(self.conn, self.uid))


class OperatorClearTests(_Fixture):
    def setUp(self):
        super().setUp()
        os.environ["OIKONOME_ADMIN_TOKEN"] = TOKEN
        self.addCleanup(os.environ.pop, "OIKONOME_ADMIN_TOKEN", None)
        self.console = TestClient(self.appmod.app)
        r = self.console.post("/admin/console/login", data={"token": TOKEN},
                              follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self._cleared = mock.patch.object(self.appmod,
                                          "_deliver_factor_cleared_notice")
        self.cleared = self._cleared.start()
        self.addCleanup(self._cleared.stop)
        # a live session and device, which the clear must evict
        from oikonome.auth import sessions
        sessions.create_session(self.conn, self.uid, self.tid, "test-ua")

    def _audit_last(self):
        return self.admin.execute(
            "SELECT action, target, detail FROM admin_audit "
            "WHERE action='clear_second_factor' ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def test_clear_evicts_audits_and_tells_the_account(self):
        self.client.post("/recover", data={"token": self._link()})
        r = self.console.post("/admin/console/clear-2fa",
                              data={"user_id": str(self.uid),
                                    "confirm": self.email},
                              follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Second factor cleared", r.text)
        self.assertFalse(self._factors())
        self.assertEqual(recovery.remaining(self.conn, self.uid), 0)
        sessions_left = self.conn.execute(
            "SELECT count(*) AS n FROM sessions WHERE user_id=%s",
            (self.uid,)).fetchone()["n"]
        self.assertEqual(sessions_left, 0)
        row = self._audit_last()
        self.assertEqual(row["target"], self.email)
        self.assertIn("totp=yes", str(row["detail"]))
        self.cleared.assert_called_once_with(self.email)
        # a pending cooling-off request is moot once the operator acted
        self.assertIsNone(factor_reset.pending(self.conn, self.uid))
        # the password was NOT touched: the reset is the road to a new one
        from oikonome.auth import passwords
        h = self.conn.execute("SELECT password_hash FROM users WHERE id=%s",
                              (self.uid,)).fetchone()["password_hash"]
        self.assertTrue(passwords.verify_password(h, PW))

    def test_clear_needs_the_typed_address(self):
        r = self.console.post("/admin/console/clear-2fa",
                              data={"user_id": str(self.uid),
                                    "confirm": "someone@else.dev"},
                              follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("NOT cleared", r.text)
        self.assertTrue(self._factors())
        self.cleared.assert_not_called()

    def test_clear_is_not_reachable_without_a_console_session(self):
        r = TestClient(self.appmod.app).post(
            "/admin/console/clear-2fa",
            data={"user_id": str(self.uid), "confirm": self.email},
            follow_redirects=False)
        self.assertIn(r.status_code, (303, 401, 403))
        self.assertTrue(self._factors())

    def test_the_tenant_page_offers_the_door_only_with_a_factor(self):
        r = self.console.get(f"/admin/console/tenant/{self.tid}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("clear second factor", r.text)
        self.admin.execute("UPDATE users SET totp_secret=NULL WHERE id=%s",
                           (self.uid,))
        r = self.console.get(f"/admin/console/tenant/{self.tid}")
        self.assertNotIn("clear second factor", r.text)

    def test_the_cli_clears_the_same_way(self):
        from oikonome import cli
        with mock.patch.object(self.appmod, "_deliver_factor_cleared_notice") \
                as notice:
            cli.clear_2fa(self.email)
        self.assertFalse(self._factors())
        self.assertEqual(recovery.remaining(self.conn, self.uid), 0)
        notice.assert_called_once_with(self.email)
        self.assertIsNotNone(self._audit_last())
        with self.assertRaises(SystemExit):
            cli.clear_2fa("nobody-" + self.email)


class SignInScreensNameTheDoorTests(unittest.TestCase):
    """The prompt lives where the wall is — on the recovery-code step of
    both sign-in screens — and says the same thing on each."""
    ROOT = Path(__file__).resolve().parents[2]

    def test_web_and_mobile_recovery_prompts_carry_the_same_door(self):
        web = (self.ROOT / "webapp/src/pages/Login.tsx").read_text()
        mob = (self.ROOT / "mobile/src/app/login.tsx").read_text()
        for src in (web, mob):
            self.assertIn("Lost your recovery codes too?", src)
            self.assertIn("7-day recovery", src)
            self.assertIn("/forgot", src)
            self.assertIn("support_email", src)

    def test_the_public_access_door_names_the_support_inbox_on_hosted(self):
        import oikonome.web.app as appmod
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                          "OIKONOME_SUPPORT_EMAIL": ""}):
            self.assertIsNone(appmod.access_info()["support_email"])
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                          "OIKONOME_SUPPORT_EMAIL": "help@example.org"}):
            self.assertEqual(appmod.access_info()["support_email"],
                             "help@example.org")
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""}):
            self.assertIsNone(appmod.access_info()["support_email"])


if __name__ == "__main__":
    unittest.main()
