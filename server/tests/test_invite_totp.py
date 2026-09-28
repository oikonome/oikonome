"""Minting a family invite is a durable-principal grant — on a
TOTP account it owes a LIVE code, exactly like binding a new passkey.

Stepped up on the password only, an attacker holding a stolen session
cookie plus the password (but no authenticator) could mint an invite — the
response returns the claim URL directly, no mailbox needed — claim it at
their own address, and hold a viewer principal that survives the owner's
password change. The invite door carries the same _require_live_totp guard
as passkey_register.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"


def _fresh_code(secret):
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)


class InviteLiveTotpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.client = TestClient(cls.app)
        cls.client.post("/api/signup", data={
            "email": f"invtotp-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        from oikonome.auth import totp as totp_mod
        cls.secret = totp_mod.new_secret()
        r = cls.client.post("/api/totp/confirm", data={
            "secret": cls.secret, "code": _fresh_code(cls.secret),
            "password": PW})
        assert r.status_code == 200, r.text

    def test_password_alone_no_longer_mints_an_invite(self):
        clear_totp_burn()
        r = self.client.post("/api/invites",
                             json={"label": "fam", "password": PW})
        self.assertEqual(r.status_code, 401)
        self.assertIn("totp_required", r.json()["detail"])

    def test_wrong_code_is_refused(self):
        clear_totp_burn()
        r = self.client.post("/api/invites",
                             json={"label": "fam", "password": PW,
                                   "totp_code": "000000"})
        self.assertEqual(r.status_code, 401)

    def test_live_code_mints(self):
        r = self.client.post("/api/invites",
                             json={"label": "fam", "password": PW,
                                   "totp_code": _fresh_code(self.secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("/app/invite?token=", r.json()["url"])

    def test_account_without_totp_is_unaffected(self):
        """The guard is a no-op when TOTP isn't enrolled — _step_up (and,
        for passkey-only accounts, the passkey step-up) still governs."""
        client = TestClient(self.app)
        client.post("/api/signup", data={
            "email": f"invtotp-b-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        r = client.post("/api/invites",
                        json={"label": "fam", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)


class SupportAccessLiveTotpTests(unittest.TestCase):
    """Support consent is the same class of durable grant as an
    invite — session+password without the authenticator must not open a
    168h operator data window."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.client = TestClient(cls.app)
        cls.client.post("/api/signup", data={
            "email": f"supacc-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        from oikonome.auth import totp as totp_mod
        cls.secret = totp_mod.new_secret()
        r = cls.client.post("/api/totp/confirm", data={
            "secret": cls.secret, "code": _fresh_code(cls.secret),
            "password": PW})
        assert r.status_code == 200, r.text

    def test_password_alone_no_longer_grants_support(self):
        clear_totp_burn()
        r = self.client.post("/api/support-access",
                             json={"hours": 24, "reason": "help",
                                   "password": PW})
        self.assertEqual(r.status_code, 401)
        self.assertIn("totp_required", r.json()["detail"])

    def test_live_code_grants(self):
        r = self.client.post("/api/support-access",
                             json={"hours": 24, "reason": "help",
                                   "password": PW,
                                   "totp_code": _fresh_code(self.secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("ok"))




class ScriptTokenLiveTotpTests(unittest.TestCase):
    """A script token is the same class of durable grant again: a bearer
    push credential that no password change or reset revokes. Minting one
    on password alone let a session+password thief plant persistence that
    outlives every remediation short of finding the token list — so mint
    (and its symmetric revoke, which can silence every collector) owes the
    same live code the support-access grant demands."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.client = TestClient(cls.app)
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client.post("/api/signup", data={
            "email": f"sttotp-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        from oikonome.auth import totp as totp_mod
        cls.secret = totp_mod.new_secret()
        r = cls.client.post("/api/totp/confirm", data={
            "secret": cls.secret, "code": _fresh_code(cls.secret),
            "password": PW})
        assert r.status_code == 200, r.text

    def test_password_alone_no_longer_mints_a_token(self):
        clear_totp_burn()
        r = self.client.post("/api/tokens",
                             json={"name": "coll", "password": PW})
        self.assertEqual(r.status_code, 401)
        self.assertIn("totp_required", r.json()["detail"])

    def test_wrong_code_is_refused(self):
        clear_totp_burn()
        r = self.client.post("/api/tokens",
                             json={"name": "coll", "password": PW,
                                   "totp_code": "000000"})
        self.assertEqual(r.status_code, 401)

    def test_live_code_mints_and_revoke_owes_one_too(self):
        r = self.client.post("/api/tokens",
                             json={"name": "coll", "password": PW,
                                   "totp_code": _fresh_code(self.secret)})
        self.assertEqual(r.status_code, 200, r.text)
        tid = r.json()["id"]
        self.assertTrue(r.json().get("token"))
        clear_totp_burn()
        r = self.client.post("/api/tokens/revoke",
                             json={"id": tid, "password": PW})
        self.assertEqual(r.status_code, 401)
        self.assertIn("totp_required", r.json()["detail"])
        r = self.client.post("/api/tokens/revoke",
                             json={"id": tid, "password": PW,
                                   "totp_code": _fresh_code(self.secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("ok"))

    def test_account_without_totp_is_unaffected(self):
        """The guard is a no-op when TOTP isn't enrolled — _step_up (and,
        for passkey-only accounts, the passkey step-up) still governs."""
        client = TestClient(self.app)
        client.post("/api/signup", data={
            "email": f"sttotp-b-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        r = client.post("/api/tokens",
                        json={"name": "coll", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)

if __name__ == "__main__":
    unittest.main()


class MemberRemovalLiveTotpTests(unittest.TestCase):
    """Removing a household member is the invite door's mirror image — a
    durable change to who can see the ledger — and it owed only the
    password while minting owed a live code. A session+password thief could
    not add themselves but could evict the spouse. Same guard, both ways."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app
        cls.client = TestClient(cls.app)
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.client.post("/api/signup", data={
            "email": f"rmtotp-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        from oikonome.auth import totp as totp_mod
        cls.secret = totp_mod.new_secret()
        r = cls.client.post("/api/totp/confirm", data={
            "secret": cls.secret, "code": _fresh_code(cls.secret),
            "password": PW})
        assert r.status_code == 200, r.text

    def _member(self):
        # mint an invite (live code) and claim it from a second client
        r = self.client.post("/api/invites",
                             json={"label": "fam", "password": PW,
                                   "totp_code": _fresh_code(self.secret)})
        token = r.json()["url"].split("token=")[1]
        other = TestClient(self.app)
        r = other.post("/api/invite/claim", json={
            "token": token, "password": PW,
            "email": f"rmfam-{uuid.uuid4().hex[:8]}@example.dev"})
        assert r.status_code == 200, r.text
        users = self.client.get("/api/users").json()["users"]
        return next(u["id"] for u in users if not u["me"])

    def test_password_alone_no_longer_removes_a_member(self):
        uid = self._member()
        clear_totp_burn()
        r = self.client.request("DELETE", f"/api/users/{uid}",
                                json={"password": PW})
        self.assertEqual(r.status_code, 401)
        self.assertIn("totp_required", r.json()["detail"])
        r = self.client.request("DELETE", f"/api/users/{uid}",
                                json={"password": PW,
                                      "totp_code": _fresh_code(self.secret)})
        self.assertEqual(r.status_code, 200, r.text)
