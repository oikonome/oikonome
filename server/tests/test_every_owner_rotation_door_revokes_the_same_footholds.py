"""Every owner rotation door revokes the same set of footholds.

An owner who notices a hijack while still signed in reaches for whichever
door the screen offers: change the password, change the login email, turn
on an authenticator, turn it off, add a first passkey. Each of those is
presented as closing the hijack, so each must take away the same things a
stolen session can have planted:

  * the other sessions;
  * script tokens;
  * unspent password-reset links (one minted to the old mailbox strips
    every factor an hour later);
  * pending family invites (a share link that needs no mailbox);
  * members who joined in the last days through this owner's invites — a
    claimed invite is a whole second account, with its own password and
    second factor, that nothing about the owner's credential reaches. They
    are removed exactly as an emailed reset removes them, named on the
    paused-settings record and in the door's answer, so the owner can
    invite back the ones that are theirs.

A door that misses one leaves the owner believing the hijack is closed
while it is not. This file drives all five doors over the same planted
set and compares what each takes away.
"""

import os
import secrets
import unittest
import uuid
from unittest import mock

import psycopg
from psycopg.rows import dict_row
from fastapi.testclient import TestClient

from oikonome.auth import invites
from oikonome.auth import reset as reset_mod
from oikonome.db import tenancy
from oikonome.notify import outbound_pause

from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"
NEW = "a-brand-new-passphrase"


def _code(secret):
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)


class _FakeReg:
    def __init__(self):
        self.credential_id = uuid.uuid4().bytes
        self.credential_public_key = b"\x05\x06\x07\x08"
        self.sign_count = 0


