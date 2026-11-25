"""A browser push subscription is a row the shared web process and worker
read on every household push, so the door that adds one is bounded and a
row that can never be delivered retires itself.

- The subscribe door is rate limited per address, and new destinations
  per account; one account holds at most push.MAX_PER_USER subscriptions
  (the oldest goes when a new browser subscribes).
- Keys must be what a browser sends: p256dh a base64url P-256 point,
  auth 16 bytes. Anything else is a plain 422.
- A subscription whose keys fail locally is marked dead and never tried
  again; a push service answering 503, or a network failure, leaves it
  alive for the next send.
- One send pass reads at most MAX_PER_USER rows per person.
- The phone's twin door (/api/devices/push) is rate limited too.
"""

import os
import unittest
import uuid
from unittest import mock

import requests
from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .push_keys import browser_keys
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
        self.email = f"wpb-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            admin.execute("DELETE FROM push_vapid")
            self.tid = tenancy.create_tenant(admin, self.email)
            self.uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (self.tid, self.email,
                 passwords.hash_password(PW))).fetchone()["id"]
        finally:
            admin.close()

    def _login(self):
        c = TestClient(self.appmod.app)
        r = c.post("/login", data={"email": self.email, "password": PW},
                   follow_redirects=False)
        assert r.status_code == 303, r.text
        return c

    @staticmethod
    def _sub(keys=None):
        return {"endpoint":
                f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}",
                "keys": keys or browser_keys()}

    def _rows(self, live_only=False):
        from oikonome.notify import push
        where = f" AND {push.LIVE}" if live_only else ""
        admin = tenancy.admin_connect()
        try:
            return admin.execute(
                "SELECT id, endpoint, dead_at FROM push_subscriptions "
                f"WHERE user_id=%s{where} ORDER BY created_at, id",
                (self.uid,)).fetchall()
        finally:
            admin.close()

    def _insert(self, keys, n=1):
        admin = tenancy.admin_connect()
        try:
            for _ in range(n):
                admin.execute(
                    "INSERT INTO push_subscriptions (user_id, tenant_id, "
                    "endpoint, p256dh, auth) VALUES (%s, %s, %s, %s, %s)",
                    (self.uid, self.tid,
                     f"https://fcm.googleapis.com/fcm/send/"
                     f"{uuid.uuid4().hex}", keys["p256dh"], keys["auth"]))
        finally:
            admin.close()


class SubscribeDoorTests(_Base):
    def setUp(self):
        super().setUp()
        self.client = self._login()
        r = self.client.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)

    def test_one_account_holds_a_bounded_number_of_browsers(self):
        from oikonome.notify import push
        from oikonome.web import security
        sent = []
        for _ in range(push.MAX_PER_USER + 3):
            security._limiter._hits.clear()
            sub = self._sub()
            sent.append(sub["endpoint"])
            r = self.client.post("/api/notify/push/subscribe", json=sub)
            self.assertEqual(r.status_code, 200, r.text)
        held = [r["endpoint"] for r in self._rows()]
        self.assertEqual(len(held), push.MAX_PER_USER)
        # the oldest went; the newest stays
        self.assertEqual(set(held), set(sent[3:]))

    def test_new_destinations_from_one_account_are_rate_limited(self):
        codes = [self.client.post("/api/notify/push/subscribe",
                                  json=self._sub()).status_code
                 for _ in range(40)]
        self.assertIn(429, codes)
        self.assertEqual(codes[0], 200)

    def test_refused_rebinds_do_not_spend_the_new_destination_allowance(self):
        """A browser whose subscription lapsed re-posts it on every page
        load with no step-up and is refused; those refusals leave the
        account's allowance whole, so turning push back on still works."""
        plain = self._login()                 # signed in, never elevated
        codes = {plain.post("/api/notify/push/subscribe",
                            json=self._sub()).status_code
                 for _ in range(25)}
        self.assertEqual(codes, {403})
        r = self.client.post("/api/notify/push/subscribe", json=self._sub())
        self.assertEqual(r.status_code, 200, r.text)

    def test_the_door_is_rate_limited_per_address(self):
        sub = self._sub()
        self.assertEqual(self.client.post(
            "/api/notify/push/subscribe", json=sub).status_code, 200)
        # the same browser re-binding at every page load is the common
        # case and is not a new destination — the address bound still
        # stops a script looping it
        codes = [self.client.post("/api/notify/push/subscribe",
                                  json=sub).status_code
                 for _ in range(200)]
        self.assertIn(429, codes)
        self.assertEqual(codes[:50], [200] * 50)

    def test_keys_that_are_not_a_browsers_are_refused(self):
        good = browser_keys()
        bad = [{"p256dh": "AAAA", "auth": "AAAA"},
               {"p256dh": good["p256dh"], "auth": "AAAA"},
               {"p256dh": "BA" + "A" * 85, "auth": good["auth"]},
               {"p256dh": good["p256dh"][:-4], "auth": good["auth"]},
               {"p256dh": good["p256dh"] + "!", "auth": good["auth"]}]
        for keys in bad:
            r = self.client.post("/api/notify/push/subscribe",
                                 json=self._sub(keys))
            self.assertEqual(r.status_code, 422, (keys, r.text))
            self.assertIsInstance(r.json()["detail"], str)
        self.assertEqual(self._rows(), [])
        # a browser's real keys, padded or not, pass
        padded = {k: v + "=" * (-len(v) % 4) for k, v in good.items()}
        for keys in (good, padded):
            self.assertEqual(self.client.post(
                "/api/notify/push/subscribe",
                json=self._sub(keys)).status_code, 200)


