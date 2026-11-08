"""Every credential-posture door, against every foothold.

A door that changes how an account signs in takes away what a stolen
session or a leaked mailbox could have planted — and nothing its purpose
does not justify. Which door takes what is one table, the level each door
runs in auth/posture.LEVELS; this file is that table's acceptance test.
One fixture plants every foothold (plus the caller's own session and push
subscription), each door runs once, and the closed set must be exactly its
level's: a foothold left standing is a door that forgot one, a foothold
taken that the level does not name is a door that overshoots (a routine
new sheet that kills the collectors). A level change shows as a whole red
row, and EXPECTED below is the one place to change.

The recovery sheet is the one column a door decides within its level: a
new sheet is minted where a client asks for one (or a first factor needs
one), so it is spelled per row.

The last classes keep the three races the table cannot show: a first
passkey's sheet is staged with the key, a reissue racing a reclaim's
move-out never lands on the moved account, and no foothold is closed by
code outside auth/posture.
"""

import ast
import os
import re
import secrets
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import (device_tokens, email_revert, factor_reset,
                           invites, posture, recovery, sessions,
                           signup_reclaim)
from oikonome.auth import reset as reset_mod
from oikonome.db import crypto, tenancy
from oikonome.engine import budget
from oikonome.notify import outbound_pause

from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"
NEW = "a-brand-new-passphrase"
P = posture

# which posture foothold each planted row is
FOOTHOLD = {
    "session": P.SESSIONS, "elevation": P.SESSIONS, "device": P.DEVICES,
    "ticket": P.MINT_TICKETS, "push": P.PUSH, "reset_link": P.RECOVERIES,
    "factor_reset": P.RECOVERIES, "token": P.SCRIPT_TOKENS,
    "webhook": P.WEBHOOKS, "outbound": P.OUTBOUND, "sms": P.SMS,
    "invite": P.INVITES, "member": P.INVITES, "support": P.SUPPORT,
}

# door -> (level, sheet closed, footholds closed beyond the level)
EXPECTED = {
    "password_change":            (P.CREDENTIAL, True, set()),
    "password_change_reissue":    (P.CREDENTIAL, True, set()),
    "password_change_keeps_sheet": (P.CREDENTIAL, False, set()),
    "email_change":               (P.CREDENTIAL, True, set()),
    "email_change_reissue":       (P.CREDENTIAL, True, set()),
    "totp_on":                    (P.FACTOR, True, set()),
    "totp_off_reissue":           (P.FACTOR, True, set()),
    "recovery_regenerate":        (P.SHEET, True, set()),
    "first_passkey":              (P.FACTOR, True, set()),
    "passkey_delete_reissue":     (P.FACTOR, True, set()),
    # without the field nobody is signed out: the old sheet stays valid,
    # so a sweep would close nothing. Key-scoped only: the stamp naming
    # the key goes.
    "passkey_delete":             (None, False, {"elevation"}),
    "reset":                      (P.RECOVERY, True, set()),
    "clear_second_factor":        (P.RECOVERY, True, set()),
    "email_revert":               (P.REVERT, False, set()),
    "move_out":                   (P.MOVE_OUT, True, set()),
}

# footholds only an owner has (the household's settings, the members its
# invites created); a member subject has none of them
OWNER_ONLY = {"outbound", "sms", "member"}


def _code(secret):
    from oikonome.auth import totp as _t
    clear_totp_burn()
    return _t.code_now(secret)


class _FakeReg:
    def __init__(self):
        self.credential_id = uuid.uuid4().bytes
        self.credential_public_key = b"\x05\x06\x07\x08"
        self.sign_count = 0


def _admin():
    return tenancy.admin_connect()