class EveryRotationDoorRevokesTheSameSet(unittest.TestCase):
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
        self._env = {k: os.environ.pop(k, None) for k in (
            "OIKONOME_HOSTED", "OIKONOME_RECOVERY_MEMBER_WINDOW_DAYS")}
        self.addCleanup(self._restore_env)
        self.email = f"door-{uuid.uuid4().hex[:8]}@example.dev"
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={"email": self.email,
                                                  "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.cc = psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                                  autocommit=True)
        self.addCleanup(self.cc.close)
        row = self.cc.execute("SELECT id, tenant_id FROM users WHERE email=%s",
                              (self.email,)).fetchone()
        self.uid, self.tid = row["id"], row["tenant_id"]

    def _restore_env(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---- the planted set ---------------------------------------------------

    def _plant(self) -> dict:
        """What a stolen owner session leaves behind, planted straight in
        the database so every door faces the identical set."""
        other = secrets.token_hex(32)
        self.cc.execute(
            "INSERT INTO sessions (token_hash, user_id, tenant_id, "
            "expires_at) VALUES (%s, %s, %s, now() + interval '1 day')",
            (other, self.uid, self.tid))
        token_hash = secrets.token_hex(32)
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO api_tokens (token_hash, tenant_id, created_by, "
                "name) VALUES (%s, %s, %s, 'collector')",
                (token_hash, self.tid, self.uid))
            admin.commit()
        finally:
            admin.close()
        reset_token = reset_mod.create_reset(self.cc, self.uid)
        pending = invites.create(self.cc, self.tid, self.uid, role="member",
                                 label="later@example.dev")
        member_email = f"joined-{uuid.uuid4().hex[:8]}@example.dev"
        claimed = invites.create(self.cc, self.tid, self.uid, role="member",
                                 label=member_email)["token"]
        member = invites.claim(self.cc, claimed, member_email, PW)["user_id"]
        return {"other_session": other, "token_hash": token_hash,
                "reset": reset_token, "pending_invite": pending["token"],
                "member": member, "member_email": member_email}

    def _assert_all_revoked(self, planted: dict, answer: dict, door: str):
        self.assertIsNone(self.cc.execute(
            "SELECT 1 FROM sessions WHERE token_hash=%s",
            (planted["other_session"],)).fetchone(),
            f"{door}: another session survived")
        admin = tenancy.admin_connect()
        try:
            tok = admin.execute(
                "SELECT revoked_at FROM api_tokens WHERE token_hash=%s",
                (planted["token_hash"],)).fetchone()
        finally:
            admin.close()
        self.assertIsNotNone(tok["revoked_at"],
                             f"{door}: a script token survived")
        self.assertIsNone(reset_mod.lookup_reset(self.cc, planted["reset"]),
                          f"{door}: an unspent reset link survived")
        self.assertIsNone(self.cc.execute(
            "SELECT 1 FROM invites WHERE created_by=%s AND used_at IS NULL",
            (self.uid,)).fetchone(), f"{door}: a pending invite survived")
        self.assertIsNone(self.cc.execute(
            "SELECT 1 FROM users WHERE id=%s", (planted["member"],)).fetchone(),
            f"{door}: the member the owner's invite just created survived")
        self.assertEqual(
            [m["email"] for m in answer.get("members_removed") or []],
            [planted["member_email"]],
            f"{door}: the answer does not name the removed member")
        admin = tenancy.admin_connect()
        try:
            cfg = admin.execute(
                "SELECT config FROM tenant_settings WHERE tenant_id=%s",
                (self.tid,)).fetchone()["config"]
        finally:
            admin.close()
        rec = outbound_pause.paused(cfg)
        self.assertIsNotNone(rec, f"{door}: no record for the banner")
        self.assertEqual([m["email"] for m in rec["removed_members"]],
                         [planted["member_email"]],
                         f"{door}: the banner does not name the member")
        # the owner's own browser stays signed in through every door
        self.assertEqual(self.client.get("/api/me").status_code, 200, door)

    # ---- the doors ---------------------------------------------------------

    def test_password_change(self):
        planted = self._plant()
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_all_revoked(planted, r.json(), "password change")

    def test_login_email_change(self):
        planted = self._plant()
        r = self.client.post("/api/email/change", data={
            "new_email": f"moved-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_all_revoked(planted, r.json(), "email change")

    def test_first_authenticator(self):
        planted = self._plant()
        secret = self.client.post("/api/totp/enroll",
                                  data={"password": PW}).json()["secret"]
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_all_revoked(planted, r.json(), "authenticator on")

    def test_authenticator_turned_off(self):
        secret = self.client.post("/api/totp/enroll",
                                  data={"password": PW}).json()["secret"]
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        planted = self._plant()
        r = self.client.post("/api/totp/disable", data={
            "code": _code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self._assert_all_revoked(planted, r.json(), "authenticator off")

    def test_first_passkey(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        planted = self._plant()
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
        self._assert_all_revoked(planted, r.json(), "first passkey")


class AMembersOwnRotationRemovesNobody(EveryRotationDoorRevokesTheSameSet):
    """Only an owner's rotation reaches the members its invites created; a
    member changing their own password leaves the household as it is."""

    def test_password_change(self):
        member_email = f"self-{uuid.uuid4().hex[:8]}@example.dev"
        tok = invites.create(self.cc, self.tid, self.uid, role="member",
                             label=member_email)["token"]
        invites.claim(self.cc, tok, member_email, PW)
        other_email = f"other-{uuid.uuid4().hex[:8]}@example.dev"
        tok = invites.create(self.cc, self.tid, self.uid, role="member",
                             label=other_email)["token"]
        other = invites.claim(self.cc, tok, other_email, PW)["user_id"]
        c = TestClient(self.app)
        r = c.post("/api/login", data={"email": member_email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        r = c.post("/api/password/change", json={
            "current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json().get("members_removed"), [])
        self.assertIsNotNone(self.cc.execute(
            "SELECT 1 FROM users WHERE id=%s", (other,)).fetchone())
        self.assertIsNotNone(self.cc.execute(
            "SELECT 1 FROM users WHERE id=%s", (self.uid,)).fetchone())

    test_login_email_change = None
    test_first_authenticator = None
    test_authenticator_turned_off = None
    test_first_passkey = None


if __name__ == "__main__":
    unittest.main()
