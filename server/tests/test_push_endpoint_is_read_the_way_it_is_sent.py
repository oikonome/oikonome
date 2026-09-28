"""The web-push endpoint allowlist holds for the parser that sends.

A subscription endpoint is a URL the server later POSTs to, so the
allowlist of push-service hosts is what keeps it from being a request
forgery primitive. The check and the delivery use different URL parsers,
and they disagree about some strings: a backslash before an `@` hides the
real authority from one and shows it to the other. Such an endpoint must
be refused at subscribe time, and a row stored before the check learned
that must never be sent to.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"

SMUGGLED = [
    "https://169.254.169.254\\@fcm.googleapis.com/fcm/send/x",
    "https://internal.example\\@updates.push.services.mozilla.com/wpush/v2/x",
    "https://user@fcm.googleapis.com/fcm/send/x",
    "https://fcm.googleapis.com:8443/fcm/send/x",
    "https://fcm.googleapis.com/fcm/send/x y",
    "https://fcm.googleapis.com/fcm/send/\x00x",
]
GENUINE = [
    "https://fcm.googleapis.com/fcm/send/abc:APA91bExample-token_1",
    "https://updates.push.services.mozilla.com/wpush/v2/gAAAAABexample",
    "https://web.push.apple.com/QExampleToken",
    "https://wns2-by3p.notify.windows.com/w/?token=BQYAAAExample%3d",
]


class EndpointCheckTests(unittest.TestCase):

    def test_smuggled_authorities_are_refused(self):
        from oikonome.notify import push
        for ep in SMUGGLED:
            self.assertFalse(push.endpoint_allowed(ep), repr(ep))

    def test_real_push_services_are_accepted(self):
        from oikonome.notify import push
        for ep in GENUINE:
            self.assertTrue(push.endpoint_allowed(ep), ep)

    def test_the_host_the_sender_reads_is_the_host_checked(self):
        """For every accepted endpoint, urllib3 — what the delivery POST
        goes through — names the same host the allowlist matched."""
        from urllib.parse import urlsplit

        from urllib3.util import parse_url
        for ep in GENUINE:
            self.assertEqual(parse_url(ep).host, urlsplit(ep).hostname, ep)


class SubscribeAndSendTests(unittest.TestCase):
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

    def test_subscribe_refuses_a_smuggled_endpoint(self):
        c = TestClient(self.app)
        email = f"pushep-{uuid.uuid4().hex[:10]}@example.dev"
        r = c.post("/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        r = c.post("/api/notify/push/subscribe", json={
            "endpoint": SMUGGLED[0], "keys": {"p256dh": "k", "auth": "a"}})
        self.assertEqual(r.status_code, 400, r.text)

    def test_a_stored_smuggled_endpoint_is_never_posted_to(self):
        from oikonome.notify import push
        email = f"pushep-{uuid.uuid4().hex[:10]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, email)
            from oikonome.auth import passwords
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash) "
                "VALUES (%s, %s, %s) RETURNING id",
                (tid, email, passwords.hash_password(PW))).fetchone()["id"]
            ep = SMUGGLED[0] + uuid.uuid4().hex
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth) VALUES (%s, %s, %s, 'k', 'a')",
                (uid, tid, ep))
        finally:
            admin.close()
        conn = tenancy.control_connect()
        try:
            with mock.patch("pywebpush.webpush") as wp, \
                    mock.patch.object(push, "available", return_value=True):
                self.assertEqual(push.send_tenant(conn, str(tid), "t", "b"),
                                 0)
            wp.assert_not_called()
            dead = conn.execute(
                "SELECT dead_at FROM push_subscriptions WHERE endpoint=%s",
                (ep,)).fetchone()["dead_at"]
            self.assertIsNotNone(dead)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
