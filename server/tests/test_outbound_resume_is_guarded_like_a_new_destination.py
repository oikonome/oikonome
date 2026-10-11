"""Lifting an outbound pause re-opens the destinations a rotation paused,
so it is guarded exactly like pointing them somewhere new — and what the
pause holds is reported truthfully while it stands.

Invariants:
* Resume needs an elevated session (or the legacy proof in the body); a
  bare cookie is refused and the pause stands.
* The shared demonstration login may not resume, and is not shown the
  other members' addresses the pause names.
* A resume that authenticated before a reset evicted its session does not
  lift the pause the reset wrote.
* "Send test" during a pause says the relay / a recipient is paused,
  rather than "no SMTP configured" or "not accepted their invitation".
* A password change waits for the settings row holding no `users` row
  lock (the canonical lock order), so a settings save's step-up that burns
  a TOTP counter cannot be stuck behind it.
"""

import os
import threading
import time
import unittest
import uuid
from unittest import mock

import psycopg
from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.notify import outbound_pause

from .util import _ensure_db, accept_recipient_invite, write_config

PW = "correct-horse-battery"
PW2 = "another-long-phrase-9"


class _Household(unittest.TestCase):
    """A self-host household whose owner set a Settings relay, an extra
    recipient and an AI endpoint. No tests of its own."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.app)
        self.email = f"outr-{uuid.uuid4().hex[:10]}@example.dev"
        r = self.client.post("/api/signup", data={"email": self.email,
                                                 "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            self.uid = admin.execute(
                "SELECT id FROM users WHERE lower(email)=lower(%s)",
                (self.email,)).fetchone()["id"]
        finally:
            admin.close()
        self.partner = f"partner-{uuid.uuid4().hex[:6]}@example.dev"
        conn = tenancy.tenant_connect(self.tid)
        try:
            write_config(conn, smtp_host="relay.example.dev", smtp_port=2525,
                         smtp_starttls=False,
                         email_recipients=[self.email, self.partner],
                         llm_url="http://10.0.0.9:11434", llm_model="x")
        finally:
            conn.close()
        accept_recipient_invite(self.tid, self.partner)

    # ---- helpers ----------------------------------------------------------

    def _admin(self, sql, args=()):
        admin = tenancy.admin_connect()
        try:
            admin.execute(sql, args)
        finally:
            admin.close()

    def _rotate(self):
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": PW2})
        self.assertEqual(r.status_code, 200, r.text)

    def _unelevate(self):
        self._admin("UPDATE sessions SET elevated_at = NULL "
                    "WHERE user_id = %s", (self.uid,))

    def _elevate(self):
        r = self.client.post("/api/auth/elevate", json={"password": PW2})
        self.assertEqual(r.status_code, 200, r.text)

    def _paused(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            return outbound_pause.paused(budget.load_config(conn))
        finally:
            conn.close()



class OutboundResumeGuardTests(_Household):
    # ---- resume is a step-up act -------------------------------------------

    def test_resume_with_only_a_cookie_is_refused_and_the_pause_stands(self):
        self._rotate()
        self._unelevate()
        r = self.client.post("/api/settings/outbound/resume")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"]["error"], "elevation_required")
        self.assertIsNotNone(self._paused())

    def test_resume_inside_the_elevation_window_lifts_the_pause(self):
        self._rotate()
        self._unelevate()
        self._elevate()
        r = self.client.post("/api/settings/outbound/resume")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["resumed"])
        self.assertIsNone(self._paused())

    def test_resume_with_the_password_in_the_body_still_works(self):
        self._rotate()
        self._unelevate()
        r = self.client.post("/api/settings/outbound/resume",
                             json={"password": PW2})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self._paused())

    def test_the_shared_login_cannot_resume(self):
        self._rotate()
        self._elevate()
        self._admin("UPDATE users SET second_factor_waived = true "
                    "WHERE id = %s", (self.uid,))
        r = self.client.post("/api/settings/outbound/resume")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIsNotNone(self._paused())

    def test_a_resume_from_a_session_a_reset_evicted_does_not_lift_it(self):
        """The reset commits while the resume is between its checks and its
        write: the resume must find itself signed out, not lift the pause
        the reset just wrote."""
        self._rotate()
        self._elevate()
        real = self.appmod._require_elevation
        uid = self.uid

        def elevation_then_reset(user, **kw):
            real(user, **kw)
            from oikonome.auth import reset as reset_mod
            admin = tenancy.admin_connect()
            try:
                with admin.transaction():
                    reset_mod.revoke_everything(admin, uid)
            finally:
                admin.close()

        with mock.patch.object(self.appmod, "_require_elevation",
                               elevation_then_reset):
            r = self.client.post("/api/settings/outbound/resume")
        self.assertEqual(r.status_code, 401, r.text)
        p = self._paused()
        self.assertIsNotNone(p)
        self.assertEqual(p["reason"], "recovery")

    # ---- what the shared login is shown -----------------------------------

    def test_the_shared_login_is_not_shown_other_members_paused_addresses(self):
        self._rotate()
        self._admin("UPDATE users SET second_factor_waived = true "
                    "WHERE id = %s", (self.uid,))
        got = self.client.get("/api/settings").json()["outbound_paused"]
        self.assertIsNotNone(got, "the relay and AI pause are still shown")
        self.assertNotIn(self.partner, got["recipients"])
        self.assertTrue(got["smtp"] and got["llm"])

    def test_the_owner_still_sees_every_paused_address(self):
        self._rotate()
        got = self.client.get("/api/settings").json()["outbound_paused"]
        self.assertEqual(got["recipients"], [self.partner])

    # ---- Send test during a pause ------------------------------------------

    def test_send_test_says_the_relay_is_paused(self):
        self._rotate()
        with mock.patch.dict(os.environ, {"OIKONOME_SMTP_HOST": "",
                                          "OIKONOME_HOSTED": ""}):
            r = self.client.post("/api/settings/smtp-test")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("paused", r.json()["detail"])
        self.assertNotIn("no SMTP configured", r.json()["detail"])

    def test_send_test_runs_once_the_paused_relay_is_removed(self):
        """A pause names the relay that was set, and the senders hold it
        back only while one is set: with the relay removed, mail goes
        through the instance's own relay, so the test must try that relay
        rather than refuse as though the removed one were still held."""
        self._rotate()
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
            cfg.pop("smtp_host", None)
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        self.assertTrue(self._paused()["smtp"])
        sent = lambda subj, text, html, rcpts, smtp=None: [   # noqa: E731
            {"email": e, "ok": True, "error": None} for e in rcpts]
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "",
                                          "OIKONOME_SMTP_HOST":
                                              "instance-relay.example.dev"}), \
                mock.patch("oikonome.web.report.send_each", sent):
            r = self.client.post("/api/settings/smtp-test")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn(self.email.lower(),
                      [str(a).lower() for a in r.json()["sent_to"]])

    def test_send_test_names_a_paused_recipient_as_paused(self):
        self._rotate()
        # the relay confirmed, the extra recipient still held
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
            cfg["outbound_paused"] = {**cfg["outbound_paused"],
                                      "smtp": False, "llm": False}
            budget.save_config(conn, cfg)
        finally:
            conn.close()
        sent = lambda subj, text, html, rcpts, smtp=None: [   # noqa: E731
            {"email": e, "ok": True, "error": None} for e in rcpts]
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""}), \
                mock.patch("oikonome.web.report.send_each", sent):
            r = self.client.post("/api/settings/smtp-test")
        self.assertEqual(r.status_code, 200, r.text)
        skipped = {s["email"]: s["reason"] for s in r.json()["skipped"]}
        self.assertIn(self.partner, skipped)
        self.assertIn("paused", skipped[self.partner])
        self.assertNotIn("invitation", skipped[self.partner])

    # ---- lock order ---------------------------------------------------------

    def test_a_password_change_waits_for_the_settings_row_holding_no_users_row(self):
        """While another transaction holds the household's settings row,
        the password change must be waiting for it BEFORE it writes the
        users row: a users row held across that wait is the half of a
        cycle the deadlock detector cannot see."""
        holder = tenancy.admin_connect()
        result = {}

        def change():
            result["r"] = self.client.post("/api/password/change", json={
                "current_password": PW, "new_password": PW2})

        t = threading.Thread(target=change)
        try:
            with holder.transaction():
                holder.execute("SELECT 1 FROM tenant_settings "
                               "WHERE tenant_id=%s FOR UPDATE", (self.tid,))
                t.start()
                deadline = time.monotonic() + 4
                waiting = False
                while time.monotonic() < deadline and not waiting:
                    waiting = bool(holder.execute(
                        "SELECT 1 FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock' "
                        "AND pid <> pg_backend_pid()").fetchone())
                    if not waiting:
                        time.sleep(0.05)
                self.assertTrue(waiting, "the change never reached the "
                                         "settings row")
                probe = tenancy.admin_connect()
                try:
                    try:
                        with probe.transaction():
                            probe.execute(
                                "SELECT 1 FROM users WHERE id=%s "
                                "FOR UPDATE NOWAIT", (self.uid,))
                        users_free = True
                    except psycopg.errors.LockNotAvailable:
                        users_free = False
                finally:
                    probe.close()
            t.join(15)
        finally:
            holder.close()
        self.assertTrue(users_free, "the users row was held while waiting "
                                    "on the settings row")
        self.assertEqual(result["r"].status_code, 200, result["r"].text)


class RecoveryMailRelayTests(_Household):
    """Account-recovery mail goes through the operator's relay whenever
    there is one. The Settings relay is what a hijacker can repoint, and
    the reset link an owner asks for to take the account back is mailed
    before any recovery has completed — before any pause exists."""

    ENV_RELAY = {"OIKONOME_SMTP_HOST": "env-relay.example.dev",
                 "OIKONOME_HOSTED": "",
                 "OIKONOME_BASE_URL": "https://app.example.dev"}

    def _sent(self):
        sent = []

        def send(subject, plain, html, to, smtp=None, **kw):
            sent.append(smtp)
        return sent, send

    def test_an_unpaused_settings_relay_does_not_carry_recovery_mail(self):
        with mock.patch.dict(os.environ, self.ENV_RELAY), \
                mock.patch.object(self.appmod, "DEV_MODE", False):
            smtp = self.appmod._smtp_for(self.email)
        self.assertEqual(smtp["host"], "env-relay.example.dev")

    def test_without_an_operator_relay_the_settings_relay_still_sends(self):
        env = {**self.ENV_RELAY, "OIKONOME_SMTP_HOST": ""}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.appmod, "DEV_MODE", False):
            smtp = self.appmod._smtp_for(self.email)
        self.assertEqual(smtp["host"], "relay.example.dev")

    def test_a_paused_relay_sends_the_reset_through_the_operator(self):
        self._rotate()
        sent, send = self._sent()
        with mock.patch.dict(os.environ, self.ENV_RELAY), \
                mock.patch.object(self.appmod, "DEV_MODE", False), \
                mock.patch("oikonome.web.report.send", send):
            self.appmod._deliver_reset(self.email, "tok")
        self.assertEqual([s["host"] for s in sent], ["env-relay.example.dev"])

    def test_a_paused_relay_with_no_operator_relay_logs_the_reset(self):
        self._rotate()
        sent, send = self._sent()
        env = {**self.ENV_RELAY, "OIKONOME_SMTP_HOST": ""}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(self.appmod, "DEV_MODE", False), \
                mock.patch("oikonome.web.report.send", send), \
                mock.patch.object(self.appmod, "_log_link") as logged:
            self.appmod._deliver_reset(self.email, "tok")
        self.assertEqual(sent, [], "nothing goes through the paused relay")
        logged.assert_called_once()


if __name__ == "__main__":
    unittest.main()
