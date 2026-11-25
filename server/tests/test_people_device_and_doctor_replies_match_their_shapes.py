"""Every door about the signed-in person, their household's people, their
phones and the Doctor page is called and its reply matches the shape it
documents (web/schemas/household.py, devices.py, doctor.py).

The conformance hook (tests/api_conformance.py) validates each reply;
this module reaches every door itself, as the owner and, where a reply
differs by role, as a member and a viewer too, so the shapes stay checked
whatever the rest of the suite happens to call. The mail sender is stood
in for.
"""

from __future__ import annotations

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from . import api_conformance
from .util import _ensure_db, write_config

PW = "correct-horse-battery"
MEMBER_PW = "family-member-pass"


class PeopleDeviceAndDoctorShapes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.owner = TestClient(appmod.app)
        cls.email = f"people-{uuid.uuid4().hex[:8]}@example.dev"
        r = cls.owner.post("/api/signup", data={"email": cls.email,
                                                "password": PW})
        assert r.status_code == 200, r.text
        cls.tid = cls.owner.get("/api/me").json()["tenant_id"]
        conn = tenancy.tenant_connect(cls.tid)
        try:
            write_config(conn)
        finally:
            conn.close()
        cls.owner.post("/api/auth/elevate", json={"password": PW})
        cls.member = cls._join("member")
        cls.viewer = cls._join("viewer")

    @classmethod
    def _join(cls, role):
        from oikonome.web import security
        security._limiter._hits.clear()
        r = cls.owner.post("/api/invites", json={"label": role, "role": role})
        assert r.status_code == 200, r.text
        token = r.json()["url"].rsplit("token=", 1)[1]
        client = TestClient(cls.appmod.app)
        got = client.post("/api/invite/claim", json={
            "token": token, "password": MEMBER_PW,
            "email": f"{role}-{uuid.uuid4().hex[:8]}@example.dev"})
        assert got.status_code == 200, got.text
        return client

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _ok(self, client, method, path, **kw):
        before = sum(api_conformance.validated().values())
        r = client.request(method, "/api" + path, **kw)
        self.assertEqual(r.status_code, 200, f"{method} {path}: {r.text}")
        self.assertGreater(sum(api_conformance.validated().values()), before,
                           f"{method} {path}: the reply was not validated")
        return r.json()

    def test_me_and_the_roster_for_each_role(self):
        for who, client in (("owner", self.owner), ("member", self.member),
                            ("viewer", self.viewer)):
            with self.subTest(who=who):
                me = self._ok(client, "GET", "/me")
                self.assertEqual(me["role"], who)
                users = self._ok(client, "GET", "/users")["users"]
                self.assertEqual(len(users), 3)
                # the owner sees everyone's switches; anyone else only
                # their own row's
                seen = [u for u in users if u["notify"] is not None]
                self.assertEqual(len(seen), 3 if who == "owner" else 1)

    def test_me_names_a_bounced_address_and_a_broadcast(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO email_delivery_state (email, state, bounce_type,"
                " reason, suppressed) VALUES (%s, 'bouncing', 'HardBounce',"
                " 'unknown user', true) ON CONFLICT (email) DO NOTHING",
                (self.email,))
            admin.execute(
                "INSERT INTO broadcast (id, message, severity) VALUES "
                "(1, 'maintenance tonight', 'warn') ON CONFLICT (id) DO "
                "UPDATE SET message = EXCLUDED.message")
            me = self._ok(self.owner, "GET", "/me")
        finally:
            admin.execute("DELETE FROM email_delivery_state WHERE email=%s",
                          (self.email,))
            admin.execute("DELETE FROM broadcast WHERE id = 1")
            admin.close()
        self.assertEqual(me["email_delivery"]["state"], "bouncing")
        self.assertEqual(me["broadcast"]["severity"], "warn")

    def test_invites_roles_switches_and_removal(self):
        self._ok(self.owner, "POST", "/auth/elevate", json={"password": PW})
        made = self._ok(self.owner, "POST", "/invites",
                        json={"label": "cousin", "role": "viewer"})
        self.assertTrue(made["url"])
        pending = self._ok(self.owner, "GET", "/invites")["invites"]
        self.assertTrue(pending)
        self._ok(self.owner, "DELETE",
                 f"/invites/{pending[0]['token_hash']}")

        users = self._ok(self.owner, "GET", "/users")["users"]
        viewer = next(u for u in users if u["role"] == "viewer")
        member = next(u for u in users if u["role"] == "member")
        self._ok(self.owner, "POST", f"/users/{member['id']}/role",
                 json={"role": "member"})
        self._ok(self.owner, "POST", f"/users/{viewer['id']}/notify",
                 json={"push_muted": True, "email_muted": False,
                       "email_pref": {"kind": "monthly", "on": False}})

        self._ok(self.viewer, "POST", "/me/email", json={"muted": True})
        self._ok(self.viewer, "POST", "/me/email",
                 json={"kind": "yearly", "on": False})
        self._ok(self.viewer, "POST", "/me/push", json={"muted": False})

        # a fourth person, removed by the owner
        gone = self._join("viewer")
        gid = gone.get("/api/me").json()
        users = self._ok(self.owner, "GET", "/users")["users"]
        target = next(u for u in users if u["email"] == gid["email"])
        self._ok(self.owner, "DELETE", f"/users/{target['id']}")

    def test_a_member_leaves(self):
        leaver = self._join("member")
        r = leaver.post("/api/auth/elevate", json={"password": MEMBER_PW})
        self.assertEqual(r.status_code, 200, r.text)
        self._ok(leaver, "POST", "/account/leave", data={})

    def test_the_sign_in_address_moves(self):
        self._ok(self.member, "POST", "/auth/elevate",
                 json={"password": MEMBER_PW})
        me = self.member.get("/api/me").json()
        self._ok(self.member, "POST", "/email/change",
                 data={"new_email": me["email"]})
        moved = self._ok(self.member, "POST", "/email/change",
                         data={"new_email":
                               f"moved-{uuid.uuid4().hex[:8]}@example.dev"})
        self.assertIsNone(moved["pending_email"])
        self._ok(self.member, "POST", "/email/change/cancel")

    def test_a_pending_change_is_resent_on_a_hosted_instance(self):
        client = TestClient(self.appmod.app)
        email = f"hosted-{uuid.uuid4().hex[:8]}@example.dev"
        self.assertEqual(client.post("/api/signup", data={
            "email": email, "password": PW}).status_code, 200)
        client.post("/api/auth/elevate", json={"password": PW})
        new = f"hosted-new-{uuid.uuid4().hex[:8]}@example.dev"
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}), \
                mock.patch.object(self.appmod, "_send_email_change_confirm"):
            pending = self._ok(client, "POST", "/email/change",
                               data={"new_email": new})
            self.assertEqual(pending["pending_email"], new)
            resent = self._ok(client, "POST", "/email/change/resend")
            self.assertTrue(resent["resent"])
            self.assertTrue(self._ok(client, "POST", "/email/change/cancel")
                            ["cancelled"])

    def test_phones(self):
        minted = self._ok(self.owner, "POST", "/devices",
                          json={"device_name": "shape phone",
                                "platform": "android"})
        phone = TestClient(self.appmod.app)
        hdr = {"Authorization": f"Bearer {minted['token']}"}
        listed = self._ok(phone, "GET", "/devices", headers=hdr)
        self.assertTrue(listed["devices"][0]["current"])
        self._ok(phone, "POST", "/devices/widget", headers=hdr)
        self._ok(phone, "POST", "/devices/push", headers=hdr,
                 json={"token": "f" * 32, "platform": "fcm"})
        self.assertEqual(
            self._ok(phone, "GET", "/devices", headers=hdr)
            ["devices"][0]["push"], "on")
        self._ok(phone, "POST", "/devices/push", headers=hdr,
                 json={"token": None})
        self._ok(phone, "POST", "/devices/revoke", headers=hdr,
                 json={"id": minted["id"]})
        # a sign-in the phone walks away from
        self._ok(TestClient(self.appmod.app), "POST", "/devices/abandon",
                 json={"mint_ticket": "nothing-this-names"})

    def test_a_phone_trades_its_login_ticket(self):
        client = TestClient(self.appmod.app)
        r = client.post("/api/login", data={"email": self.email,
                                            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        minted = self._ok(TestClient(self.appmod.app), "POST", "/devices",
                          json={"mint_ticket": r.json()["mint_ticket"],
                                "device_name": "ticket phone",
                                "platform": "ios"})
        self.assertTrue(minted["token"])

    def test_the_doctor_page_and_the_month_verdict(self):
        self._ok(self.owner, "GET", "/doctor")
        bundle = self._ok(self.owner, "GET", "/doctor/bundle")
        self.assertTrue(bundle["checks"])
        conn = tenancy.tenant_connect(self.tid)
        try:
            conn.execute(
                "INSERT INTO script_heartbeats (tenant_id, source, label, "
                "last_rows, via, kind) VALUES (%s, 'shape-collector', "
                "'Collector', 4, 'token', 'push')", (self.tid,))
        finally:
            conn.close()
        rows = self._ok(self.owner, "GET", "/doctor/scripts")["scripts"]
        self.assertEqual(rows[0]["status"], "off")
        self._ok(self.owner, "POST", "/doctor/scripts/shape-collector",
                 json={"expected_hours": 24, "alerts": True})
        self.assertEqual(
            self._ok(self.owner, "GET", "/doctor/scripts")
            ["scripts"][0]["status"], "fresh")
        self._ok(self.owner, "GET", "/today")


class AnErasureAnswersItsShape(unittest.TestCase):
    """Its own household, since the door erases it."""

    def test_the_owner_erases_the_account(self):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        client = TestClient(appmod.app)
        email = f"erase-{uuid.uuid4().hex[:8]}@example.dev"
        self.assertEqual(client.post("/api/signup", data={
            "email": email, "password": PW}).status_code, 200)
        client.post("/api/auth/elevate", json={"password": PW})
        before = sum(api_conformance.validated().values())
        r = client.post("/api/account/delete", data={})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(sum(api_conformance.validated().values()), before)


if __name__ == "__main__":
    unittest.main()
