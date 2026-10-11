"""The warning that a factor-clearing reset was requested reaches the
account under recovery out of band, and tells nobody else about their
"authenticator being removed".

Only that account can cancel the request, and the email may sit in the very
inbox an attacker holds, so the push channels carry it to that account's OWN
browsers and devices, whoever else in the household is subscribed. An
account with no push destination of its own must not be left with silence:
the household's devices then get a neutral notice instead. The household's
SMS number was verified by an owner for their own phone, so it carries the
full notice only for the sole owner, and a neutral one otherwise. The email
to the account itself always goes.
"""

import datetime as dt
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import device_tokens
from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db

PW = "correct-horse-battery"


class FactorResetNoticeReachesOnlyItsAccount(unittest.TestCase):
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
        c = TestClient(self.appmod.app)
        self.owner = f"fro-{uuid.uuid4().hex[:8]}@example.dev"
        r = c.post("/api/signup", data={"email": self.owner, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = c.get("/api/me").json()["tenant_id"]
        self.member = f"frm-{uuid.uuid4().hex[:8]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            self.owner_id = admin.execute(
                "SELECT id FROM users WHERE email=%s",
                (self.owner,)).fetchone()["id"]
            self.member_id = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,'h','member',now()) RETURNING id",
                (self.tid, self.member)).fetchone()["id"]
        finally:
            admin.close()
        tc = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(tc)
            cfg["notify_phone"] = {"number": "+12175551234", "verified": True}
            budget.save_config(tc, cfg)
        finally:
            tc.close()

    def _subscribe(self, user_id):
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth) VALUES (%s,%s,%s,'p','a')",
                (user_id, self.tid,
                 f"https://push.example.com/{uuid.uuid4().hex}"))
        finally:
            admin.close()

    def _phone(self, user_id):
        admin = tenancy.admin_connect()
        try:
            _tok, row = device_tokens.mint(admin, user_id, self.tid,
                                           "phone", "android")
            device_tokens.set_push(admin, user_id, row["id"],
                                   uuid.uuid4().hex, "android")
        finally:
            admin.close()

    def _notify(self, email):
        sent = {"web": [], "native": [], "sms": []}

        def web(cc, tid, title, body, url="/app/", *, user_id=None, **_kw):
            sent["web"].append((user_id, body))

        def native(cc, tid, event, *a, user_id=None, **kw):
            sent["native"].append(user_id)

        def sms(number, text):
            sent["sms"].append(text)
        with mock.patch("oikonome.notify.push.send_tenant", web), \
                mock.patch("oikonome.notify.push_native.send_tenant",
                           native), \
                mock.patch("oikonome.notify.sms.send", sms), \
                mock.patch.object(self.appmod, "_smtp_for",
                                  return_value=None):
            self.appmod._deliver_factor_reset_notice(
                email, self.tid,
                dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc), "tok")
        return sent

    def test_a_members_notice_goes_to_their_own_push_in_a_shared_household(
            self):
        self._subscribe(self.owner_id)
        self._phone(self.owner_id)
        self._subscribe(self.member_id)
        self._phone(self.member_id)
        sent = self._notify(self.member)
        self.assertEqual([u for u, _ in sent["web"]], [self.member_id])
        self.assertIn("authenticator", sent["web"][0][1])
        self.assertEqual(sent["native"], [self.member_id])
        # the owner's phone gets a line that names no one's factors
        self.assertEqual(len(sent["sms"]), 1)
        self.assertNotIn("authenticator", sent["sms"][0])

    def test_an_account_with_no_push_of_its_own_falls_back_to_a_neutral_one(
            self):
        self._subscribe(self.owner_id)
        self._phone(self.owner_id)
        sent = self._notify(self.member)
        self.assertEqual([u for u, _ in sent["web"]], [None])
        self.assertNotIn("authenticator", sent["web"][0][1])
        self.assertEqual(sent["native"], [None])

    def test_the_sole_owners_notice_uses_every_channel(self):
        self._subscribe(self.owner_id)
        self._phone(self.owner_id)
        sent = self._notify(self.owner)
        self.assertEqual([u for u, _ in sent["web"]], [self.owner_id])
        self.assertEqual(sent["native"], [self.owner_id])
        self.assertEqual(len(sent["sms"]), 1)
        self.assertIn("authenticator", sent["sms"][0])

    def test_with_two_owners_the_household_number_gets_a_neutral_sms(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,'h','owner',now())",
                (self.tid, f"fro2-{uuid.uuid4().hex[:8]}@example.dev"))
        finally:
            admin.close()
        sms = self._notify(self.owner)["sms"]
        self.assertEqual(len(sms), 1)
        self.assertNotIn("authenticator", sms[0])

    def test_the_relay_is_handed_only_that_members_device(self):
        """End to end through the native sender: two members with phones,
        a recovery request for one, and the relay sees that one's token."""
        self._phone(self.owner_id)
        self._phone(self.member_id)
        admin = tenancy.admin_connect()
        try:
            mine = {r["push_token"] for r in admin.execute(
                "SELECT push_token FROM device_tokens WHERE user_id=%s",
                (self.member_id,)).fetchall()}
        finally:
            admin.close()
        posted = []

        class _Ok:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"sent": 1}

        def post(url, json=None, **kw):
            posted.append({n["token"] for n in json["notifications"]})
            return _Ok()
        with mock.patch.dict(os.environ,
                             {"OIKONOME_PUSH_RELAY_URL": "https://relay.test"}), \
                mock.patch("httpx.post", post), \
                mock.patch("oikonome.notify.push.send_tenant",
                           lambda *a, **kw: 0), \
                mock.patch("oikonome.notify.sms.send", lambda *a: None), \
                mock.patch.object(self.appmod, "_smtp_for",
                                  return_value=None):
            self.appmod._deliver_factor_reset_notice(
                self.member, self.tid,
                dt.datetime(2026, 10, 9, tzinfo=dt.timezone.utc), "tok")
        self.assertEqual(posted, [mine])


if __name__ == "__main__":
    unittest.main()
