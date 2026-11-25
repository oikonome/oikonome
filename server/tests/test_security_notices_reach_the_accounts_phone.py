"""Every account-security notice reaches the account's own phone as well as
its mailbox, and the push says nothing a relay could read.

A change made from a stolen session is silent until the owner hears of it;
mail alone can sit unread for days, while a phone wakes the person now. The
push goes through the relay as a bare "security" event (no line of text):
the relay picks its fixed line and a tap opens the Security screen, while the
relay and the phone platforms learn neither the address nor any token value.
"""

import json
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from .util import _ensure_db

PW = "correct-horse-battery"


class SecurityNoticesReachThePhoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def _household(self, *, phone: bool):
        from oikonome.web import security
        security._limiter._hits.clear()
        c = TestClient(self.appmod.app)
        email = f"sec-{uuid.uuid4().hex[:8]}@example.dev"
        c.post("/api/signup", data={"email": email, "password": PW})
        c.post("/api/auth/elevate", json={"password": PW})
        tid = c.get("/api/me").json()["tenant_id"]
        if phone:
            minted = c.post("/api/devices", json={
                "device_name": "phone", "platform": "android"}).json()
            r = TestClient(self.appmod.app).post(
                "/api/devices/push",
                headers={"Authorization": f"Bearer {minted['token']}"},
                json={"token": "fcm-" + uuid.uuid4().hex, "platform": "fcm"})
            self.assertEqual(r.status_code, 200, r.text)
        return email, tid

    def _deliver(self, email, tid, change):
        posted, mails = [], []

        class _Ok:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"sent": 1}

        def post(url, json=None, **kw):
            posted.append(json)
            return _Ok()
        with mock.patch.dict(os.environ,
                             {"OIKONOME_PUSH_RELAY_URL": "https://relay.test"}), \
                mock.patch("httpx.post", post), \
                mock.patch.object(self.appmod, "_smtp_for",
                                  lambda *a: {"configured": True}), \
                mock.patch("oikonome.web.report.send",
                           lambda *a, **k: mails.append(a[3])), \
                mock.patch("oikonome.notify.sms.send", lambda *a: None):
            self.appmod._deliver_security_notice(email, tid, change)
        return posted, mails

    def test_every_security_change_mails_and_pushes_the_account(self):
        email, tid = self._household(phone=True)
        for change in self.appmod._SECURITY_CHANGES:
            with self.subTest(change=change):
                posted, mails = self._deliver(email, tid, change)
                self.assertEqual(mails, [[email]])
                self.assertEqual(len(posted), 1)
                sent = json.dumps(posted[0])
                self.assertNotIn(email, sent)
                self.assertNotIn(email.split("@")[0], sent)
                for n in posted[0]["notifications"]:
                    self.assertEqual(n["event"], "security")
                    self.assertNotIn("text", n)

    def test_an_account_with_no_phone_gets_the_mail_only(self):
        email, tid = self._household(phone=False)
        posted, mails = self._deliver(email, tid, "api_token_created")
        self.assertEqual(mails, [[email]])
        self.assertEqual(posted, [])

    def test_the_household_fallback_is_the_plain_alert(self):
        """With no phone of its own, a notice whose silence is the worse
        answer goes to the household's devices. Those readers did nothing:
        they get the plain alert, never the security event, whose line
        speaks of "your account" and whose tap opens their own Security
        screen with nothing pending on it."""
        from oikonome.notify import push_native
        email, tid = self._household(phone=False)
        events, security = [], []
        with mock.patch.object(
                push_native, "send_tenant",
                lambda conn, tenant_id, event, **kw: events.append(
                    (event, kw.get("user_id"))) or 0), \
                mock.patch.object(
                    push_native, "send_security",
                    lambda *a, **kw: security.append(kw) or 0), \
                mock.patch("oikonome.notify.push.send_tenant",
                           lambda *a, **kw: 0), \
                mock.patch("oikonome.notify.sms.send", lambda *a: None):
            self.appmod._security_fan_out(
                email, tid, "Security notice", "short",
                neutral="a security action on a household account "
                        "needs attention")
        self.assertEqual(events, [("alert", None)])
        self.assertEqual(security, [])


if __name__ == "__main__":
    unittest.main()
