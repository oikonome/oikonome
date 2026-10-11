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
        # The instance signing key is a singleton in the shared test
        # database; a suite that ran with a master key leaves it encrypted
        # under a key this module never sees. Start each test without one
        # (and without a master key) so the outcome does not depend on
        # which suites ran first.
        self._prev_mk = os.environ.pop("OIKONOME_MASTER_KEY", None)
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM push_vapid")
        finally:
            admin.close()

    def tearDown(self):
        if self._prev_mk is not None:
            os.environ["OIKONOME_MASTER_KEY"] = self._prev_mk

    def _tenant_with_subscription(self, endpoint):
        email = f"pushep-{uuid.uuid4().hex[:10]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, email)
            from oikonome.auth import passwords
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash) "
                "VALUES (%s, %s, %s) RETURNING id",
                (tid, email, passwords.hash_password(PW))).fetchone()["id"]
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth) VALUES (%s, %s, %s, 'k', 'a')",
                (uid, tid, endpoint))
        finally:
            admin.close()
        return tid

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
        ep = SMUGGLED[0] + uuid.uuid4().hex
        tid = self._tenant_with_subscription(ep)
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

    def _store_key_under_a_master_key_we_no_longer_have(self):
        """The instance signing key as the startup sweep leaves it, then
        the master key removed: a ciphertext nobody here can read."""
        from cryptography.fernet import Fernet

        from oikonome.db import crypto
        from oikonome.notify import push
        os.environ["OIKONOME_MASTER_KEY"] = Fernet.generate_key().decode()
        try:
            priv, pub = push._generate_keypair()
            stored = crypto.encrypt_cp(priv)
        finally:
            del os.environ["OIKONOME_MASTER_KEY"]
        admin = tenancy.admin_connect()
        try:
            admin.execute("INSERT INTO push_vapid (id, private_key, "
                          "public_key) VALUES (1, %s, %s)", (stored, pub))
        finally:
            admin.close()
        return stored

    def test_an_unreadable_signing_key_skips_web_push_without_raising(self):
        """A key encrypted under a master key the process no longer has
        must not crash the send (the caller's other channels share its
        try block), must be named in the log, and must be left in place —
        every existing browser subscription is bound to it."""
        from oikonome.notify import push
        stored = self._store_key_under_a_master_key_we_no_longer_have()
        tid = self._tenant_with_subscription(GENUINE[0] + uuid.uuid4().hex)
        conn = tenancy.control_connect()
        try:
            with mock.patch("pywebpush.webpush") as wp, \
                    mock.patch.object(push, "available", return_value=True), \
                    self.assertLogs(push.log, "ERROR") as logs:
                self.assertEqual(push.send_tenant(conn, str(tid), "t", "b"),
                                 0)
            wp.assert_not_called()
            self.assertIn("OIKONOME_MASTER_KEY", "\n".join(logs.output))
            row = conn.execute("SELECT private_key FROM push_vapid "
                               "WHERE id=1").fetchone()
            self.assertEqual(row["private_key"], stored)
        finally:
            conn.close()

    def test_a_smuggled_endpoint_is_retired_even_when_the_key_is_unreadable(
            self):
        """Retiring a disallowed row needs no signing key, so it happens
        whatever state the key is in."""
        from oikonome.notify import push
        self._store_key_under_a_master_key_we_no_longer_have()
        ep = SMUGGLED[0] + uuid.uuid4().hex
        tid = self._tenant_with_subscription(ep)
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
