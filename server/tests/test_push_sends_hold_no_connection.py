"""A push send on a request path holds no control-plane connection while
a push service answers.

Each web-push POST and each relay call waits on a service outside the
instance (bounded, but in seconds), and the control pool is a handful of
connections that every signed-in request draws its session check from. A
send that holds one across those waits lets a few slow pushes stall the
whole instance. The test push and the factor-reset notice are the two
request-path senders; both must read who to send to, give the connection
back, and only then send.
"""
import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import device_tokens
from oikonome.db import tenancy
from oikonome.notify import push

from .util import _ensure_db

PW = "correct-horse-battery"


class _Tracker:
    """Wraps tenancy.control_connect and counts the handles still open."""

    def __init__(self):
        self.real = tenancy.control_connect
        self.open = []
        self.seen_at_send = []

    def connect(self, *a, **k):
        tracker = self
        inner = self.real(*a, **k)

        class Handle:
            def __getattr__(self, name):
                return getattr(inner, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.close()

            def close(self):
                if self in tracker.open:
                    tracker.open.remove(self)
                inner.close()
        h = Handle()
        self.open.append(h)
        return h

    def webpush(self, **kw):
        self.seen_at_send.append(("web", len(self.open)))

    def relay(self, url, json=None, **kw):
        self.seen_at_send.append(("relay", len(self.open)))
        r = mock.Mock()
        r.status_code = 200
        r.json = lambda: {"sent": len(json["notifications"]), "dead": []}
        r.text = ""
        return r


class PushSendsHoldNoConnection(unittest.TestCase):
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
        self.email = f"pnh-{uuid.uuid4().hex[:8]}@example.dev"
        r = self.client.post("/api/signup",
                             data={"email": self.email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        admin = tenancy.admin_connect()
        try:
            self.uid = admin.execute(
                "SELECT id FROM users WHERE email=%s",
                (self.email,)).fetchone()["id"]
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth) VALUES (%s,%s,%s,'p','a')",
                (self.uid, self.tid,
                 f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}"))
            _tok, row = device_tokens.mint(admin, self.uid, self.tid,
                                           "phone", "android")
            device_tokens.set_push(admin, self.uid, row["id"],
                                   uuid.uuid4().hex, "fcm")
        finally:
            admin.close()

    def _patched(self, t):
        return (mock.patch.object(tenancy, "control_connect", t.connect),
                mock.patch("pywebpush.webpush", t.webpush),
                mock.patch.object(push, "available", return_value=True),
                mock.patch("httpx.post", t.relay),
                mock.patch.dict(os.environ, {
                    "OIKONOME_PUSH_RELAY_URL": "https://relay.example"}))

    def _assert_none_held(self, t):
        kinds = {k for k, _ in t.seen_at_send}
        self.assertEqual({"web", "relay"}, kinds, t.seen_at_send)
        self.assertEqual([0] * len(t.seen_at_send),
                         [n for _, n in t.seen_at_send], t.seen_at_send)

    def test_the_test_push_door(self):
        t = _Tracker()
        a, b, c, d, e = self._patched(t)
        with a, b, c, d, e:
            r = self.client.post("/api/notify/test", json={"channel": "push"})
        self.assertEqual(200, r.status_code, r.text)
        self._assert_none_held(t)

    def test_the_factor_reset_notice(self):
        t = _Tracker()
        a, b, c, d, e = self._patched(t)
        with a, b, c, d, e, \
                mock.patch("oikonome.notify.sms.send", lambda *a: None), \
                mock.patch.object(self.appmod, "_smtp_for",
                                  return_value=None):
            self.appmod._deliver_factor_reset_notice(
                self.email, self.tid,
                dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc), "tok")
        self._assert_none_held(t)

    def test_a_gone_subscription_is_still_retired(self):
        """Holding no connection across the send must not lose the write
        that follows it: a 410 from the push service retires the row."""
        from pywebpush import WebPushException
        resp = mock.Mock(status_code=410)

        def gone(**kw):
            raise WebPushException("gone", response=resp)
        with mock.patch("pywebpush.webpush", gone), \
                mock.patch.object(push, "available", return_value=True):
            push.send_tenant(None, self.tid, "t", "b",
                             connect=tenancy.control_connect)
        admin = tenancy.admin_connect()
        try:
            dead = admin.execute(
                "SELECT dead_at FROM push_subscriptions WHERE user_id=%s",
                (self.uid,)).fetchone()["dead_at"]
        finally:
            admin.close()
        self.assertIsNotNone(dead)


if __name__ == "__main__":
    unittest.main()
