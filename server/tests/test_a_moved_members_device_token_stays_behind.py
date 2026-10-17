"""A device or widget token answers only while its holder still belongs to
the household it was minted in.

The token row carries the tenant it was minted for. Every path that moves
a person to another household revokes their tokens today, but the lookup
must not depend on that: a token whose holder now sits in a different
household (or whose widget names a different person or household from
its parent device) answers 401, never the old household's data.

The same holds for a browser session, and for the push notices both kinds
of device receive: a moved person's phone and browsers stop being the old
household's push targets the moment they belong to another household.
"""
import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import device_tokens, widget_tokens
from oikonome.db import tenancy

from .util import _ensure_db


class AMovedMembersDeviceTokenStaysBehind(unittest.TestCase):
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
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(admin, f"dt-{uuid.uuid4().hex[:6]}")
            self.other = tenancy.create_tenant(admin,
                                               f"dt2-{uuid.uuid4().hex[:6]}")
            self.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s, %s, 'x', 'member', now()) "
                "RETURNING id",
                (self.tid, f"dt-{uuid.uuid4().hex[:8]}@example.dev")
            ).fetchone()["id"]
            self.dtok, row = device_tokens.mint(admin, self.uid, self.tid,
                                                "phone", "android")
            self.wtok = widget_tokens.mint(admin, self.uid, self.tid,
                                           row["id"])
        finally:
            admin.close()

    def _me(self, tok):
        return TestClient(self.app).get(
            "/api/me", headers={"Authorization": f"Bearer {tok}"})

    def _move(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET tenant_id=%s WHERE id=%s",
                          (self.other, self.uid))
        finally:
            admin.close()

    def test_both_answer_while_the_holder_is_in_the_household(self):
        self.assertEqual(self._me(self.dtok).status_code, 200)
        conn = tenancy.control_connect()
        try:
            self.assertIsNotNone(widget_tokens.lookup(conn, self.wtok))
        finally:
            conn.close()

    def test_neither_answers_once_the_holder_has_moved(self):
        self._move()
        self.assertEqual(self._me(self.dtok).status_code, 401)
        conn = tenancy.control_connect()
        try:
            self.assertIsNone(device_tokens.lookup(conn, self.dtok))
            self.assertIsNone(widget_tokens.lookup(conn, self.wtok))
        finally:
            conn.close()


class AMovedMembersSessionAndPushStayBehind(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.auth import sessions
        from oikonome.web import security
        security._limiter._hits.clear()
        admin = tenancy.admin_connect()
        try:
            self.tid = tenancy.create_tenant(admin, f"mv-{uuid.uuid4().hex[:6]}")
            self.other = tenancy.create_tenant(admin,
                                               f"mv2-{uuid.uuid4().hex[:6]}")
            self.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s, %s, 'x', 'member', now()) "
                "RETURNING id",
                (self.tid, f"mv-{uuid.uuid4().hex[:8]}@example.dev")
            ).fetchone()["id"]
            self.sess = sessions.create_session(admin, self.uid, self.tid)
            sid = admin.execute(
                "SELECT id FROM sessions WHERE user_id=%s",
                (self.uid,)).fetchone()["id"]
            _tok, row = device_tokens.mint(admin, self.uid, self.tid,
                                           "phone", "android")
            self.push_tok = f"native-{uuid.uuid4().hex}"
            device_tokens.set_push(admin, self.uid, row["id"],
                                   self.push_tok, "android")
            self.endpoints = [f"https://push.example/{uuid.uuid4().hex}"
                              for _ in range(2)]
            # one bound to the live session, one legacy row with none
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth, session_id) "
                "VALUES (%s, %s, %s, 'k', 'a', %s)",
                (self.uid, self.tid, self.endpoints[0], sid))
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth) VALUES (%s, %s, %s, 'k', 'a')",
                (self.uid, self.tid, self.endpoints[1]))
        finally:
            admin.close()

    def _move(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE users SET tenant_id=%s WHERE id=%s",
                          (self.other, self.uid))
        finally:
            admin.close()

    def _native(self) -> set[str]:
        conn = tenancy.control_connect()
        try:
            return {r["push_token"]
                    for r in device_tokens.push_targets(conn, self.tid)}
        finally:
            conn.close()

    def _web(self) -> set[str]:
        from oikonome.notify import push
        admin = tenancy.admin_connect()
        try:
            return {r["endpoint"] for r in admin.execute(
                "SELECT endpoint FROM push_subscriptions "
                f"WHERE tenant_id=%s AND {push.LIVE}",
                (self.tid,)).fetchall()}
        finally:
            admin.close()

    def _me(self):
        from oikonome.auth.sessions import COOKIE_NAME
        c = TestClient(self.app)
        c.cookies.set(COOKIE_NAME, self.sess)
        return c.get("/api/me")

    def test_everything_answers_while_the_person_is_in_the_household(self):
        self.assertEqual(self._me().status_code, 200)
        self.assertEqual(self._native(), {self.push_tok})
        self.assertEqual(self._web(), set(self.endpoints))

    def test_a_moved_persons_session_answers_401(self):
        self._move()
        self.assertEqual(self._me().status_code, 401)
        from oikonome.auth import sessions
        conn = tenancy.control_connect()
        try:
            self.assertIsNone(sessions.lookup_session(conn, self.sess))
        finally:
            conn.close()

    def test_a_moved_person_gets_no_push_from_the_old_household(self):
        self._move()
        self.assertEqual(self._native(), set())
        self.assertEqual(self._web(), set())


if __name__ == "__main__":
    unittest.main()