class _Resp:
    def __init__(self, code):
        r = requests.models.Response()
        r.status_code = code
        r.reason = "x"
        r._content = b""
        self.r = r


class SenderTests(_Base):
    def _send(self):
        from oikonome.notify import push
        cc = tenancy.control_connect()
        try:
            return push.send_tenant(cc, str(self.tid), "t", "b")
        finally:
            cc.close()

    def test_a_subscription_whose_keys_fail_locally_is_retired(self):
        self._insert({"p256dh": "AAAA", "auth": "AAAA"})
        import pywebpush
        with mock.patch("requests.post",
                        side_effect=AssertionError("must not POST")):
            self.assertEqual(self._send(), 0)
        rows = self._rows()
        self.assertIsNotNone(rows[0]["dead_at"])
        with mock.patch.object(pywebpush, "webpush") as wp:
            self._send()
        wp.assert_not_called()

    def test_a_503_leaves_the_subscription_alive(self):
        self._insert(browser_keys())
        with mock.patch("requests.post", return_value=_Resp(503).r):
            self.assertEqual(self._send(), 0)
        self.assertIsNone(self._rows()[0]["dead_at"])

    def test_a_network_failure_leaves_the_subscription_alive(self):
        self._insert(browser_keys())
        with mock.patch("requests.post",
                        side_effect=requests.ConnectionError("down")):
            self.assertEqual(self._send(), 0)
        self.assertIsNone(self._rows()[0]["dead_at"])

    def test_a_410_still_retires_it(self):
        self._insert(browser_keys())
        with mock.patch("requests.post", return_value=_Resp(410).r):
            self._send()
        self.assertIsNotNone(self._rows()[0]["dead_at"])

    def test_one_send_pass_reads_a_bounded_number_per_person(self):
        from oikonome.notify import push
        self._insert(browser_keys(), n=push.MAX_PER_USER + 5)
        cc = tenancy.control_connect()
        try:
            self.assertEqual(len(push.live_subscriptions(cc, self.tid)),
                             push.MAX_PER_USER)
            self.assertEqual(len(push.live_subscriptions(
                cc, self.tid, self.uid)), push.MAX_PER_USER)
        finally:
            cc.close()


class DeviceDoorTests(_Base):
    def test_the_phone_push_door_is_rate_limited(self):
        client = self._login()
        minted = client.post("/api/devices", json={
            "device_name": "limit phone", "platform": "android"}).json()
        bare = TestClient(self.appmod.app)
        hdr = {"Authorization": f"Bearer {minted['token']}"}
        codes = [bare.post("/api/devices/push", headers=hdr,
                           json={"token": f"fcm-token-{i:04d}" + "x" * 20,
                                 "platform": "fcm"}).status_code
                 for i in range(40)]
        self.assertEqual(codes[0], 200)
        self.assertIn(429, codes)


if __name__ == "__main__":
    unittest.main()
