"""A person's own switch for the household's push notices, and the owner's
view of every member's notices.

Email has long had a per-person switch (the address in the household's
mute list, and which kinds reach it); push had none, so the only way for
one member to stop the daily verdict reaching their phone was to
unregister it. The switch is per login: the person flips it through the
self door every role holds, the owner flips anyone's from the roster, and
both write the same column — so the roster and the person's own screen
can never disagree. The household fan-outs (browsers and phones) skip a
muted person; a notice addressed to that person alone still reaches them.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import device_tokens
from oikonome.db import tenancy
from oikonome.engine import budget
from oikonome.notify import push

from .util import _ensure_db

PW = "correct-horse-battery"
MEMBER_PW = "family-member-pass"


class HouseholdPushSwitchTests(unittest.TestCase):
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
        self.owner = TestClient(self.app)
        self.owner_email = f"own-{uuid.uuid4().hex[:8]}@example.dev"
        self.owner.post("/api/signup", data={"email": self.owner_email,
                                             "password": PW})
        self.owner.post("/api/auth/elevate", json={"password": PW})
        self.tid = self.owner.get("/api/me").json()["tenant_id"]
        self.viewer, self.viewer_email = self._claim("viewer")
        users = self.owner.get("/api/users").json()["users"]
        self.viewer_id = next(u["id"] for u in users
                              if u["email"] == self.viewer_email)
        self.owner_id = next(u["id"] for u in users
                             if u["email"] == self.owner_email)

    def _claim(self, role):
        r = self.owner.post("/api/invites", json={
            "label": "partner", "role": role, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        token = r.json()["url"].rsplit("token=", 1)[1]
        c = TestClient(self.app)
        email = f"{role}-{uuid.uuid4().hex[:8]}@example.dev"
        got = c.post("/api/invite/claim", json={
            "token": token, "email": email, "password": MEMBER_PW})
        self.assertEqual(got.status_code, 200, got.text)
        return c, email

    def _phone(self, client, name):
        minted = client.post("/api/devices", json={
            "device_name": name, "platform": "android"}).json()
        bare = TestClient(self.app)
        r = bare.post("/api/devices/push",
                      headers={"Authorization": f"Bearer {minted['token']}"},
                      json={"token": f"fcm-{name}-" + uuid.uuid4().hex,
                            "platform": "fcm"})
        self.assertEqual(r.status_code, 200, r.text)
        return r

    def _browser(self, user_id):
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, endpoint,"
                " p256dh, auth) VALUES (%s, %s, %s, 'k', 'a')",
                (user_id, self.tid,
                 f"https://fcm.googleapis.com/fcm/send/{uuid.uuid4().hex}"))
        finally:
            admin.close()

    def _roster_notify(self, client, uid):
        users = client.get("/api/users").json()["users"]
        return next(u for u in users if u["id"] == uid)["notify"]

    def _cfg(self):
        tc = tenancy.tenant_connect(self.tid)
        try:
            return budget.load_config(tc)
        finally:
            tc.close()

    def test_a_viewer_turns_household_push_off_and_the_fan_outs_skip_them(self):
        self._phone(self.viewer, "viewer-phone")
        self._phone(self.owner, "owner-phone")
        self._browser(self.viewer_id)
        self.assertFalse(self.viewer.get("/api/me").json()["push_muted"])
        r = self.viewer.post("/api/me/push", json={"muted": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(self.viewer.get("/api/me").json()["push_muted"])
        # the owner sees it on the roster, with the devices the switch governs
        n = self._roster_notify(self.owner, self.viewer_id)
        self.assertTrue(n["push_muted"])
        self.assertEqual((n["push_phones"], n["push_browsers"]), (1, 1))
        cc = tenancy.control_connect()
        try:
            household = {t["push_token"] for t in
                         device_tokens.push_targets(cc, self.tid)}
            self.assertTrue(any(t.startswith("fcm-owner-phone") for t in household))
            self.assertFalse(any(t.startswith("fcm-viewer-phone") for t in household))
            # named, their own phone is still reached (an account notice)
            own = device_tokens.push_targets(cc, self.tid, self.viewer_id)
            self.assertEqual(len(own), 1)
            self.assertEqual(push.live_subscriptions(cc, self.tid), [])
            self.assertEqual(len(push.live_subscriptions(
                cc, self.tid, self.viewer_id)), 1)
            # unmuted, the browser is a household target again
            self.viewer.post("/api/me/push", json={"muted": False})
            self.assertEqual(len(push.live_subscriptions(cc, self.tid)), 1)
        finally:
            cc.close()

    def test_the_owner_sets_a_members_switches_and_the_member_sees_them(self):
        r = self.owner.post(f"/api/users/{self.viewer_id}/notify", json={
            "push_muted": True, "email_muted": True,
            "email_pref": {"kind": "monthly", "on": False}})
        self.assertEqual(r.status_code, 200, r.text)
        n = r.json()["notify"]
        self.assertTrue(n["push_muted"])
        self.assertTrue(n["email_muted"])
        self.assertFalse(n["email_prefs"]["monthly"])
        self.assertTrue(n["email_prefs"]["daily"])
        self.assertIn(self.viewer_email, self._cfg()["email_muted"])
        me = self.viewer.get("/api/me").json()
        self.assertTrue(me["push_muted"])
        self.assertTrue(me["daily_email"]["muted"])
        self.assertFalse(me["daily_email"]["prefs"]["monthly"])
        # and the member's own change comes straight back to the roster
        self.viewer.post("/api/me/email", json={"muted": False})
        self.viewer.post("/api/me/push", json={"muted": False})
        n = self._roster_notify(self.owner, self.viewer_id)
        self.assertFalse(n["email_muted"])
        self.assertFalse(n["push_muted"])
        self.assertFalse(n["email_prefs"]["monthly"])

    def test_only_the_owner_sets_another_persons_switches(self):
        r = self.viewer.post(f"/api/users/{self.owner_id}/notify",
                             json={"push_muted": True})
        self.assertEqual(r.status_code, 403, r.text)
        member, _ = self._claim("member")
        r = member.post(f"/api/users/{self.owner_id}/notify",
                        json={"push_muted": True})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertFalse(self.owner.get("/api/me").json()["push_muted"])
        # a viewer's roster shows their own switches and nobody else's
        self.assertIsNotNone(self._roster_notify(self.viewer, self.viewer_id))
        self.assertIsNone(self._roster_notify(self.viewer, self.owner_id))

    def test_a_viewers_settings_carry_no_other_members_mail_switches(self):
        member, member_email = self._claim("member")
        member_id = next(u["id"] for u in
                         self.owner.get("/api/users").json()["users"]
                         if u["email"] == member_email)
        r = self.owner.post(f"/api/users/{member_id}/notify", json={
            "email_muted": True,
            "email_pref": {"kind": "monthly", "on": False}})
        self.assertEqual(r.status_code, 200, r.text)
        self.viewer.post("/api/me/email", json={"muted": True})
        # the owner reads everyone's switches
        st = self.owner.get("/api/settings").json()
        self.assertIn(member_email, st["email_muted"])
        self.assertIn(member_email,
                      {x["email"] for x in st["email_recipient_status"]})
        # a viewer, and a member, read their own and nobody else's
        for client, mine in ((self.viewer, self.viewer_email),
                             (member, member_email)):
            st = client.get("/api/settings").json()
            self.assertTrue(all(str(m).lower() == mine
                                for m in st.get("email_muted") or []),
                            st.get("email_muted"))
            rows = st.get("email_recipient_status") or []
            self.assertTrue(all(x["email"] == mine for x in rows), rows)
        st = self.viewer.get("/api/settings").json()
        self.assertEqual(st["email_muted"], [self.viewer_email])

    def test_the_doors_refuse_a_shapeless_body(self):
        r = self.viewer.post("/api/me/push", json={"muted": "yes"})
        self.assertEqual(r.status_code, 400)
        r = self.owner.post(f"/api/users/{self.viewer_id}/notify", json={})
        self.assertEqual(r.status_code, 400)
        r = self.owner.post(f"/api/users/{uuid.uuid4()}/notify",
                            json={"push_muted": True})
        self.assertEqual(r.status_code, 404)


    # ---- the switch is about the household's notices, nothing else -------

    def _owner_phone_token(self):
        self._phone(self.owner, "owner-phone")
        admin = tenancy.admin_connect()
        try:
            return {r["push_token"] for r in admin.execute(
                "SELECT push_token FROM device_tokens WHERE user_id=%s "
                "AND push_token IS NOT NULL", (self.owner_id,)).fetchall()}
        finally:
            admin.close()

    def _relay(self):
        posted = []

        class _Ok:
            status_code = 200
            text = ""

            @staticmethod
            def json():
                return {"sent": 1}

        def post(url, json=None, **kw):
            posted.extend(n["token"] for n in json["notifications"])
            return _Ok()
        return posted, post

    def test_a_security_warning_reaches_people_who_turned_household_push_off(
            self):
        """The household fallback for a factor reset (the account under
        recovery has no phone of its own) must reach every member's devices,
        muted or not: the mute is about the daily verdict, alerts and
        reports, and a silenced warning lets the reset land unnoticed."""
        import oikonome.web.app as appmod
        from unittest import mock
        tokens = self._owner_phone_token()
        self._browser(self.owner_id)
        self.owner.post("/api/me/push", json={"muted": True})
        posted, post = self._relay()
        reached_browsers = []

        def web(cc, tid, title, body, url="/app/", *, user_id=None,
                connect=None, **_kw):
            with push.db_handle(cc, connect) as c:
                reached_browsers.extend(
                    s["endpoint"] for s in
                    push.live_subscriptions(c, tid, user_id))
            return 0
        with mock.patch.dict(os.environ,
                             {"OIKONOME_PUSH_RELAY_URL": "https://relay.test"}), \
                mock.patch("httpx.post", post), \
                mock.patch("oikonome.notify.push.send_tenant", web), \
                mock.patch("oikonome.notify.sms.send", lambda *a: None):
            appmod._security_fan_out(
                self.viewer_email, self.tid, "Account recovery requested",
                "short", neutral="a security action needs attention")
        self.assertEqual(set(posted), tokens)
        self.assertEqual(len(reached_browsers), 1)

    def test_a_muted_owners_test_push_reaches_their_own_phone(self):
        """Send-test-push checks the caller's own devices; the household
        mute must not turn it into a false 'no live push subscription', and
        the phone count must still include the caller's phones."""
        from unittest import mock
        tokens = self._owner_phone_token()
        self.owner.post("/api/me/push", json={"muted": True})
        posted, post = self._relay()
        with mock.patch.dict(os.environ,
                             {"OIKONOME_PUSH_RELAY_URL": "https://relay.test"}), \
                mock.patch("httpx.post", post):
            r = self.owner.post("/api/notify/test", json={"channel": "push"})
            st = self.owner.get("/api/notify").json()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(set(posted), tokens)
        self.assertEqual(st["native_push_devices"], 1)

    def test_a_refused_change_writes_nothing(self):
        """One request, one answer: a body whose email half is refused must
        not leave its push half saved."""
        for bad in ({"push_muted": True, "email_muted": "yes"},
                    {"push_muted": True,
                     "email_pref": {"kind": "weekly-ish", "on": True}},
                    {"push_muted": True, "email_pref": "off"}):
            r = self.owner.post(f"/api/users/{self.viewer_id}/notify",
                                json=bad)
            self.assertEqual(r.status_code, 400, (bad, r.text))
            self.assertFalse(
                self._roster_notify(self.owner, self.viewer_id)["push_muted"],
                bad)

    def test_the_roster_says_on_the_mail_exactly_when_the_sender_would_mail(
            self):
        """The roster's 'on the mail' is the scheduled sender's own list
        (before the person's mute, which the roster shows beside it): an
        address configured but never accepted is not on the mail, and a
        household with no list mails whom the sender would mail."""
        from oikonome.jobs import worker

        def sender():
            tc = tenancy.tenant_connect(self.tid)
            try:
                return {e.lower() for e in
                        worker._recipient_lists(tc, self.tid)[0]}
            finally:
                tc.close()

        def check():
            got = sender()
            muted = {str(m).lower() for m in
                     (self._cfg().get("email_muted") or [])}
            for uid, email in ((self.owner_id, self.owner_email),
                               (self.viewer_id, self.viewer_email)):
                n = self._roster_notify(self.owner, uid)
                if email in muted:
                    continue
                self.assertEqual(n["on_mail"], email in got, email)

        check()                             # no list configured
        tc = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(tc)
            cfg["email_recipients"] = [self.viewer_email]
            budget.save_config(tc, cfg)
        finally:
            tc.close()
        # configured, never accepted: the sender skips it, so must the roster
        self.assertNotIn(self.viewer_email, sender())
        self.assertFalse(
            self._roster_notify(self.owner, self.viewer_id)["on_mail"])
        # ...but they ARE on the household's list: the roster says "listed,
        # not receiving yet" and keeps their switches, never "add them"
        self.assertTrue(
            self._roster_notify(self.owner, self.viewer_id)["mail_listed"])
        # the owner is on the list by construction
        self.assertTrue(
            self._roster_notify(self.owner, self.owner_id)["mail_listed"])
        # an older stored shape still names the person as listed
        tc = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(tc)
            cfg["email_recipients"] = [f"Partner <{self.viewer_email}>"]
            budget.save_config(tc, cfg)
        finally:
            tc.close()
        self.assertTrue(
            self._roster_notify(self.owner, self.viewer_id)["mail_listed"])
        check()


if __name__ == "__main__":
    unittest.main()