class EveryDoorClosesExactlyItsLevel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self._env = {k: os.environ.pop(k, None) for k in (
            "OIKONOME_HOSTED", "OIKONOME_RECOVERY_MEMBER_WINDOW_DAYS",
            "OIKONOME_OPEN_SIGNUP")}
        self.addCleanup(self._restore_env)
        self.email = f"door-{uuid.uuid4().hex[:8]}@example.dev"
        self.client = TestClient(self.app)
        r = self.client.post("/api/signup", data={"email": self.email,
                                                  "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.cc = tenancy.control_connect()
        self.addCleanup(self.cc.close)
        row = self.cc.execute("SELECT id, tenant_id FROM users WHERE email=%s",
                              (self.email,)).fetchone()
        self.uid, self.tid = row["id"], row["tenant_id"]
        self.secret = None
        self.pk = None

    def _restore_env(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # ---- account states the doors need, set up BEFORE the planting ------

    def _totp(self):
        secret = self.client.post("/api/totp/enroll",
                                  data={"password": PW}).json()["secret"]
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self.secret = secret

    def _passkey(self, **proof):
        os.environ["OIKONOME_HOSTED"] = "1"
        o = self.client.post("/api/passkeys/options",
                             json={"password": PW, **proof}).json()
        cred = {"id": "AQIDBA", "response": {"transports": ["internal"]}}
        with mock.patch(
                "oikonome.auth.passkeys.verify_registration_response",
                return_value=_FakeReg()):
            return self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"], "credential": cred,
                "password": PW, **proof})

    def _totp_and_passkey(self):
        self._totp()
        r = self._passkey(totp_code=_code(self.secret))
        self.assertEqual(r.status_code, 200, r.text)
        self.pk = r.json()["id"]

    # ---- the planted set -------------------------------------------------

    def _caller_session_id(self, client):
        tok = client.cookies.get(sessions.COOKIE_NAME)
        if not tok:
            return None
        row = self.cc.execute("SELECT id FROM sessions WHERE token_hash=%s",
                              (sessions._h(tok),)).fetchone()
        return row and row["id"]

    def _plant(self, uid, tid, email, owner=True, caller=None) -> dict:
        """Every foothold, straight in the database, so every door faces
        the identical set. `caller`: the signed-in client whose own
        session and push subscription an in-session door must keep."""
        p = {"uid": uid, "tid": tid, "owner": owner}
        admin = _admin()
        try:
            p["session"] = secrets.token_hex(32)
            admin.execute(
                "INSERT INTO sessions (token_hash, user_id, tenant_id, "
                "expires_at, elevated_at, elevated_passkey_id) VALUES "
                "(%s, %s, %s, now() + interval '1 day', now(), %s)",
                (p["session"], uid, tid, self.pk))
            minted = device_tokens.mint(admin, uid, tid, "planted",
                                        "android")
            p["device"] = minted[1]["id"]
            p["ticket"] = device_tokens._h(
                device_tokens.issue_mint_ticket(admin, uid))
            p["token"] = admin.execute(
                "INSERT INTO api_tokens (token_hash, tenant_id, created_by, "
                "name) VALUES (%s, %s, %s, 'collector') RETURNING id",
                (secrets.token_hex(32), tid, uid)).fetchone()["id"]
            p["webhook"] = admin.execute(
                "INSERT INTO webhooks (tenant_id, url, secret, events, "
                "created_by, created_by_user) VALUES (%s, %s, 'sealed', "
                "ARRAY['*'], %s, %s) RETURNING id",
                (tid, f"https://hooks.example/{uuid.uuid4().hex}", email,
                 str(uid))).fetchone()["id"]
            p["support"] = admin.execute(
                "INSERT INTO support_consents (tenant_id, granted_by, reason, "
                "expires_at) VALUES (%s, %s, 'help', now() + interval "
                "'1 day') RETURNING id", (tid, uid)).fetchone()["id"]
            p["push"] = f"https://push.example/{uuid.uuid4().hex}"
            admin.execute(
                "INSERT INTO push_subscriptions (user_id, tenant_id, "
                "endpoint, p256dh, auth) VALUES (%s, %s, %s, 'k', 'a')",
                (uid, tid, p["push"]))
            p["own_push"] = None
            sid = caller and self._caller_session_id(caller)
            if sid:
                p["own_push"] = f"https://push.example/{uuid.uuid4().hex}"
                admin.execute(
                    "INSERT INTO push_subscriptions (user_id, tenant_id, "
                    "endpoint, p256dh, auth, session_id) VALUES "
                    "(%s, %s, %s, 'k', 'a', %s)",
                    (uid, tid, p["own_push"], sid))
            admin.commit()
            p["reset_link"] = reset_mod.create_reset(admin, uid)
            factor_reset.request(admin, uid)
            admin.commit()
            p["sheet"] = recovery.issue(admin, uid)
            admin.commit()
        finally:
            admin.close()
        p["invite"] = invites.create(self.cc, tid, uid, role="member",
                                     label="later@example.dev")["token"]
        if owner:
            member_email = f"joined-{uuid.uuid4().hex[:8]}@example.dev"
            claimed = invites.create(self.cc, tid, uid, role="member",
                                     label=member_email)["token"]
            p["member"] = invites.claim(self.cc, claimed, member_email,
                                        PW)["user_id"]
            conn = tenancy.tenant_connect(tid)
            try:
                cfg = budget.load_config(conn)
                cfg.update(smtp_host="relay.example.dev", smtp_port=2525,
                           smtp_starttls=False,
                           llm_url="http://10.0.0.9:11434", llm_model="x",
                           notify_phone={"number": "+12175551234",
                                         "verified": True})
                budget.save_config(conn, cfg)
            finally:
                conn.close()
        return p

    # ---- what stands afterwards -----------------------------------------

    def _closed(self, p) -> dict:
        uid, tid = p["uid"], p["tid"]
        admin = _admin()
        try:
            one = lambda sql, *a: admin.execute(sql, a).fetchone()  # noqa: E731
            s = one("SELECT elevated_at FROM sessions WHERE token_hash=%s",
                    p["session"])
            out = {
                "session": s is None,
                "elevation": s is None or s["elevated_at"] is None,
                "device": one("SELECT revoked_at FROM device_tokens "
                              "WHERE id=%s", p["device"])["revoked_at"]
                is not None,
                "ticket": one("SELECT 1 FROM webauthn_challenges "
                              "WHERE challenge=%s", p["ticket"]) is None,
                "token": one("SELECT revoked_at FROM api_tokens WHERE id=%s",
                             p["token"])["revoked_at"] is not None,
                "webhook": not one("SELECT enabled FROM webhooks WHERE id=%s",
                                   p["webhook"])["enabled"],
                "support": one("SELECT revoked_at FROM support_consents "
                               "WHERE id=%s", p["support"])["revoked_at"]
                is not None,
                "push": one("SELECT 1 FROM push_subscriptions "
                            "WHERE endpoint=%s", p["push"]) is None,
                "reset_link": reset_mod.lookup_reset(
                    admin, p["reset_link"]) is None,
                "factor_reset": factor_reset.pending(admin, uid) is None,
                "invite": one("SELECT 1 FROM invites WHERE token_hash=%s",
                              invites._h(p["invite"])) is None,
                "sheet": one("SELECT 1 FROM recovery_codes WHERE user_id=%s "
                             "AND code_hash=%s", uid,
                             recovery._h(p["sheet"][0])) is None,
            }
            if p["owner"]:
                out["member"] = one("SELECT 1 FROM users WHERE id=%s",
                                    p["member"]) is None
                cfg = one("SELECT config FROM tenant_settings "
                          "WHERE tenant_id=%s", tid)["config"]
                rec = outbound_pause.paused(cfg)
                # a hosted household's relay is the instance's, so the
                # AI endpoint is the setting every instance pauses
                out["outbound"] = bool(rec and (rec["smtp"] or rec["llm"]))
                phone = cfg.get("notify_phone")
                out["sms"] = not (isinstance(phone, dict)
                                  and phone.get("verified"))
            if p["own_push"]:
                out["own_push"] = one("SELECT 1 FROM push_subscriptions "
                                      "WHERE endpoint=%s",
                                      p["own_push"]) is None
        finally:
            admin.close()
        return out

    def _check(self, door, p, answer=None, caller_kept=True):
        level, sheet, extra = EXPECTED[door]
        closes = P.LEVELS.get(level, frozenset())
        got = self._closed(p)
        want = {f: (FOOTHOLD[f] in closes or f in extra)
                for f in FOOTHOLD if p["owner"] or f not in OWNER_ONLY}
        want["sheet"] = sheet
        if "own_push" in got:
            want["own_push"] = not caller_kept
        diff = {f: ("closed" if got[f] else "left standing")
                for f in want if got[f] != want[f]}
        self.assertEqual(diff, {}, f"{door} (level {level}): these "
                                   f"footholds did not do what the level "
                                   f"says")
        if caller_kept:
            self.assertEqual(self.client.get("/api/me").status_code, 200,
                             f"{door} signed its own caller out")
        if answer is not None:
            # one reply shape on every rotating door
            for k in P.blank_report():
                self.assertIn(k, answer, f"{door}: reply lacks {k}")
            if sheet and level != P.RECOVERY and EXPECTED[door] and \
                    answer.get("recovery_codes"):
                self.assertEqual(len(answer["recovery_codes"]),
                                 recovery.BATCH)

    def _plant_here(self):
        return self._plant(self.uid, self.tid, self.email,
                           caller=self.client)

    # ---- the doors -------------------------------------------------------

    def test_password_change(self):
        p = self._plant_here()
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": NEW})
        self.assertEqual(r.status_code, 200, r.text)
        self._check("password_change", p, r.json())

    def test_password_change_reissue(self):
        self._totp()
        p = self._plant_here()
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": NEW,
            "totp_code": _code(self.secret), "reissue_codes": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["recovery_codes"])
        self._check("password_change_reissue", p, r.json())

    def test_password_change_keeps_sheet(self):
        self._totp()
        p = self._plant_here()
        r = self.client.post("/api/password/change", json={
            "current_password": PW, "new_password": NEW,
            "totp_code": _code(self.secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(r.json()["recovery_codes"])
        self._check("password_change_keeps_sheet", p, r.json())

    def test_email_change(self):
        p = self._plant_here()
        r = self.client.post("/api/email/change", data={
            "new_email": f"moved-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self._check("email_change", p, r.json())

    def test_email_change_reissue(self):
        self._totp()
        p = self._plant_here()
        r = self.client.post("/api/email/change", data={
            "new_email": f"moved-{uuid.uuid4().hex[:8]}@example.dev",
            "password": PW, "totp_code": _code(self.secret),
            "reissue_codes": "1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["recovery_codes"])
        self._check("email_change_reissue", p, r.json())

    def test_totp_on(self):
        secret = self.client.post("/api/totp/enroll",
                                  data={"password": PW}).json()["secret"]
        p = self._plant_here()
        r = self.client.post("/api/totp/confirm", data={
            "secret": secret, "code": _code(secret), "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        self._check("totp_on", p, r.json())

    def test_totp_off_reissue(self):
        self._totp_and_passkey()
        p = self._plant_here()
        r = self.client.post("/api/totp/disable", data={
            "code": _code(self.secret), "password": PW,
            "reissue_codes": "1"})
        self.assertEqual(r.status_code, 200, r.text)
        self._check("totp_off_reissue", p, r.json())

    def test_recovery_regenerate(self):
        self._totp()
        p = self._plant_here()
        r = self.client.post("/api/totp/recovery-regenerate", data={
            "password": PW, "totp_code": _code(self.secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self._check("recovery_regenerate", p, r.json())

    def test_first_passkey(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        p = self._plant_here()
        r = self._passkey()
        self.assertEqual(r.status_code, 200, r.text)
        self._check("first_passkey", p, r.json())

    def _delete_key(self, **extra):
        return self.client.request("DELETE", f"/api/passkeys/{self.pk}",
                                   json={"password": PW,
                                         "totp_code": _code(self.secret),
                                         **extra})

    def test_passkey_delete_reissue(self):
        self._totp_and_passkey()
        p = self._plant_here()
        r = self._delete_key(reissue_codes=True)
        self.assertEqual(r.status_code, 200, r.text)
        self._check("passkey_delete_reissue", p, r.json())

    def test_passkey_delete(self):
        self._totp_and_passkey()
        p = self._plant_here()
        r = self._delete_key()
        self.assertEqual(r.status_code, 200, r.text)
        self._check("passkey_delete", p, r.json())

    def test_reset(self):
        p = self._plant_here()
        r = TestClient(self.app).post(
            "/reset", data={"token": p["reset_link"], "password": NEW},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self._check("reset", p, caller_kept=False)
        self.assertEqual(self.client.get("/api/me").status_code, 401,
                         "a reset left the old browser signed in")

    def test_clear_second_factor(self):
        p = self._plant_here()
        admin = _admin()
        try:
            factor_reset.clear_second_factor(admin, self.uid)
            admin.commit()
        finally:
            admin.close()
        self._check("clear_second_factor", p, caller_kept=False)
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_email_revert(self):
        admin = _admin()
        try:
            token = email_revert.create(
                admin, self.uid, f"old-{uuid.uuid4().hex[:8]}@example.dev",
                self.email)
            admin.commit()
        finally:
            admin.close()
        p = self._plant_here()
        r = TestClient(self.app).post("/account/email-revert",
                                      data={"token": token})
        self.assertEqual(r.status_code, 200, r.text)
        self._check("email_revert", p, caller_kept=False)
        self.assertEqual(self.client.get("/api/me").status_code, 401)

    def test_move_out(self):
        """The reclaim's member road: an unverified member's row moves into
        a fresh household under the clicker's password."""
        member = f"mov-{uuid.uuid4().hex[:8]}@example.dev"
        admin = _admin()
        try:
            vid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'member') RETURNING id",
                (self.tid, member)).fetchone()["id"]
            token = signup_reclaim.create(admin, vid, member, None, None)
            admin.commit()
        finally:
            admin.close()
        p = self._plant(vid, self.tid, member, owner=False)
        os.environ.update({"OIKONOME_HOSTED": "1",
                           "OIKONOME_OPEN_SIGNUP": "1"})
        with mock.patch.object(self.appmod, "DEV_MODE", False):
            r = TestClient(self.app).post(
                "/signup/reclaim", data={"token": token,
                                         "password": "a-new-own-password"},
                follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        moved = self.cc.execute("SELECT tenant_id FROM users WHERE id=%s",
                                (vid,)).fetchone()["tenant_id"]
        self.assertNotEqual(moved, self.tid)
        self._check("move_out", p, caller_kept=False)


class AMembersOwnRotationRemovesNobody(EveryDoorClosesExactlyItsLevel):
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
        for uid in (other, self.uid):
            self.assertIsNotNone(self.cc.execute(
                "SELECT 1 FROM users WHERE id=%s", (uid,)).fetchone())


# only the member's own password change runs in the subclass above
for _name in [n for n in vars(EveryDoorClosesExactlyItsLevel)
              if n.startswith("test_") and n != "test_password_change"]:
    setattr(AMembersOwnRotationRemovesNobody, _name, None)
del _name


class EveryLevelIsATableRow(unittest.TestCase):
    """The doors above are every door auth/posture names, and every level
    a door uses is in the table."""

    def test_every_level_has_a_door(self):
        used = {lvl for lvl, _, _ in EXPECTED.values() if lvl}
        self.assertEqual(used, set(P.LEVELS))

    def test_routine_sheet_is_sign_in_only(self):
        # the user's decision: a routine regenerate stops at sign-in
        self.assertEqual(P.LEVELS[P.SHEET], {
            P.SESSIONS, P.DEVICES, P.MINT_TICKETS, P.PUSH, P.RECOVERIES})

    def test_a_level_that_touches_the_sheet_needs_a_batch(self):
        # Without a batch a CREDENTIAL rotation could neither void the
        # sheet it leaves with no factor nor mint one, and would succeed
        # silently with the old codes live. Refused before any lock, so
        # nothing is written.
        conn = mock.MagicMock()
        for level, mint in ((P.CREDENTIAL, False), (P.SHEET, False),
                            (P.FACTOR, True)):
            with self.subTest(level=level), \
                    self.assertRaises(ValueError):
                P.rotate(conn, None, {"user_id": "u"}, level, mint=mint)
        conn.execute.assert_not_called()
        conn.transaction.assert_not_called()


class AFirstPasskeysSheetIsStagedWithTheKey(unittest.TestCase):
    """The first passkey's recovery sheet commits after the key's own
    transaction, never before it. Minted on its own committed connection,
    a failure after the mint would leave the key bound and the rotation
    run with a batch no reply ever showed — and a failure in the mint
    would leave the key bound with no sheet, after which a retry sees a
    passkey, takes the new one for a second key, and never mints again."""

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
        os.environ.pop("OIKONOME_HOSTED", None)
        self.addCleanup(os.environ.pop, "OIKONOME_HOSTED", None)
        self.email = f"stg-{uuid.uuid4().hex[:8]}@example.dev"
        self.client = TestClient(self.app, raise_server_exceptions=False)
        r = self.client.post("/api/signup", data={"email": self.email,
                                                  "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        os.environ["OIKONOME_HOSTED"] = "1"
        conn = tenancy.control_connect()
        try:
            self.uid = conn.execute("SELECT id FROM users WHERE email=%s",
                                    (self.email,)).fetchone()["id"]
        finally:
            conn.close()

    def _register(self):
        o = self.client.post("/api/passkeys/options",
                             json={"password": PW}).json()
        with mock.patch(
                "oikonome.auth.passkeys.verify_registration_response",
                return_value=_FakeReg()):
            return self.client.post("/api/passkeys", json={
                "challenge_id": o["challenge_id"],
                "credential": {"id": "AQIDBA",
                               "response": {"transports": ["internal"]}},
                "password": PW})

    def _keys(self):
        conn = tenancy.control_connect()
        try:
            return conn.execute("SELECT count(*) AS n FROM passkeys "
                                "WHERE user_id=%s",
                                (self.uid,)).fetchone()["n"]
        finally:
            conn.close()

    def test_a_failed_first_batch_binds_no_key(self):
        real = recovery.issue

        def boom(*a, **k):
            raise RuntimeError("the batch did not go in")
        with mock.patch.object(recovery, "issue", boom):
            r = self._register()
        self.assertEqual(r.status_code, 500, r.text)
        self.assertEqual(self._keys(), 0,
                         "the key was bound without the sheet it mints")
        self.assertIs(recovery.issue, real)
        # and the retry is still a first factor: it mints
        r = self._register()
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()["recovery_codes"]), recovery.BATCH)


class AReissueRacingAMoveOutNeverLandsOnTheMovedAccount(unittest.TestCase):
    """A reissue that starts while the reclaim's move-out holds the
    member's row must not land its batch on the account the clicker now
    owns: whoever held the member's old session would hold a sheet for it.
    The move takes the factor lock before the row lock, so the reissue
    waits for the move and then finds its caller signed out."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        self._p = [
            mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                         "OIKONOME_OPEN_SIGNUP": "1"}),
            mock.patch.object(self.appmod, "DEV_MODE", False),
            mock.patch.object(self.appmod, "_deliver_verification"),
        ]
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in reversed(self._p)])

    def test_the_reissue_is_refused_and_no_sheet_follows(self):
        from oikonome.auth import passwords, totp as totp_mod
        host = f"rh-{uuid.uuid4().hex[:8]}@example.dev"
        member = f"rm-{uuid.uuid4().hex[:8]}@example.dev"
        r = TestClient(self.appmod.app).post(
            "/api/signup", data={"email": host, "password": PW,
                                 "invite": "", "ref": ""})
        self.assertEqual(r.status_code, 200, r.text)
        secret = totp_mod.new_secret()
        admin = _admin()
        try:
            tid = admin.execute("SELECT tenant_id FROM users WHERE email=%s",
                                (host,)).fetchone()["tenant_id"]
            vid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role, "
                "totp_secret) VALUES (%s, %s, %s, 'member', %s) "
                "RETURNING id",
                (tid, member, passwords.hash_password(PW),
                 crypto.encrypt_cp(secret))).fetchone()["id"]
            token = signup_reclaim.create(admin, vid, member, None, None)
            stolen = sessions.create_session(admin, vid, tid)
            admin.commit()
        finally:
            admin.close()
        thief = TestClient(self.appmod.app)
        thief.cookies.set(sessions.COOKIE_NAME, stolen)
        self.assertEqual(thief.get("/api/me").status_code, 200)

        # The reissue is signed in and past its step-up when the move takes
        # the member's row; the move is then held inside that row lock,
        # where it blanks the member's assistant questions, while the
        # reissue goes on.
        authed, resume = threading.Event(), threading.Event()
        held, go = threading.Event(), threading.Event()
        from oikonome.engine import assistant
        real_forget = assistant.forget_asker
        real_elev = self.appmod._require_elevation

        def slow_forget(*a, **k):
            held.set()
            go.wait(15)
            return real_forget(*a, **k)

        def paused_elev(*a, **k):
            out_ = real_elev(*a, **k)
            authed.set()
            resume.wait(15)
            return out_

        out = {}

        def move():
            out["move"] = TestClient(self.appmod.app).post(
                "/signup/reclaim",
                data={"token": token, "password": "the-clickers-own-pw"},
                follow_redirects=False)

        def reissue():
            out["reissue"] = thief.post(
                "/api/totp/recovery-regenerate",
                data={"password": PW, "totp_code": _code(secret)})

        with mock.patch.object(assistant, "forget_asker", slow_forget), \
                mock.patch.object(self.appmod, "_require_elevation",
                                  paused_elev):
            tr = threading.Thread(target=reissue, daemon=True)
            tr.start()
            self.assertTrue(authed.wait(15), "the reissue never got going")
            tm = threading.Thread(target=move, daemon=True)
            tm.start()
            self.assertTrue(held.wait(15), "the move never reached its lock")
            resume.set()
            tr.join(1.5)             # the reissue reaches its wait
            go.set()
            tm.join(20)
            tr.join(20)
        self.assertFalse(tm.is_alive() or tr.is_alive(),
                         "the move and the reissue hung on each other")
        self.assertEqual(out["move"].status_code, 303, out["move"].text)
        self.assertNotEqual(out["reissue"].status_code, 200,
                            out["reissue"].text)
        admin = _admin()
        try:
            left = admin.execute("SELECT count(*) AS n FROM recovery_codes "
                                 "WHERE user_id=%s", (vid,)).fetchone()["n"]
        finally:
            admin.close()
        self.assertEqual(left, 0, "the reissue's sheet landed on the moved "
                                  "account")


class NoFootholdIsClosedOutsidePosture(unittest.TestCase):
    """The rule that keeps the table true: the code that takes a foothold
    away lives in auth/posture, and every rotating door gets there through
    `posture.rotate` — once. A door that spells its own sweep again is the
    drift this replaced (a forgotten script token, a forgotten sheet, a
    routine door running the hijack rotation)."""

    SWEEPS = re.compile(
        r"DELETE FROM sessions WHERE user_id"
        r"|api_tokens SET revoked_at"
        r"|support_consents SET revoked_at"
        r"|DELETE FROM push_subscriptions"
        r"|DELETE FROM passkeys WHERE user_id"
        r"|DELETE FROM recovery_codes"
        r"|pause_for_rotation\("
        r"|revoke_all_for_user\("
        r"|revoke_all_others\("
        r"|revoke_mint_tickets\("
        r"|_drop_web_push\("
        r"|_unverify_sms_number\("
        r"|_drop_sms_number\("
        r"|evict_invite_footholds\("
        r"|_end_pending_recoveries\(")

    # Functions that end sign-ins or a person's place in a household and
    # are not credential rotations, each for its own reason:
    ALLOWED = {
        # one sign-in, ended by its holder
        ("web/app.py", "logout"): "signs out the one session asking",
        ("web/app.py", "logout_page"): "signs out the one session asking",
        # "sign out everywhere else": sessions only, by request, no change
        # to any credential
        ("web/app.py", "sessions_revoke"): "the session list's own button",
        ("web/app.py", "_drop_web_push"): "the button above's push half",
        # a member removed or leaving: THAT person's own tokens and hooks
        ("web/app.py", "user_remove"): "a member removed by the owner",
        ("web/app.py", "account_leave"): "a member leaving",
        ("auth/reset.py", "_remove_members"): "members a rotation removes",
        # the member-and-invite step the rotation calls, kept where the
        # claim it races lives
        ("auth/reset.py", "evict_invite_footholds"): "called by rotate",
        # thin aliases into posture, kept for their callers
        ("auth/reset.py", "_drop_sms_number"): "alias into posture",
        ("auth/reset.py", "_unverify_sms_number"): "alias into posture",
        # the primitives themselves
        ("auth/recovery.py", "issue"): "the batch mint",
        ("auth/recovery.py", "drop"): "the batch void",
    }

    ROOT = Path(__file__).resolve().parents[1] / "oikonome"
    FILES = ("web/app.py", "auth/reset.py", "auth/factor_reset.py",
             "auth/recovery.py", "web/adminconsole.py", "cli.py")

    def _functions(self, rel):
        src = (self.ROOT / rel).read_text()
        lines = src.splitlines()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                body = "\n".join(lines[node.lineno - 1:node.end_lineno])
                yield node, body

    def test_no_sweep_outside_posture(self):
        found = []
        for rel in self.FILES:
            for node, body in self._functions(rel):
                # nested functions are scanned with their parent
                if node.col_offset:
                    continue
                if self.SWEEPS.search(body) and \
                        (rel, node.name) not in self.ALLOWED:
                    found.append(f"{rel}:{node.name}")
        self.assertEqual(found, [], "foothold SQL outside auth/posture")

    def test_the_allowlist_has_not_gone_stale(self):
        names = {(rel, n.name) for rel in self.FILES
                 for n, _ in self._functions(rel)}
        self.assertEqual(set(self.ALLOWED) - names, set())
        self.assertTrue(all(self.ALLOWED.values()))

    DOORS = {"password_change", "totp_confirm", "recovery_regenerate",
             "totp_disable", "email_change", "_resend_email_change",
             "email_revert_finish", "signup_reclaim_finish",
             "passkey_register", "passkey_delete"}

    def test_every_door_rotates_once(self):
        counts = {}
        for node, body in self._functions("web/app.py"):
            if node.name in self.DOORS and not node.col_offset:
                counts[node.name] = body.count("posture.rotate(")
        self.assertEqual(counts, {d: 1 for d in self.DOORS})
        src = (self.ROOT / "auth/reset.py").read_text()
        self.assertEqual(src.count("posture.rotate("), 1)


if __name__ == "__main__":
    unittest.main()
