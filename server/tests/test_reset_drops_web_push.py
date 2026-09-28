"""A password reset drops the browser web-push subscriptions.

Every posture change that evicts sessions drops them; otherwise a
hijacker's browser keeps receiving the verdict text and alert titles after
the owner recovers the account by email.
"""

import unittest
import uuid

from oikonome.auth import passwords, reset
from oikonome.db import tenancy

from .util import _ensure_db


class ResetDropsPushTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_consume_deletes_the_users_subscriptions(self):
        admin = tenancy.admin_connect()
        try:
            email = f"rp-{uuid.uuid4().hex[:8]}@x.dev"
            tid = tenancy.create_tenant(admin, email)
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (tid, email, passwords.hash_password("x" * 12))
            ).fetchone()["id"]
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth) VALUES (%s, %s, %s, 'k', 'a')",
                (uid, tid, f"https://push.example/{uuid.uuid4().hex}"))
            rid = admin.execute(
                "INSERT INTO password_resets (user_id, token_hash, "
                "expires_at) VALUES (%s, %s, now() + interval '1 hour') "
                "RETURNING id", (uid, uuid.uuid4().hex)).fetchone()["id"]
            admin.commit()
            self.assertTrue(reset.consume(
                admin, rid, uid, passwords.hash_password("y" * 12)))
            admin.commit()
            left = admin.execute(
                "SELECT count(*) AS n FROM push_subscriptions "
                "WHERE user_id=%s", (uid,)).fetchone()["n"]
            self.assertEqual(left, 0)
        finally:
            admin.close()
