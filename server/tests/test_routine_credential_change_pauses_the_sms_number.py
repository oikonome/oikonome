"""An owner's routine credential change pauses the household's SMS number.

A number verified by whoever held the session keeps receiving the daily
summary with no session at all, so a signed-in owner changing their
password, login address or second factor marks it UNVERIFIED: messages stop
until a fresh code sent to that number is typed back, and the reply says
so (`sms_number_needs_verify`) so the clients can ask the owner to confirm
it. The number itself stays, so the owner's own phone is one confirmation
from working again. A RECOVERY — an emailed reset, an operator's factor
clear (test_sms_number_is_a_guarded_destination) — still drops it outright.
"""

import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import budget

from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"
NEW = "brand-new-password-9"
NUMBER = "+12175550123"


class RoutineChangePausesSmsNumberTests(unittest.TestCase):
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
        self.client = TestClient(self.app)
        self.email = f"sms-{uuid.uuid4().hex[:8]}@example.dev"
        r = self.client.post("/api/signup",
                             data={"email": self.email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.tid = self.client.get("/api/me").json()["tenant_id"]
        self._set_phone()

    def _set_phone(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg["notify_phone"] = {"number": NUMBER, "verified": True,
                                       "consent_programs": ["summary"]}
                cfg["notify_phone_pending"] = {"number": "+12175550124"}
        finally:
            conn.close()

    def _phone(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            cfg = budget.load_config(conn)
            return cfg.get("notify_phone"), cfg.get("notify_phone_pending")
        finally:
            conn.close()

    def _paused(self, reply: dict):
        self.assertIs(reply.get("sms_number_needs_verify"), True, reply)
        phone, pending = self._phone()
        # the number and its consent stay; only the verification is gone
        self.assertEqual(phone["number"], NUMBER)
        self.assertIs(phone["verified"], False)
        self.assertEqual(phone["consent_programs"], ["summary"])
        self.assertTrue(phone.get("unverified_at"))
        # a half-finished verification's code went to the number being set
        self.assertIsNone(pending)
        # the settings page sees a number that needs confirming again
        status = self.client.get("/api/notify").json()
        self.assertTrue(status["phone"])
        self.assertIs(status["phone_verified"], False)

    def _enrol_totp(self):
        from oikonome.auth import totp as _t
        secret = self.client.post("/api/totp/enroll",
                                  data={"password": PW}).json()["secret"]
        clear_totp_burn()
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _t.code_now(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return secret, r.json()

    def test_a_password_change_pauses_it(self):
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        self._paused(r.json())

    def test_a_first_second_factor_pauses_it(self):
        _secret, reply = self._enrol_totp()
        self._paused(reply)

    def test_turning_two_factor_off_pauses_it(self):
        from oikonome.auth import totp as _t
        secret, _reply = self._enrol_totp()
        self._set_phone()
        clear_totp_burn()
        r = self.client.post("/api/totp/disable", data={
            "code": _t.code_now(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self._paused(r.json())

    def test_a_first_passkey_pauses_it(self):
        class _FakeReg:
            credential_id = uuid.uuid4().bytes
            credential_public_key = b"\x05\x06\x07\x08"
            sign_count = 0

        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            o = self.client.post("/api/passkeys/options",
                                 json={"password": PW}).json()
            cred = {"id": "AQIDBA", "response": {"transports": ["internal"]}}
            with mock.patch(
                    "oikonome.auth.passkeys.verify_registration_response",
                    return_value=_FakeReg()):
                r = self.client.post("/api/passkeys", json={
                    "challenge_id": o["challenge_id"], "credential": cred,
                    "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self._paused(r.json())

    def test_a_login_address_change_pauses_it(self):
        r = self.client.post("/api/email/change", data={
            "new_email": f"sms-new-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self._paused(r.json())

    def test_a_paused_number_gets_no_summary(self):
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        from oikonome.notify import sms as _sms
        with mock.patch.object(_sms, "configured", return_value=True), \
                mock.patch.object(_sms, "send") as send:
            r = self.client.post("/api/notify/test",
                                 json={"channel": "sms"})
        self.assertEqual(r.status_code, 400, r.text)
        send.assert_not_called()

    def test_a_rotation_with_no_verified_number_says_nothing(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            with budget.config_txn(conn) as cfg:
                cfg.pop("notify_phone", None)
        finally:
            conn.close()
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(r.json()["sms_number_needs_verify"], False)

    def test_a_members_rotation_leaves_the_owners_number(self):
        from oikonome.auth import passwords
        member = f"smsm-{uuid.uuid4().hex[:8]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "verified_at) VALUES (%s,%s,%s,'member',now())",
                (self.tid, member, passwords.hash_password(PW)))
        finally:
            admin.close()
        c = TestClient(self.app)
        r = c.post("/login", data={"email": member, "password": PW},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        r = c.post("/api/password/change", json={
            "current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(r.json()["sms_number_needs_verify"], False)
        phone, pending = self._phone()
        self.assertEqual(phone, {"number": NUMBER, "verified": True,
                                 "consent_programs": ["summary"]})
        self.assertEqual(pending, {"number": "+12175550124"})


if __name__ == "__main__":
    unittest.main()
