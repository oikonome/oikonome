"""Kicking another session or device is a posture change: on a TOTP account
the password alone must not be enough.

Signing every other session out is exactly how a thief who has stolen a
password and a live session locks the real owner out and keeps an exclusive
window. The password is the credential the second factor exists to stop
being sufficient, so these doors owe a live authenticator code as well —
the same rule passkey add/remove already carried. A per-id loop over the
session list reaches the identical outcome, so it is gated the same way,
and a mobile device token is a signed-in session by another name.

Also here: the full-data export's CSV cell must neutralize a formula that
hides behind an invisible leading character (tab, CR, LF, BOM, space). The
rule lives in one function so the two exports in this codebase cannot
disagree about it.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.auth import device_tokens, sessions, totp as totp_mod
from oikonome.db import tenancy

from .util import _ensure_db, clear_totp_burn

PASSWORD = "correct-horse-battery"


def _signup(email):
    admin = tenancy.admin_connect()
    try:
        tid = tenancy.create_tenant(admin, email)
        from oikonome.auth import passwords
        uid = admin.execute(
            "INSERT INTO users (tenant_id, email, password_hash, verified_at) "
            "VALUES (%s, %s, %s, now()) RETURNING id",
            (tid, email, passwords.hash_password(PASSWORD))).fetchone()["id"]
    finally:
        admin.close()
    return tid, uid


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
        self.email = f"srev-{uuid.uuid4().hex[:8]}@x.dev"
        self.tid, self.uid = _signup(self.email)
        self.client = self._login()

    def _login(self):
        c = TestClient(self.appmod.app)
        r = c.post("/login", data={"email": self.email, "password": PASSWORD},
                   follow_redirects=False)
        assert r.status_code == 303, r.text
        return c

    def _enroll_totp(self) -> str:
        secret = totp_mod.new_secret()
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": totp_mod.code_now(secret),
            "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        clear_totp_burn()
        return secret

    def _other_session(self) -> str:
        """A second signed-in browser (created directly: a second /login
        would need its own live code once TOTP is on, and one code is one
        code)."""
        from oikonome.web.app import _control_conn
        with _control_conn() as conn:
            sessions.create_session(conn, self.uid, self.tid,
                                    user_agent="Mozilla/5.0 other")
        rows = self.client.get("/api/sessions").json()["sessions"]
        other = [s for s in rows if not s["current"]]
        self.assertTrue(other, "expected a second session")
        return other[0]["id"]

    def _mint_device(self) -> str:
        from oikonome.web.app import _control_conn
        with _control_conn() as conn:
            minted = device_tokens.mint(conn, self.uid, self.tid,
                                        device_name="Pixel", platform="android")
        self.assertIsNotNone(minted)
        return str(minted[1]["id"])


class TotpAccountRevokeTests(_Base):
    def test_sign_out_everywhere_else_needs_live_code(self):
        secret = self._enroll_totp()
        self._other_session()               # something to kick
        r = self.client.post("/api/sessions/revoke",
                             json={"all_others": True, "password": PASSWORD})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("totp_required", r.text)
        # the other session is still alive
        rows = self.client.get("/api/sessions").json()["sessions"]
        self.assertTrue([s for s in rows if not s["current"]])
        clear_totp_burn()
        r2 = self.client.post("/api/sessions/revoke", json={
            "all_others": True, "password": PASSWORD,
            "totp_code": totp_mod.code_now(secret)})
        self.assertEqual(r2.status_code, 200, r2.text)
        rows = self.client.get("/api/sessions").json()["sessions"]
        self.assertEqual([s for s in rows if not s["current"]], [])

    def test_revoke_one_session_by_id_needs_live_code(self):
        """The per-id door reaches the same outcome as all_others in a loop,
        so a password-only path here would reopen what all_others closed."""
        secret = self._enroll_totp()
        sid = self._other_session()
        r = self.client.post("/api/sessions/revoke",
                             json={"id": sid, "password": PASSWORD})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("totp_required", r.text)
        clear_totp_burn()
        r2 = self.client.post("/api/sessions/revoke", json={
            "id": sid, "password": PASSWORD,
            "totp_code": totp_mod.code_now(secret)})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["revoked"], 1)

    def test_revoke_device_needs_live_code(self):
        secret = self._enroll_totp()
        did = self._mint_device()
        r = self.client.post("/api/devices/revoke",
                             json={"id": did, "password": PASSWORD})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("totp_required", r.text)
        clear_totp_burn()
        r2 = self.client.post("/api/devices/revoke", json={
            "id": did, "password": PASSWORD,
            "totp_code": totp_mod.code_now(secret)})
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["revoked"], 1)

    def test_wrong_code_is_refused(self):
        self._enroll_totp()
        self._other_session()
        r = self.client.post("/api/sessions/revoke", json={
            "all_others": True, "password": PASSWORD, "totp_code": "000000"})
        self.assertEqual(r.status_code, 401, r.text)


class NoTotpAccountUnchangedTests(_Base):
    """No second factor enrolled → nothing new to present. The password
    step-up these routes already had is the whole gate."""

    def test_all_others_still_password_only(self):
        self._other_session()
        r = self.client.post("/api/sessions/revoke",
                             json={"all_others": True, "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)

    def test_revoke_by_id_still_password_only(self):
        sid = self._other_session()
        r = self.client.post("/api/sessions/revoke",
                             json={"id": sid, "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)

    def test_device_revoke_still_password_only(self):
        did = self._mint_device()
        r = self.client.post("/api/devices/revoke",
                             json={"id": did, "password": PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)


class ExportCellFormulaGuardTests(unittest.TestCase):
    """The full-data export shares one formula-injection rule with the
    per-report exports; a leading invisible character must not smuggle a
    formula past it."""

    def test_invisible_lead_before_formula_is_neutralized(self):
        from oikonome.web.pages import _csv_cell
        for cell in ("\n=1+1", "﻿=1+1", "\t=1+1", "\r=1+1", " =1+1",
                     "=1+1", "+1", "-cmd", "@SUM(A1)"):
            self.assertTrue(_csv_cell(cell).startswith("'"), repr(cell))

    def test_ordinary_text_untouched(self):
        from oikonome.web.pages import _csv_cell
        self.assertEqual(_csv_cell("Bigbox Wholesale"), "Bigbox Wholesale")
        self.assertEqual(_csv_cell("2026-08-17"), "2026-08-17")
        self.assertEqual(_csv_cell("\nplain"), "\nplain")
        self.assertEqual(_csv_cell(None), None)
        self.assertEqual(_csv_cell(12.5), 12.5)

    def test_json_and_bytes_paths_still_work(self):
        """The str branch must not swallow the JSONB / BYTEA handling that
        the product→product restore depends on."""
        import base64
        import json
        from oikonome.web.pages import _csv_cell
        self.assertEqual(json.loads(_csv_cell({"a": 1})), {"a": 1})
        self.assertEqual(base64.b64decode(_csv_cell(b"\x00\x01")), b"\x00\x01")


if __name__ == "__main__":
    unittest.main()
