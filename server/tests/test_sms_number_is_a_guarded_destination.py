"""The household's SMS number is an outbound destination like the mail
relay or a daily-mail recipient, so it gets the same two protections.

Setting a number is a step-up act. A stolen session cookie is not the
password; if a cookie alone could set and verify a number (the code goes to
the number being set, so the thief verifies their own phone), every daily
summary text would go to the thief from then on. Clearing a number stays
free: it only takes a destination away.

Account recovery takes the number away. A password reset or the operator's
factor clear evicts every foothold a hijacker could have planted, and a
verified number is one: it is tenant config, so evicting sessions alone
leaves the household's summaries going to whoever verified it. Only an
owner's recovery clears it — only an owner can set it, and a member's
reset must not switch off the owner's texts.
"""

import json
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import passwords
from oikonome.auth import reset as reset_mod
from oikonome.db import tenancy

from .util import _ensure_db

PW = "correct-horse-battery"
NEW = "a-brand-new-passphrase"
SMS_ENV = {"OIKONOME_TWILIO_ACCOUNT_SID": "AC1",
           "OIKONOME_TWILIO_AUTH_TOKEN": "t",
           "OIKONOME_TWILIO_FROM": "+12175550000"}
PLANTED = {"number": "+12175559999", "verified": True,
           "consent_programs": ["summary"]}


def _stored(tid) -> dict:
    admin = tenancy.admin_connect()
    try:
        row = admin.execute("SELECT config FROM tenant_settings "
                            "WHERE tenant_id=%s", (tid,)).fetchone()
    finally:
        admin.close()
    return dict(row["config"] or {}) if row else {}


def _plant_phone(tid) -> None:
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "UPDATE tenant_settings SET config = config || %s::jsonb "
            "WHERE tenant_id=%s",
            (json.dumps({"notify_phone": PLANTED,
                         "notify_phone_pending": {"number": "+12175558888"},
                         "food_monthly": 321}), tid))
        admin.commit()
    finally:
        admin.close()


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        os.environ.pop("OIKONOME_HOSTED", None)
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self.c, self.email, self.uid, self.tid = self._household()

    def _household(self):
        c = TestClient(self.app)
        email = f"smsdst-{uuid.uuid4().hex[:10]}@example.dev"
        r = c.post("/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        admin = tenancy.admin_connect()
        try:
            u = admin.execute("SELECT id, tenant_id FROM users WHERE email=%s",
                              (email,)).fetchone()
        finally:
            admin.close()
        return c, email, u["id"], u["tenant_id"]

    def _unelevate(self):
        admin = tenancy.admin_connect()
        try:
            admin.execute("UPDATE sessions SET elevated_at = NULL "
                          "WHERE user_id = %s", (self.uid,))
        finally:
            admin.close()

    def _set_number(self, **extra):
        sent: list = []
        with mock.patch.dict(os.environ, SMS_ENV), \
             mock.patch("oikonome.notify.sms.send",
                        side_effect=lambda to, body: sent.append(to) or True):
            r = self.c.post("/api/notify/phone",
                            json={"number": "+12175551234",
                                  "consent": ["summary"], **extra})
        return r, sent

    def _reset(self, uid):
        conn = tenancy.control_connect()
        try:
            token = reset_mod.create_reset(conn, uid)
        finally:
            conn.close()
        r = TestClient(self.app).post(
            "/reset", data={"token": token, "password": NEW},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)


class SettingANumberNeedsElevation(_Base):

    def test_unelevated_session_cannot_set_a_number(self):
        self._unelevate()
        r, sent = self._set_number()
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(r.json()["detail"]["error"], "elevation_required")
        self.assertEqual(sent, [], "no code may go to the thief's phone")
        self.assertNotIn("notify_phone_pending", _stored(self.tid))

    def test_elevated_session_sets_a_number(self):
        self._unelevate()
        r = self.c.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        r, sent = self._set_number()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sent, ["+12175551234"])

    def test_older_app_build_proves_with_the_password_in_the_body(self):
        self._unelevate()
        r, sent = self._set_number(password="wrong-one")
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(sent, [])
        r, sent = self._set_number(password=PW)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(sent, ["+12175551234"])

    def test_clearing_the_number_does_not_ask(self):
        _plant_phone(self.tid)
        self._unelevate()
        r = self.c.post("/api/notify/phone", json={"number": ""})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("notify_phone", _stored(self.tid))


class RecoveryTakesTheNumberAway(_Base):

    def test_password_reset_clears_the_owners_sms_number(self):
        _plant_phone(self.tid)
        self._reset(self.uid)
        cfg = _stored(self.tid)
        self.assertNotIn("notify_phone", cfg)
        self.assertNotIn("notify_phone_pending", cfg)
        # nothing else in the household's settings is touched
        self.assertEqual(cfg.get("food_monthly"), 321)

    def test_operator_factor_clear_clears_the_owners_sms_number(self):
        from oikonome.auth import factor_reset
        _plant_phone(self.tid)
        admin = tenancy.admin_connect()
        try:
            factor_reset.clear_second_factor(admin, self.uid)
            admin.commit()
        finally:
            admin.close()
        self.assertNotIn("notify_phone", _stored(self.tid))

    def test_a_members_reset_leaves_the_owners_number(self):
        _plant_phone(self.tid)
        admin = tenancy.admin_connect()
        try:
            mid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s, %s, %s, 'member', now()) "
                "RETURNING id",
                (self.tid, f"member-{uuid.uuid4().hex[:8]}@example.dev",
                 passwords.hash_password(PW))).fetchone()["id"]
            admin.commit()
        finally:
            admin.close()
        self._reset(mid)
        self.assertEqual(_stored(self.tid)["notify_phone"]["number"],
                         PLANTED["number"])

    def test_a_reset_leaves_other_households_numbers(self):
        """The operator's factor clear runs on a connection that sees
        every tenant, so the clear must name this household's row."""
        from oikonome.auth import factor_reset
        _, _, _, other_tid = self._household()
        _plant_phone(other_tid)
        admin = tenancy.admin_connect()
        try:
            factor_reset.clear_second_factor(admin, self.uid)
            admin.commit()
        finally:
            admin.close()
        self.assertEqual(_stored(other_tid)["notify_phone"]["number"],
                         PLANTED["number"])


if __name__ == "__main__":
    unittest.main()
