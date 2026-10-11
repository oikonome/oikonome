"""A first passkey and the rotation it triggers land together or not at
all.

Adding the account's first strong factor is the moment an owner believes
a hijack is closed, so the same request evicts every other session and
bearer, removes the members the owner's recent invites created, and mints
the recovery codes that keep a lost key from being a lockout. If the key
were committed first and the rest ran afterwards, a failure half way (a
lock conflict in the member removal, say) would answer 500 with the key
already bound: a retry then sees a passkey on the account, treats the new
one as a second key, and neither mints the codes nor evicts anyone ever
again. The planted member outlives the rotation and the only strong
factor has no recovery fallback.

So the key, the eviction and the removal are one transaction, and the
eviction takes the household's settings row (through the outbound pause)
before the household-member lock, the order every rotation door keeps.
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

from .util import _ensure_db

PW = "correct-horse-battery"


class _FakeReg:
    def __init__(self):
        self.credential_id = uuid.uuid4().bytes
        self.credential_public_key = b"\x05\x06\x07\x08"
        self.sign_count = 0


class FirstPasskeyRotationIsAllOrNothing(unittest.TestCase):
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
        self._env = {k: os.environ.get(k) for k in (
            "OIKONOME_HOSTED", "OIKONOME_RECOVERY_MEMBER_WINDOW_DAYS")}
        os.environ.pop("OIKONOME_RECOVERY_MEMBER_WINDOW_DAYS", None)
        os.environ.pop("OIKONOME_HOSTED", None)
        self.addCleanup(self._restore_env)
        self.email = f"pk-{uuid.uuid4().hex[:8]}@example.dev"
        self.client = TestClient(self.app, raise_server_exceptions=False)
        r = self.client.post("/api/signup", data={"email": self.email,
                                                  "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.cc = psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                                  autocommit=True)
        self.addCleanup(self.cc.close)
        row = self.cc.execute("SELECT id, tenant_id FROM users WHERE email=%s",
                              (self.email,)).fetchone()
        self.uid, self.tid = row["id"], row["tenant_id"]
        self.other = secrets.token_hex(32)
        self.cc.execute(
            "INSERT INTO sessions (token_hash, user_id, tenant_id, "
            "expires_at) VALUES (%s, %s, %s, now() + interval '1 day')",
            (self.other, self.uid, self.tid))
        self.member_email = f"joined-{uuid.uuid4().hex[:8]}@example.dev"
        tok = invites.create(self.cc, self.tid, self.uid, role="member",
                             label=self.member_email)["token"]
        self.member = invites.claim(self.cc, tok, self.member_email,
                                    PW)["user_id"]
        # passkeys are a hosted feature; the account above was opened on
        # a self-hosted signup, which needs no invitation
        os.environ["OIKONOME_HOSTED"] = "1"

    def _restore_env(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _add_passkey(self):
        o = self.client.post("/api/passkeys/options",
                             json={"password": PW}).json()
        cred = {"id": "AQIDBA", "response": {"transports": ["internal"]}}
        with mock.patch(
                "oikonome.auth.passkeys.verify_registration_response",
                return_value=_FakeReg()):
            return self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"], "credential": cred,
                "password": PW})

    def _count(self, sql, *args) -> int:
        admin = tenancy.admin_connect()
        try:
            return admin.execute(sql, args).fetchone()["n"]
        finally:
            admin.close()

    def test_a_failed_member_removal_binds_no_key_and_a_retry_completes(self):
        """The member removal fails on the first try: the answer is an
        error and nothing happened — no key, the other session alive, the
        member still there, no codes. The retry is then a first passkey
        again, and does the whole rotation: codes minted, other session
        gone, member removed."""
        real = reset_mod.evict_invite_footholds

        def fails(*a, **kw):
            raise RuntimeError("the member removal could not finish")

        with mock.patch.object(reset_mod, "evict_invite_footholds", fails):
            r = self._add_passkey()
        self.assertEqual(r.status_code, 500, r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM passkeys WHERE user_id=%s", self.uid), 0,
            "the key was bound though its rotation failed")
        self.assertIsNotNone(self.cc.execute(
            "SELECT 1 FROM sessions WHERE token_hash=%s",
            (self.other,)).fetchone(),
            "half a rotation ran: the other session went without the key")
        self.assertIsNotNone(self.cc.execute(
            "SELECT 1 FROM users WHERE id=%s", (self.member,)).fetchone())

        self.assertIs(reset_mod.evict_invite_footholds, real)
        r = self._add_passkey()
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body.get("recovery_codes"),
                        "the retried first passkey minted no recovery codes")
        self.assertEqual([m["email"] for m in body["members_removed"]],
                         [self.member_email])
        self.assertIsNone(self.cc.execute(
            "SELECT 1 FROM sessions WHERE token_hash=%s",
            (self.other,)).fetchone())
        self.assertIsNone(self.cc.execute(
            "SELECT 1 FROM users WHERE id=%s", (self.member,)).fetchone())
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM recovery_codes WHERE user_id=%s",
            self.uid), len(body["recovery_codes"]))

    def test_a_refused_first_key_still_spends_its_challenge(self):
        """The bind shares a transaction with the rotation, but a key the
        verifier refuses must still burn its single-use challenge: the
        refusal is a 400, nothing else changes, and the same challenge is
        then worthless even with a response that would verify."""
        o = self.client.post("/api/passkeys/options",
                             json={"password": PW}).json()
        cred = {"id": "AQIDBA", "response": {"transports": ["internal"]}}
        with mock.patch(
                "oikonome.auth.passkeys.verify_registration_response",
                side_effect=RuntimeError("bad attestation")):
            r = self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"], "credential": cred,
                "password": PW})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNotNone(self.cc.execute(
            "SELECT 1 FROM sessions WHERE token_hash=%s",
            (self.other,)).fetchone())
        with mock.patch(
                "oikonome.auth.passkeys.verify_registration_response",
                return_value=_FakeReg()):
            r = self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"], "credential": cred,
                "password": PW})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(self._count(
            "SELECT count(*) AS n FROM passkeys WHERE user_id=%s", self.uid), 0)

    def test_the_member_removal_runs_with_the_settings_row_already_held(self):
        """When the first passkey's rotation reaches the household-member
        lock, the settings row is already held by that same rotation — so
        a door holding the settings row and waiting on the member lock (a
        reclaim, another rotation) can never be waited on in reverse."""
        real = reset_mod.evict_invite_footholds
        seen: dict = {}

        def probe(conn, user_id, *a, **kw):
            other = tenancy.admin_connect()
            try:
                with other.transaction():
                    other.execute(
                        "SELECT 1 FROM tenant_settings WHERE tenant_id=%s "
                        "FOR UPDATE NOWAIT", (self.tid,))
                seen["held"] = False
            except psycopg.errors.LockNotAvailable:
                seen["held"] = True
            finally:
                other.close()
            return real(conn, user_id, *a, **kw)

        with mock.patch.object(reset_mod, "evict_invite_footholds", probe):
            r = self._add_passkey()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(seen.get("held"),
                        "the member lock was reached with the settings row "
                        "already let go")


if __name__ == "__main__":
    unittest.main()
