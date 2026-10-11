"""Nothing slow runs while the per-user factor lock is held.

The factor lock is taken on a connection from the small control pool, the
same pool every request's session lookup draws from. A door that resolves
a hostname, hashes a password or talks to a mail server while holding it
keeps a control connection — and every request queued behind the lock
keeps one more — for as long as that work takes. A caller who names a
host whose nameserver never answers could then hold the pool for every
household on the instance with a handful of requests.

So the webhook doors validate the URL first, with nothing held, and take
the lock only around the re-check and the write; and a control connection
that waits on a lock gives up after a bounded time instead of queueing
forever, answering 409 so the client can retry.
"""
import contextlib
import os
import time
import unittest
import uuid
from unittest import mock

import psycopg
from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"


def _factor_lock_held(uid) -> bool:
    """Whether another session holds the user's factor lock right now."""
    admin = tenancy.admin_connect()
    try:
        with admin.transaction():
            got = admin.execute(
                "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS ok",
                ("oikonome:passkeys:" + str(uid),)).fetchone()["ok"]
        return not got
    finally:
        admin.close()


class SlowWorkStaysOutsideTheFactorLock(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import netguard, security
        security._limiter._hits.clear()
        netguard._DNS_CACHE.clear()
        self.client = TestClient(self.app)
        self.email = f"slowlock-{uuid.uuid4().hex[:8]}@example.dev"
        r = self.client.post("/api/signup", data={"email": self.email,
                                                  "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        admin = tenancy.admin_connect()
        try:
            self.uid = admin.execute("SELECT id FROM users WHERE email=%s",
                                     (self.email,)).fetchone()["id"]
        finally:
            admin.close()

    def _watching_resolver(self):
        """A resolver that records, at each lookup, whether the caller's
        factor lock was held while the name was being resolved."""
        from oikonome.web import netguard
        seen: list[bool] = []

        def resolve(host):
            seen.append(_factor_lock_held(self.uid))
            return ["93.184.216.34"]
        return seen, mock.patch.object(netguard, "_getaddrinfo", resolve)

    def test_webhook_create_resolves_before_taking_the_lock(self):
        seen, patch = self._watching_resolver()
        with patch:
            r = self.client.post("/api/webhooks", json={
                "url": f"https://{uuid.uuid4().hex}.hooks.example/in",
                "events": ["*"], "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(seen, "the URL was never resolved")
        self.assertEqual(seen, [False] * len(seen))

    def test_webhook_update_resolves_before_taking_the_lock(self):
        from oikonome.web import netguard
        with mock.patch.object(netguard, "_getaddrinfo",
                               lambda h: ["93.184.216.34"]):
            hook = self.client.post("/api/webhooks", json={
                "url": "https://first.hooks.example/in", "events": ["*"],
                "password": PW}).json()
        seen, patch = self._watching_resolver()
        with patch:
            r = self.client.post(f"/api/webhooks/{hook['id']}", json={
                "url": f"https://{uuid.uuid4().hex}.hooks.example/in",
                "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(seen, "the URL was never resolved")
        self.assertEqual(seen, [False] * len(seen))

    def test_a_lookup_under_the_lock_never_reaches_a_nameserver(self):
        """The class, not the door: whatever runs inside the factor-lock
        block judges a name on the answer already cached for it."""
        from oikonome.web import app as appmod, netguard
        calls = []
        with mock.patch.object(netguard, "_getaddrinfo",
                               lambda h: calls.append(h) or ["1.1.1.1"]):
            user = {"user_id": str(self.uid)}
            with appmod._still_signed_in_under_factor_lock(user):
                netguard.check_url("https://never-resolved.example/x")
        self.assertEqual(calls, [])

    def test_webhook_destination_changes_are_limited_per_account(self):
        from oikonome.web import netguard
        with mock.patch.object(netguard, "_getaddrinfo",
                               lambda h: ["93.184.216.34"]):
            hook = self.client.post("/api/webhooks", json={
                "url": "https://first.hooks.example/in", "events": ["*"],
                "password": PW}).json()
            codes = []
            for i in range(25):
                # each from a fresh client address, so only a per-account
                # budget can stop the run
                from oikonome.web import security
                with mock.patch.object(security, "client_ip",
                                       lambda req, i=i: f"203.0.113.{i}"):
                    codes.append(self.client.post(
                        f"/api/webhooks/{hook['id']}",
                        json={"url": f"https://h{i}.hooks.example/in",
                              "password": PW}).status_code)
        self.assertIn(429, codes)
        self.assertEqual(codes[0], 200)


@contextlib.contextmanager
def _short_lock_timeout_pool():
    """A fresh control pool built with a short lock wait, so the test does
    not sit out the production default; the old pool is put back after."""
    saved = dict(tenancy._control_pools)
    tenancy._control_pools.clear()
    with mock.patch.dict(os.environ,
                         {"OIKONOME_CONTROL_LOCK_TIMEOUT_MS": "300"}):
        try:
            yield
        finally:
            for p in tenancy._control_pools.values():
                p.close()
            tenancy._control_pools.clear()
            tenancy._control_pools.update(saved)


class ControlConnectionsStopWaitingOnLocks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def test_control_connections_carry_the_lock_and_idle_limits(self):
        with tenancy.control_connect() as c:
            lt = c.execute("SHOW lock_timeout").fetchone()["lock_timeout"]
            it = c.execute("SHOW idle_in_transaction_session_timeout"
                           ).fetchone()["idle_in_transaction_session_timeout"]
            st = c.execute("SHOW statement_timeout"
                           ).fetchone()["statement_timeout"]
        self.assertNotIn(lt, ("0", "0ms"))
        self.assertNotIn(it, ("0", "0ms"))
        self.assertNotIn(st, ("0", "0ms"))

    def test_a_held_factor_lock_times_out_instead_of_waiting_forever(self):
        key = "oikonome:passkeys:" + str(uuid.uuid4())
        holder = tenancy.admin_connect()
        try:
            with holder.transaction():
                holder.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                               (key,))
                with _short_lock_timeout_pool():
                    t0 = time.monotonic()
                    with self.assertRaises(psycopg.errors.LockNotAvailable):
                        with tenancy.control_connect() as c, c.transaction():
                            c.execute("SELECT pg_advisory_xact_lock("
                                      "hashtext(%s))", (key,))
                    self.assertLess(time.monotonic() - t0, 5)
        finally:
            holder.close()

    def test_a_door_that_cannot_get_the_lock_answers_409(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        client = TestClient(self.app)
        email = f"lockwait-{uuid.uuid4().hex[:8]}@example.dev"
        self.assertEqual(client.post("/api/signup", data={
            "email": email, "password": PW}).status_code, 200)
        admin = tenancy.admin_connect()
        try:
            uid = admin.execute("SELECT id FROM users WHERE email=%s",
                                (email,)).fetchone()["id"]
        finally:
            admin.close()
        holder = tenancy.admin_connect()
        try:
            with holder.transaction():
                holder.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                               ("oikonome:passkeys:" + str(uid),))
                with _short_lock_timeout_pool():
                    r = client.post("/api/password/change", json={
                        "current_password": PW,
                        "new_password": "another-long-passphrase-42"})
        finally:
            holder.close()
        self.assertEqual(r.status_code, 409, r.text)
        self.assertEqual(r.json().get("error"), "busy")


if __name__ == "__main__":
    unittest.main()
