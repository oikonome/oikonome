"""A browser's web-push subscription is a durable destination for the
household's verdicts, alerts and account-security notices.

So adding one is a step-up act, like an SMS number or a webhook — a stolen
cookie alone must not add the thief's own browser — and it lives only as
long as the browser session that made it: when that session signs out, is
revoked or expires, the channel goes with it. The same browser refreshing
the subscription it already holds is not a new destination and needs no
step-up.
"""

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        from oikonome.auth import passwords
        from oikonome.web import security
        security._limiter._hits.clear()
        self.email = f"wps-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(admin, self.email)
            self.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (self.tid, self.email,
                 passwords.hash_password(PW))).fetchone()["id"]
        finally:
            admin.close()
        self.client = self._login()

    def _login(self):
        c = TestClient(self.appmod.app)
        r = c.post("/login", data={"email": self.email, "password": PW},
                   follow_redirects=False)
        assert r.status_code == 303, r.text
        return c

    def _sub(self):
        return {"endpoint":
                f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}",
                "keys": {"p256dh": uuid.uuid4().hex, "auth": "a1"}}

    def _rows(self):
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT endpoint, session_id FROM push_subscriptions "
                "WHERE user_id=%s", (self.uid,)).fetchall()
        finally:
            admin.close()

    def _elevate(self, client):
        r = client.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)


class StepUpTests(_Base):
    def test_a_cookie_alone_cannot_add_a_browser(self):
        r = self.client.post("/api/notify/push/subscribe", json=self._sub())
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"]["error"], "elevation_required")
        self.assertEqual(self._rows(), [])

    def test_an_elevated_session_adds_one_bound_to_itself(self):
        self._elevate(self.client)
        r = self.client.post("/api/notify/push/subscribe", json=self._sub())
        self.assertEqual(r.status_code, 200, r.text)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNotNone(rows[0]["session_id"])

    def test_the_same_browser_refreshing_needs_no_step_up(self):
        sub = self._sub()
        self._elevate(self.client)
        self.assertEqual(self.client.post(
            "/api/notify/push/subscribe", json=sub).status_code, 200)
        # past the window: the same endpoint and keys are still accepted
        later = self._login()
        r = later.post("/api/notify/push/subscribe", json=sub)
        self.assertEqual(r.status_code, 200, r.text)
        # …but new keys on the endpoint are a new destination
        r = later.post("/api/notify/push/subscribe", json={
            "endpoint": sub["endpoint"],
            "keys": {"p256dh": "other", "auth": "other"}})
        self.assertEqual(r.status_code, 403, r.text)

    def test_a_device_token_cannot_make_a_browser_subscription(self):
        from oikonome.auth import device_tokens
        admin = tenancy.admin_connect()
        try:
            tok, _row = device_tokens.mint(admin, self.uid, self.tid,
                                           "phone", "android")
        finally:
            admin.close()
        c = TestClient(self.appmod.app)
        r = c.post("/api/notify/push/subscribe", json=self._sub(),
                   headers={"Authorization": f"Bearer {tok}"})
        self.assertIn(r.status_code, (401, 403), r.text)
        self.assertEqual(self._rows(), [])


class LifetimeTests(_Base):
    def _subscribed(self, client):
        self._elevate(client)
        r = client.post("/api/notify/push/subscribe", json=self._sub())
        self.assertEqual(r.status_code, 200, r.text)

    def test_signing_out_drops_that_sessions_subscription(self):
        other = self._login()
        self._subscribed(self.client)
        self._subscribed(other)
        self.assertEqual(len(self._rows()), 2)
        r = self.client.post("/api/logout")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self._rows()), 1)

    def test_revoking_one_session_drops_only_its_subscription(self):
        from oikonome.auth import sessions
        self._subscribed(self.client)
        admin = tenancy.admin_connect()
        try:
            sid = admin.execute(
                "SELECT session_id FROM push_subscriptions WHERE user_id=%s",
                (self.uid,)).fetchone()["session_id"]
            self.assertEqual(sessions.revoke_by_id(admin, self.uid, sid), 1)
        finally:
            admin.close()
        self.assertEqual(self._rows(), [])

    def test_an_expired_sessions_subscription_goes_at_the_next_sign_in(self):
        self._subscribed(self.client)
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE sessions SET expires_at = now() - "
                          "interval '1 minute' WHERE user_id=%s", (self.uid,))
        finally:
            admin.close()
        self._login()
        self.assertEqual(self._rows(), [])

    def test_an_expired_session_stops_receiving_before_any_sweep(self):
        """Between a session's expiry and the next sign-in or sweep that
        removes it, its browser must already be off the delivery list —
        and off the count the settings page reads."""
        from oikonome.notify import push
        self._subscribed(self.client)
        # a subscription from before subscriptions named their session
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth) VALUES (%s,%s,%s,'p','a')",
                (self.uid, self.tid,
                 f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}"))
            admin.execute("UPDATE sessions SET expires_at = now() - "
                          "interval '1 minute' WHERE user_id=%s", (self.uid,))
            with mock.patch("pywebpush.webpush") as wp, \
                    mock.patch.object(push, "available", return_value=True):
                n = push.send_tenant(admin, self.tid, "t", "b")
            live = admin.execute(
                f"SELECT count(*) AS n FROM push_subscriptions "
                f"WHERE user_id=%s AND {push.LIVE}",
                (self.uid,)).fetchone()["n"]
        finally:
            admin.close()
        # the unbound row still receives; the expired session's does not
        self.assertEqual(n, 1)
        self.assertEqual(wp.call_count, 1)
        self.assertEqual(live, 1)
        self.assertEqual(len(self._rows()), 2)


class FactorResetNoticeTests(_Base):
    def test_a_browser_on_the_account_does_not_silence_the_household(self):
        """Whoever held the session could have added their own browser, so
        a web subscription alone does not stand in for the household's
        neutral notice."""
        self._subscribed_raw()
        sent = []

        def web(cc, tid, title, body, url="/app/", *, user_id=None, **_kw):
            sent.append(user_id)
        with mock.patch("oikonome.notify.push.send_tenant", web), \
                mock.patch("oikonome.notify.push_native.send_tenant",
                           lambda *a, **k: None), \
                mock.patch("oikonome.notify.sms.send", lambda *a: None), \
                mock.patch.object(self.appmod, "_smtp_for",
                                  return_value=None):
            self.appmod._deliver_factor_reset_notice(
                self.email, self.tid,
                dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc), "tok")
        self.assertIn(self.uid, sent)
        self.assertIn(None, sent)

    def _subscribed_raw(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth) VALUES (%s,%s,%s,'p','a')",
                (self.uid, self.tid,
                 f"https://push.example.com/{uuid.uuid4().hex}"))
        finally:
            admin.close()


if __name__ == "__main__":
    unittest.main()
