"""Web Push is signed with a contact a push service accepts, and every
send is bounded in time.

The VAPID `sub` claim tells a push service who to contact about this
instance's pushes, and Apple's service refuses every delivery signed with
a localhost one: Safari and home-screen subscriptions would fail on every
send while Settings showed push as on. And a push service that accepts a
connection and then goes quiet must not hold the household's send (and
the lock it runs under) for ever.
"""
import os
import unittest
import uuid
from unittest import mock

from oikonome.db import tenancy
from oikonome.notify import push

from .util import _ensure_db

ENV = ("OIKONOME_PUSH_CONTACT", "OIKONOME_SUPPORT_EMAIL",
       "OIKONOME_SMTP_FROM", "OIKONOME_BASE_URL")


def _env(**kw):
    clean = {k: "" for k in ENV}
    clean.update(kw)
    return mock.patch.dict(os.environ, clean)


class VapidSubjectTests(unittest.TestCase):

    def test_an_explicit_contact_wins(self):
        with _env(OIKONOME_PUSH_CONTACT="ops@finance.example.org",
                  OIKONOME_SUPPORT_EMAIL="help@finance.example.org"):
            self.assertEqual(push.vapid_subject(),
                             "mailto:ops@finance.example.org")
        with _env(OIKONOME_PUSH_CONTACT="https://finance.example.org/contact"):
            self.assertEqual(push.vapid_subject(),
                             "https://finance.example.org/contact")

    def test_falls_back_through_the_operators_addresses(self):
        with _env(OIKONOME_SUPPORT_EMAIL="help@finance.example.org",
                  OIKONOME_SMTP_FROM="mail@finance.example.org"):
            self.assertEqual(push.vapid_subject(),
                             "mailto:help@finance.example.org")
        with _env(OIKONOME_SMTP_FROM="mail@finance.example.org"):
            self.assertEqual(push.vapid_subject(),
                             "mailto:mail@finance.example.org")
        with _env(OIKONOME_BASE_URL="https://finance.example.org/"):
            self.assertEqual(push.vapid_subject(),
                             "https://finance.example.org")

    def test_a_localhost_address_is_never_the_subject_when_another_exists(self):
        with _env(OIKONOME_SMTP_FROM="oikonome@localhost",
                  OIKONOME_BASE_URL="https://finance.example.org"):
            self.assertEqual(push.vapid_subject(),
                             "https://finance.example.org")
        with _env(OIKONOME_BASE_URL="http://finance.example.org"):
            self.assertEqual(push.vapid_subject(), push._FALLBACK_SUBJECT)


class SendTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self._prev_mk = os.environ.pop("OIKONOME_MASTER_KEY", None)
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM push_vapid")
            email = f"pushsub-{uuid.uuid4().hex[:10]}@example.dev"
            self.tid = str(tenancy.create_tenant(admin, email))
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash) "
                "VALUES (%s, %s, 'x') RETURNING id",
                (self.tid, email)).fetchone()["id"]
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth) VALUES (%s, %s, %s, 'k', 'a')",
                (uid, self.tid,
                 "https://web.push.apple.com/Q" + uuid.uuid4().hex))
        finally:
            admin.close()

    def tearDown(self):
        if self._prev_mk is not None:
            os.environ["OIKONOME_MASTER_KEY"] = self._prev_mk

    def test_the_send_carries_the_subject_and_a_timeout(self):
        conn = tenancy.control_connect()
        try:
            with _env(OIKONOME_SUPPORT_EMAIL="help@finance.example.org"), \
                    mock.patch("pywebpush.webpush") as wp, \
                    mock.patch.object(push, "available", return_value=True):
                self.assertEqual(push.send_tenant(conn, self.tid, "t", "b"), 1)
        finally:
            conn.close()
        kw = wp.call_args.kwargs
        self.assertEqual(kw["vapid_claims"]["sub"],
                         "mailto:help@finance.example.org")
        self.assertIsNotNone(kw.get("timeout"))
        self.assertLessEqual(kw["timeout"], 30)


if __name__ == "__main__":
    unittest.main()
