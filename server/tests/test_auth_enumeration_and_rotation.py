"""Auth regressions: invite claim fails uniformly, and passkey login options never confirm an address.

invite claim: one generic failure message, burn survives a taken email.
passkey login options: unknown email gets a decoy allow-list (no
enrollment enumeration).
doctor hides shared db/host telemetry on hosted.
SMTP connect goes to the address that passed the policy (DNS pin).
a spent approve link no longer names the requester.
passwords have a maximum length at every door.
email change revokes other sessions + script tokens; password
rotation kills pending family invites.
token revoke is step-up gated; .oikx scrypt params are clamped.
"""

import json
import os
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

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
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.appmod.app)
        self.email = f"auth-{uuid.uuid4().hex[:8]}@x.dev"
        self.client.post("/api/signup", data={
            "email": self.email, "password": PW})

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)

    def _mint_invite(self) -> str:
        r = self.client.post("/api/invites",
                             json={"label": "fam", "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["url"].rsplit("token=", 1)[1]


class ClaimFailsUniformlyTests(_Base):
    def test_taken_email_and_bad_token_read_the_same(self):
        token = self._mint_invite()
        # claim against an EXISTING email: generic message, no
        # "already exists" tell
        r = TestClient(self.appmod.app).post("/api/invite/claim", json={
            "token": token, "email": self.email, "password": "x" * 12})
        self.assertEqual(r.status_code, 400)
        self.assertNotIn("already exists", r.text)
        bad = TestClient(self.appmod.app).post("/api/invite/claim", json={
            "token": "bogus", "email": "new@x.dev", "password": "x" * 12})
        self.assertEqual(bad.json()["detail"], r.json()["detail"])
        # and the failed claim SPENT the invite — no reusable probe
        self.assertEqual(
            self.client.get(f"/api/invite/peek?token={token}").status_code,
            404)

    def test_happy_claim_still_works(self):
        token = self._mint_invite()
        r = TestClient(self.appmod.app).post("/api/invite/claim", json={
            "token": token, "email": f"fam-{uuid.uuid4().hex[:8]}@x.dev",
            "password": "family-member-pass"})
        self.assertEqual(r.status_code, 200, r.text)


class PasskeyOptionsDecoyTests(_Base):
    def test_unknown_email_gets_a_stable_decoy_allow_list(self):
        os.environ["OIKONOME_HOSTED"] = "1"   # passkeys are hosted-only
        anon = TestClient(self.appmod.app)
        a = anon.post("/api/login/passkey/options",
                      json={"email": "nobody@x.dev"}).json()
        b = anon.post("/api/login/passkey/options",
                      json={"email": "nobody@x.dev"}).json()
        ac, bc = (a["options"]["allowCredentials"],
                  b["options"]["allowCredentials"])
        # shaped like an enrollment — the SAME width a real account gets,
        # so the array length can't be read as a passkey count either
        from oikonome.auth import passkeys
        self.assertEqual(len(ac), passkeys._ALLOW_WIDTH)
        self.assertEqual([x["id"] for x in ac],
                         [x["id"] for x in bc])         # and undiffable
        c = anon.post("/api/login/passkey/options",
                      json={"email": "other@x.dev"}).json()
        self.assertEqual(set(), {x["id"] for x in ac}
                         & {x["id"] for x in c["options"]["allowCredentials"]})

    def test_no_email_keeps_discoverable_flow(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        lo = TestClient(self.appmod.app).post(
            "/api/login/passkey/options", json={}).json()
        self.assertFalse(lo["options"].get("allowCredentials"))


class DoctorHostedTelemetryTests(_Base):
    def _names(self):
        from oikonome.web import doctor
        admin = tenancy.admin_connect()
        try:
            return [r["name"] for r in doctor._system_rows(admin)]
        finally:
            admin.close()

    def test_hosted_hides_shared_telemetry(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        self.assertEqual(self._names(), ["version"])

    def test_self_host_keeps_the_rows(self):
        self.assertIn("database size", self._names())


class SmtpHostPinTests(_Base):
    def test_self_host_passthrough(self):
        from oikonome.web import netguard
        self.assertEqual(netguard.pinned_smtp_host("smtp.lan"), "smtp.lan")

    def test_hosted_pins_to_the_vetted_ip(self):
        from oikonome.web import netguard
        os.environ["OIKONOME_HOSTED"] = "1"
        with mock.patch.object(netguard, "_resolved_ips",
                               return_value=["93.184.216.34"]):
            self.assertEqual(netguard.pinned_smtp_host("smtp.example.com"),
                             "93.184.216.34")

    def test_hosted_blocks_private_resolution_and_literals(self):
        from oikonome.web import netguard
        os.environ["OIKONOME_HOSTED"] = "1"
        with mock.patch.object(netguard, "_resolved_ips",
                               return_value=["10.0.0.5"]):
            with self.assertRaises(netguard.BlockedURL):
                netguard.pinned_smtp_host("rebind.example.com")
        with self.assertRaises(netguard.BlockedURL):
            netguard.pinned_smtp_host("127.0.0.1")


class SpentApproveLinkTests(_Base):
    pass  # its tests live in the hosted twin


class PasswordCeilingTests(_Base):
    LONG = "x" * 300

    def test_signup_rejects_oversize(self):
        r = TestClient(self.appmod.app).post("/api/signup", data={
            "email": "big@x.dev", "password": self.LONG})
        self.assertEqual(r.status_code, 400)
        self.assertIn("at most", r.text)

    def test_password_change_rejects_oversize(self):
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": self.LONG})
        self.assertEqual(r.status_code, 400)
        self.assertIn("at most", r.text)

    def test_invite_claim_rejects_oversize(self):
        token = self._mint_invite()
        r = TestClient(self.appmod.app).post("/api/invite/claim", json={
            "token": token, "email": "big2@x.dev", "password": self.LONG})
        self.assertEqual(r.status_code, 400)
        self.assertIn("at most", r.text)


class CredentialRotationTests(_Base):
    def test_email_change_revokes_other_sessions_and_tokens(self):
        other = TestClient(self.appmod.app)
        other.post("/api/login", data={"email": self.email, "password": PW})
        self.assertEqual(other.get("/api/me").status_code, 200)
        minted = self.client.post(
            "/api/tokens", json={"name": "coll", "password": PW}).json()
        r = self.client.post("/api/email/change", data={
            "new_email": f"new-{uuid.uuid4().hex[:8]}@x.dev",
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        # this session survives, the other is out
        self.assertEqual(self.client.get("/api/me").status_code, 200)
        self.assertEqual(other.get("/api/me").status_code, 401)
        admin = tenancy.admin_connect()
        try:
            row = admin.execute(
                "SELECT revoked_at FROM api_tokens WHERE id = %s::uuid",
                (minted["id"],)).fetchone()
        finally:
            admin.close()
        self.assertIsNotNone(row["revoked_at"])

    def test_password_change_kills_pending_invites(self):
        token = self._mint_invite()
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": "a-fresh-passphrase"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(
            self.client.get(f"/api/invite/peek?token={token}").status_code,
            404)


class RevokeStepUpAndScryptTests(_Base):
    def test_token_revoke_needs_password(self):
        minted = self.client.post(
            "/api/tokens", json={"name": "s", "password": PW}).json()
        r = self.client.post("/api/tokens/revoke", json={"id": minted["id"]})
        self.assertEqual(r.status_code, 403)         # elevation_required
        r = self.client.post("/api/tokens/revoke",
                             json={"id": minted["id"], "password": "nope"})
        self.assertEqual(r.status_code, 401)
        r = self.client.post("/api/tokens/revoke",
                             json={"id": minted["id"], "password": PW})
        self.assertEqual(r.status_code, 200)

    def test_oikx_kdf_params_are_clamped(self):
        from oikonome.sync import connections_bundle as cb
        env = json.loads(cb._seal(b"payload", "pass"))
        # a legit envelope still opens
        self.assertEqual(
            cb._open(json.dumps(env).encode(), "pass"), b"payload")
        for tamper in ({"n": 2 ** 25}, {"n": 2 ** 15 + 1}, {"r": 1024},
                       {"p": 999}, {"n": 4}):
            bad = dict(env, **tamper)
            with self.assertRaises(ValueError):
                cb._open(json.dumps(bad).encode(), "pass")


if __name__ == "__main__":
    unittest.main()
