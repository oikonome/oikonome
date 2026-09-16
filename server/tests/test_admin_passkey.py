"""The admin console stops being one long password.

Three layers, tested here:

1. an operator PASSKEY is the credential — enrol, sign in, and the audit
   trail names which key acted (a shared token could only ever name an IP);
2. STEP-UP on the destructive host-agent commands — a stolen console
   cookie must not reach restore/reset/uninstall;
3. CLOUDFLARE ACCESS verified at the origin — an edge policy binds
   nothing if the origin is reachable directly.

Plus the two invariants the change must not break: the console still 404s
when it is off, and a self-host install with no passkey and no Cloudflare
keeps its plain token sign-in.

The WebAuthn verifier is mocked (no authenticator in CI) exactly as
test_passkeys.py does it — what is under test is the plumbing around the
signature, not py_webauthn's crypto.
"""

import base64
import datetime as dt
import os
import re
import tempfile
import time
import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db

TOKEN = "test-console-token-" + "z" * 32
CRED_RAW = b"\xa1\xb2\xc3\xd4" + uuid.uuid4().bytes      # unique per run
CRED_B64 = base64.urlsafe_b64encode(CRED_RAW).rstrip(b"=").decode()


class _FakeReg:
    credential_id = CRED_RAW
    credential_public_key = b"\x05\x06\x07\x08"
    sign_count = 0


class _FakeAuth:
    new_sign_count = 9


def _admin(sql, params=()):
    a = tenancy.admin_connect()
    try:
        return a.execute(sql, params).fetchall()
    finally:
        a.close()


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        os.environ["OIKONOME_ADMIN_TOKEN"] = TOKEN
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.appmod.app)

    def tearDown(self):
        for k in ("OIKONOME_ADMIN_TOKEN", "OIKONOME_ADMIN_REQUIRE_PASSKEY",
                  "OIKONOME_ADMIN_IPS", "OIKONOME_CF_ACCESS_TEAM",
                  "OIKONOME_CF_ACCESS_AUD", "OIKONOME_STATE_DIR"):
            os.environ.pop(k, None)
        a = tenancy.admin_connect()
        try:
            # admin_credentials.credential_id is globally UNIQUE on a
            # control-plane table — leaving the row behind would make this
            # suite pass exactly once per database.
            a.execute("DELETE FROM admin_sessions")
            a.execute("DELETE FROM admin_challenges")
            a.execute("DELETE FROM admin_credentials")
        finally:
            a.close()

    # ---- helpers ---------------------------------------------------------

    def _token_login(self):
        r = self.client.post("/admin/console/login", data={"token": TOKEN},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        return r

    def _enrol(self, label="yubikey-5c"):
        o = self.client.post("/admin/console/passkey/options",
                             json={"label": label})
        self.assertEqual(o.status_code, 200, o.text)
        o = o.json()
        with mock.patch("oikonome.auth.admin_passkeys."
                        "verify_registration_response", return_value=_FakeReg()) as v:
            r = self.client.post("/admin/console/passkey", json={
                "challenge_id": o["challenge_id"], "label": label,
                "credential": {"id": CRED_B64, "rawId": CRED_B64,
                               "response": {"transports": ["usb"]}}})
        self.assertEqual(r.status_code, 200, r.text)
        # UV is enforced on the SIGNED response, not just in the options
        self.assertTrue(v.call_args.kwargs.get("require_user_verification"))
        return r.json()

    def _assertion(self):
        return {"id": CRED_B64, "rawId": CRED_B64,
                "response": {"authenticatorData": "AA", "clientDataJSON": "AA",
                             "signature": "AA"}}

    def _passkey_login(self):
        o = self.client.post("/admin/console/passkey/login/options")
        self.assertEqual(o.status_code, 200, o.text)
        o = o.json()
        with mock.patch("oikonome.auth.admin_passkeys."
                        "verify_authentication_response",
                        return_value=_FakeAuth()) as v:
            r = self.client.post("/admin/console/passkey/login", json={
                "challenge_id": o["challenge_id"],
                "credential": self._assertion()})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(v.call_args.kwargs.get("require_user_verification"))
        return r

    def _ticket(self):
        o = self.client.post("/admin/console/passkey/stepup/options")
        self.assertEqual(o.status_code, 200, o.text)
        o = o.json()
        with mock.patch("oikonome.auth.admin_passkeys."
                        "verify_authentication_response",
                        return_value=_FakeAuth()):
            r = self.client.post("/admin/console/passkey/stepup", json={
                "challenge_id": o["challenge_id"],
                "credential": self._assertion()})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["stepup_ticket"]


class EnrolAndLoginTests(_Base):
    def test_enrol_then_sign_in_with_the_passkey(self):
        self._token_login()
        out = self._enrol()
        self.assertTrue(out["id"])
        # a NEW client (no cookie) signs in with the key alone
        self.client = TestClient(self.appmod.app)
        self._passkey_login()
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Operator access", r.text)      # dashboard, not login

    def test_enrolment_requires_a_session(self):
        # no cookie: neither the options call nor the persist call may work
        self.assertEqual(
            self.client.post("/admin/console/passkey/options",
                             json={}).status_code, 401)
        r = self.client.post("/admin/console/passkey", json={
            "challenge_id": str(uuid.uuid4()), "credential": {}})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(_admin("SELECT 1 FROM admin_credentials"), [])

    def test_login_challenge_is_single_use(self):
        self._token_login()
        self._enrol()
        self.client = TestClient(self.appmod.app)
        o = self.client.post("/admin/console/passkey/login/options").json()
        for expect in (200, 401):
            with mock.patch("oikonome.auth.admin_passkeys."
                            "verify_authentication_response",
                            return_value=_FakeAuth()):
                r = self.client.post("/admin/console/passkey/login", json={
                    "challenge_id": o["challenge_id"],
                    "credential": self._assertion()})
            self.assertEqual(r.status_code, expect, r.text)

    def test_login_options_refuse_before_any_key_is_enrolled(self):
        # nothing to assert against — and the answer must not be a 500
        r = self.client.post("/admin/console/passkey/login/options")
        self.assertEqual(r.status_code, 400)

    def test_unknown_credential_is_rejected(self):
        self._token_login()
        self._enrol()
        self.client = TestClient(self.appmod.app)
        o = self.client.post("/admin/console/passkey/login/options").json()
        other = base64.urlsafe_b64encode(b"nope" * 4).rstrip(b"=").decode()
        with mock.patch("oikonome.auth.admin_passkeys."
                        "verify_authentication_response",
                        return_value=_FakeAuth()) as v:
            r = self.client.post("/admin/console/passkey/login", json={
                "challenge_id": o["challenge_id"],
                "credential": {"id": other, "rawId": other, "response": {}}})
        self.assertEqual(r.status_code, 401)
        v.assert_not_called()          # never reaches the verifier

    def test_login_page_offers_the_passkey_only_once_one_exists(self):
        r = self.client.get("/admin/console")
        self.assertNotIn("data-admin-passkey-login", r.text)
        self._token_login()
        self._enrol()
        self.client = TestClient(self.appmod.app)
        r = self.client.get("/admin/console")
        self.assertIn("data-admin-passkey-login", r.text)


class DashboardStepupFlagTests(_Base):
    def test_authed_dashboard_arms_the_enrol_stepup_once_a_key_exists(self):
        """The enrol form's data-stepup gate reads passkeys_enrolled, so
        the AUTHED dashboard must pass it. Jinja's Undefined is falsy;
        otherwise, on any console with one key, the second-key enrolment
        runs a full WebAuthn ceremony and then 403s on the missing step-up
        ticket, every time."""
        # the enrol form only renders in a secure context — localhost is one
        self.client = TestClient(self.appmod.app, base_url="http://localhost")
        self._token_login()
        r = self.client.get("/admin/console")
        self.assertNotIn("data-stepup", r.text,
                         "no key enrolled yet — no step-up to demand")
        self._enrol()
        r = self.client.get("/admin/console")
        self.assertIn('data-stepup="1"', r.text,
                      "with a key enrolled, the dashboard's enrol form "
                      "must demand the fresh-assertion step-up")


class TenantDeleteStepupTests(_Base):
    def test_immediate_purge_needs_a_fresh_assertion_once_keys_exist(self):
        """An immediate tenant purge is as destructive (to that household)
        as host restore/reset/uninstall, so it demands the same fresh
        single-use passkey assertion; otherwise a stolen console cookie that
        cannot reset the box could still permanently destroy any
        household."""
        self._token_login()
        self._enrol()
        admin = tenancy.admin_connect()
        try:
            tid = str(tenancy.create_tenant(
                admin, f"del-{uuid.uuid4().hex[:8]}"))
        finally:
            admin.close()
        r = self.client.post("/admin/console/tenant-delete",
                             data={"tenant_id": tid, "mode": "immediate",
                                   "confirm": tid})
        self.assertEqual(r.status_code, 200)
        self.assertIn("passkey confirmation", r.text)
        admin = tenancy.admin_connect()
        try:
            row = admin.execute("SELECT 1 FROM tenants WHERE id=%s",
                                (tid,)).fetchone()
            self.assertIsNotNone(row, "the purge ran without the step-up")
            acts = [a["action"] for a in admin.execute(
                "SELECT action FROM admin_audit WHERE target=%s",
                (tid,)).fetchall()]
            self.assertIn("tenant_delete_refused", acts)
            # cleanup
            tenancy.delete_tenant_rows(admin, tid)
        finally:
            admin.close()

    def test_immediate_purge_still_works_with_no_key_enrolled(self):
        """Same rule as run_command: a plain-http self-host cannot enrol a
        key, and refusing there would take the console's tools away."""
        self._token_login()
        admin = tenancy.admin_connect()
        try:
            tid = str(tenancy.create_tenant(
                admin, f"del2-{uuid.uuid4().hex[:8]}"))
        finally:
            admin.close()
        r = self.client.post("/admin/console/tenant-delete",
                             data={"tenant_id": tid, "mode": "immediate",
                                   "confirm": tid})
        self.assertEqual(r.status_code, 200)
        admin = tenancy.admin_connect()
        try:
            row = admin.execute("SELECT 1 FROM tenants WHERE id=%s",
                                (tid,)).fetchone()
            self.assertIsNone(row)
        finally:
            admin.close()


class RequirePasskeyTests(_Base):
    def test_valid_token_alone_gets_a_login_page_and_no_session(self):
        self._token_login()
        self._enrol()
        self.client = TestClient(self.appmod.app)
        os.environ["OIKONOME_ADMIN_REQUIRE_PASSKEY"] = "1"
        # drop the bootstrap session so what is left afterwards is only
        # whatever this refused login created (which must be nothing)
        a = tenancy.admin_connect()
        try:
            a.execute("DELETE FROM admin_sessions")
        finally:
            a.close()
        r = self.client.post("/admin/console/login", data={"token": TOKEN},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 200)              # the login page
        self.assertIn("requires a passkey", r.text)
        self.assertNotIn("oikonome_admin", r.headers.get("set-cookie", ""))
        self.assertEqual(
            _admin("SELECT 1 FROM admin_sessions"), [],
            "a refused token must not leave a session behind")
        # …and the refusal is on the trail
        actions = [r["action"] for r in _admin(
            "SELECT action FROM admin_audit ORDER BY id DESC LIMIT 3")]
        self.assertIn("login_refused_token_only", actions)

    def test_the_passkey_still_works_under_the_flag(self):
        self._token_login()
        self._enrol()
        self.client = TestClient(self.appmod.app)
        os.environ["OIKONOME_ADMIN_REQUIRE_PASSKEY"] = "1"
        self._passkey_login()
        self.assertEqual(self.client.get("/admin/console").status_code, 200)

    def test_flag_without_any_enrolled_key_does_not_brick_the_console(self):
        # bootstrap order is token → enrol → flip the flag; honouring the
        # flag on an empty table would need an .env edit + restart to undo
        os.environ["OIKONOME_ADMIN_REQUIRE_PASSKEY"] = "1"
        self._token_login()

    def test_wrong_token_is_still_just_a_wrong_token(self):
        os.environ["OIKONOME_ADMIN_REQUIRE_PASSKEY"] = "1"
        r = self.client.post("/admin/console/login", data={"token": "nope"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("not right", r.text)


class RelyingPartyOriginTests(_Base):
    """A console reachable at more than one origin (its public name, and
    a private-network name a PHONE can use without any DNS setup) must be
    able to act as the relying party for each — but only for origins the
    operator listed."""

    def setUp(self):
        super().setUp()
        os.environ["OIKONOME_BASE_URL"] = "https://app.example.test"

    def tearDown(self):
        os.environ.pop("OIKONOME_BASE_URL", None)
        os.environ.pop("OIKONOME_ADMIN_ORIGINS", None)
        super().tearDown()

    def _rp_for(self, host, scheme="https"):
        import oikonome.web.adminconsole as ac

        class _Req:
            headers = {}
            cookies = {}
            client = None
            url = type("U", (), {"scheme": scheme, "hostname": host.split(":")[0]})()
        r = _Req()
        r.headers = {"host": host}
        # forwarded_host falls back to the Host header for an untrusted
        # peer, which is what a direct client is
        from starlette.datastructures import Headers
        r.headers = Headers({"host": host})
        with mock.patch.object(ac, "request_scheme", return_value=scheme):
            return ac._rp(r)

    def test_listed_magicdns_origin_becomes_the_relying_party(self):
        os.environ["OIKONOME_ADMIN_ORIGINS"] = \
            "https://oikonome-app.tailtest.ts.net:8443"
        rp_id, origin = self._rp_for("oikonome-app.tailtest.ts.net:8443")
        self.assertEqual(rp_id, "oikonome-app.tailtest.ts.net")
        self.assertEqual(origin, "https://oikonome-app.tailtest.ts.net:8443")

    def test_base_url_origin_still_works(self):
        rp_id, origin = self._rp_for("app.example.test")
        self.assertEqual(rp_id, "app.example.test")

    def test_unlisted_host_cannot_move_the_relying_party(self):
        # the whole point: rp_id decides which credentials a browser will
        # offer, so a forged Host must not select it
        os.environ["OIKONOME_ADMIN_ORIGINS"] = \
            "https://oikonome-app.tailtest.ts.net:8443"
        rp_id, origin = self._rp_for("evil.example.com")
        self.assertEqual(rp_id, "app.example.test",
                         "an unlisted host must fall back to BASE_URL")
        self.assertNotIn("evil", origin)

    def test_scheme_and_port_must_match_too(self):
        os.environ["OIKONOME_ADMIN_ORIGINS"] = \
            "https://oikonome-app.tailtest.ts.net:8443"
        # right host, wrong port → not the listed origin
        rp_id, _ = self._rp_for("oikonome-app.tailtest.ts.net:9999")
        self.assertEqual(rp_id, "app.example.test")
        # right host, wrong scheme → likewise
        rp_id, _ = self._rp_for("oikonome-app.tailtest.ts.net:8443",
                                scheme="http")
        self.assertEqual(rp_id, "app.example.test")

    def test_default_ports_compare_equal(self):
        os.environ["OIKONOME_ADMIN_ORIGINS"] = "https://console.example.test:443"
        rp_id, origin = self._rp_for("console.example.test")
        self.assertEqual(rp_id, "console.example.test")
        self.assertEqual(origin, "https://console.example.test")

    def test_unset_variable_changes_nothing(self):
        rp_id, _ = self._rp_for("app.example.test")
        self.assertEqual(rp_id, "app.example.test")
        rp_id, _ = self._rp_for("anything.else")
        self.assertEqual(rp_id, "app.example.test")


class PasskeyOnlyTests(_Base):
    """No operator token at all. The console is on via
    OIKONOME_ADMIN_ENABLED, a passkey is the only credential, and the way
    back in is a host-issued enrolment link."""

    def setUp(self):
        super().setUp()
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)
        os.environ["OIKONOME_ADMIN_ENABLED"] = "1"

    def tearDown(self):
        os.environ.pop("OIKONOME_ADMIN_ENABLED", None)
        super().tearDown()

    def _link(self, minutes=15):
        from oikonome import cli
        admin = tenancy.admin_connect()
        try:
            from oikonome.auth import admin_passkeys
            self.assertEqual(cli.admin_enrol(minutes), 0)
            row = admin.execute(
                "SELECT 1 FROM admin_challenges WHERE purpose='enrol-ticket'"
            ).fetchone()
            self.assertIsNotNone(row, "admin-enrol must mint a ticket")
            del admin_passkeys
        finally:
            admin.close()
        # the CLI prints the ticket; grab the raw value the same way the
        # operator would, by re-minting one we can hold
        admin = tenancy.admin_connect()
        try:
            from oikonome.auth import admin_passkeys
            admin.execute("DELETE FROM admin_challenges "
                          "WHERE purpose='enrol-ticket'")
            return admin_passkeys.mint_enrol_ticket(admin, minutes)
        finally:
            admin.close()

    def test_console_is_on_without_a_token(self):
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 200)
        self.assertIn("admin-enrol", r.text)          # tells you what to run
        self.assertNotIn('name="token"', r.text)      # no dead form

    def test_empty_token_cannot_sign_in(self):
        # compare_digest(_h(""), _h("")) is TRUE — the guard against that is
        # the whole reason this test exists
        for attempt in ("", "anything"):
            r = self.client.post("/admin/console/login",
                                 data={"token": attempt},
                                 follow_redirects=False)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("signs in with a passkey", r.text)
            self.assertEqual(_admin("SELECT 1 FROM admin_sessions"), [])

    def test_enrol_link_enrols_one_key_then_stops_working(self):
        ticket = self._link()
        r = self.client.get(f"/admin/console/enrol?t={ticket}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Enrol a console passkey", r.text)
        # over plain http the form hides itself (WebAuthn needs a secure
        # context) and says why, instead of offering a button that throws
        self.assertNotIn("data-admin-passkey-enrol", r.text)
        self.assertIn("need HTTPS", r.text)
        https = TestClient(self.appmod.app, base_url="https://testserver")
        self.assertIn("data-admin-passkey-enrol",
                      https.get(f"/admin/console/enrol?t={ticket}").text)
        # the ticket stands in for a session on BOTH calls
        o = self.client.post("/admin/console/passkey/options",
                             json={"ticket": ticket, "label": "recovered"})
        self.assertEqual(o.status_code, 200, o.text)
        with mock.patch("oikonome.auth.admin_passkeys."
                        "verify_registration_response", return_value=_FakeReg()):
            r = self.client.post("/admin/console/passkey", json={
                "challenge_id": o.json()["challenge_id"], "ticket": ticket,
                "label": "recovered",
                "credential": {"id": CRED_B64, "rawId": CRED_B64,
                               "response": {"transports": ["usb"]}}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(_admin("SELECT 1 FROM admin_credentials")), 1)
        # burned: one link buys exactly one key
        self.assertEqual(
            self.client.post("/admin/console/passkey/options",
                             json={"ticket": ticket}).status_code, 401)
        self.assertIn("expired or already",
                      self.client.get(
                          f"/admin/console/enrol?t={ticket}").text)
        # and the audit names how the key got there
        # newest first — admin_audit is deliberately never truncated, so an
        # unordered read picks up rows from an earlier test in this database
        self.assertEqual(
            _admin("SELECT actor FROM admin_audit WHERE "
                   "action='admin_passkey_enrolled' "
                   "ORDER BY id DESC LIMIT 1")[0]["actor"],
            "host-enrol-link")

    def test_no_ticket_no_enrolment(self):
        for bad in ({}, {"ticket": "adminenrol-nope"}, {"ticket": "junk"}):
            self.assertEqual(
                self.client.post("/admin/console/passkey/options",
                                 json=bad).status_code, 401, bad)
        self.assertEqual(_admin("SELECT 1 FROM admin_credentials"), [])

    def test_expired_ticket_is_refused(self):
        ticket = self._link()
        a = tenancy.admin_connect()
        try:
            a.execute("UPDATE admin_challenges SET expires_at = now() - "
                      "interval '1 minute' WHERE purpose='enrol-ticket'")
        finally:
            a.close()
        self.assertIn("expired or already",
                      self.client.get(f"/admin/console/enrol?t={ticket}").text)
        self.assertEqual(
            self.client.post("/admin/console/passkey/options",
                             json={"ticket": ticket}).status_code, 401)

    def test_ticket_redeemed_twice_in_a_race_stores_exactly_one_key(self):
        """The validity check on the ticket is a peek, and the burn happens
        after the key is stored. Two redemptions of one ticket that both
        pass the peek would both store a credential, and the burn miss on
        the second must not be logged and forgiven. The burn is the only
        atomic claim, so a miss must take back the key that redemption
        just stored and refuse."""
        from oikonome.auth import admin_passkeys
        ticket = self._link()
        o = self.client.post("/admin/console/passkey/options",
                             json={"ticket": ticket, "label": "first"})
        self.assertEqual(o.status_code, 200, o.text)
        real_verify = admin_passkeys.register_verify

        def _other_redemption_wins_first(admin, *a, **k):
            # the concurrent redemption burns the ticket between this
            # request's peek and its own burn
            self.assertTrue(admin_passkeys.burn_enrol_ticket(admin, ticket))
            return real_verify(admin, *a, **k)

        with mock.patch("oikonome.auth.admin_passkeys."
                        "verify_registration_response",
                        return_value=_FakeReg()), \
             mock.patch("oikonome.auth.admin_passkeys.register_verify",
                        side_effect=_other_redemption_wins_first):
            r = self.client.post("/admin/console/passkey", json={
                "challenge_id": o.json()["challenge_id"], "ticket": ticket,
                "label": "first",
                "credential": {"id": CRED_B64, "rawId": CRED_B64,
                               "response": {"transports": ["usb"]}}})
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("already used", r.json()["error"])
        # the losing redemption's key is gone again: this ticket bought
        # exactly the winner's key (which the simulated winner never
        # stored, so the table is empty)
        self.assertEqual(_admin("SELECT 1 FROM admin_credentials"), [])
        self.assertEqual(
            _admin("SELECT action FROM admin_audit "
                   "ORDER BY id DESC LIMIT 1")[0]["action"],
            "admin_passkey_enrol_refused")

    def test_enrol_door_is_cloaked_when_the_console_is_off(self):
        os.environ.pop("OIKONOME_ADMIN_ENABLED", None)
        self.assertEqual(
            self.client.get("/admin/console/enrol?t=x").status_code, 404)

    def test_sign_in_with_the_key_then_it_is_the_only_way(self):
        ticket = self._link()
        o = self.client.post("/admin/console/passkey/options",
                             json={"ticket": ticket}).json()
        with mock.patch("oikonome.auth.admin_passkeys."
                        "verify_registration_response", return_value=_FakeReg()):
            self.client.post("/admin/console/passkey", json={
                "challenge_id": o["challenge_id"], "ticket": ticket,
                "label": "k", "credential": {
                    "id": CRED_B64, "rawId": CRED_B64,
                    "response": {"transports": ["usb"]}}})
        self.client = TestClient(self.appmod.app)
        self._passkey_login()
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 200)
        self.assertIn("passkey-only", r.text)

    def test_dashboard_does_not_advertise_the_recovery_command(self):
        """The "Lost your key?" note has no place on the passkey-only
        dashboard. It is the one page whose reader has
        provably NOT lost their key, and it printed the exact host
        command that enrols a new credential with no sign-in. The sign-in
        page still names `admin-enrol` — that is where a locked-out
        operator actually reads, and `test_console_is_on_without_a_token`
        holds it there."""
        ticket = self._link()
        o = self.client.post("/admin/console/passkey/options",
                             json={"ticket": ticket}).json()
        with mock.patch("oikonome.auth.admin_passkeys."
                        "verify_registration_response", return_value=_FakeReg()):
            self.client.post("/admin/console/passkey", json={
                "challenge_id": o["challenge_id"], "ticket": ticket,
                "label": "k", "credential": {
                    "id": CRED_B64, "rawId": CRED_B64,
                    "response": {"transports": ["usb"]}}})
        self.client = TestClient(self.appmod.app)
        self._passkey_login()
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("Lost your key?", r.text)

    def test_last_key_cannot_be_removed_from_the_console(self):
        ticket = self._link()
        o = self.client.post("/admin/console/passkey/options",
                             json={"ticket": ticket}).json()
        with mock.patch("oikonome.auth.admin_passkeys."
                        "verify_registration_response", return_value=_FakeReg()):
            cred = self.client.post("/admin/console/passkey", json={
                "challenge_id": o["challenge_id"], "ticket": ticket,
                "label": "k", "credential": {
                    "id": CRED_B64, "rawId": CRED_B64,
                    "response": {"transports": ["usb"]}}}).json()
        self.client = TestClient(self.appmod.app)
        self._passkey_login()
        r = self.client.post("/admin/console/passkey/delete",
                             data={"cred_id": cred["id"],
                                   "stepup": self._ticket()},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("admin-enrol", r.text)
        self.assertEqual(len(_admin("SELECT 1 FROM admin_credentials")), 1)


class HostedIsPasskeyOnlyTests(_Base):
    """Hosted instances are passkey-only; every other instance may still use
    an operator token. Hosted has a domain and a cert, so it
    can always enrol a key; a shared secret buys nothing there and is
    phishable. Self-host keeps the token, because a plain-http LAN install
    cannot enrol anything at all."""

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        super().tearDown()

    def test_hosted_refuses_the_token_even_when_one_is_configured(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        r = self.client.post("/admin/console/login", data={"token": TOKEN},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("signs in with a passkey", r.text)
        self.assertEqual(_admin("SELECT 1 FROM admin_sessions"), [],
                         "a hosted token login must mint nothing")

    def test_hosted_login_page_offers_no_token_form(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn('name="token"', r.text)
        self.assertIn("admin-enrol", r.text)      # says how to get in

    def test_hosted_token_still_switches_the_console_ON(self):
        # the token keeps working as an enable flag so an existing hosted
        # .env does not silently 404 the console after an upgrade
        os.environ["OIKONOME_HOSTED"] = "1"
        self.assertEqual(self.client.get("/admin/console").status_code, 200)
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)
        self.assertEqual(self.client.get("/admin/console").status_code, 404)

    def test_hosted_passkey_login_is_unaffected(self):
        self._token_login()                       # self-host bootstrap
        self._enrol()
        os.environ["OIKONOME_HOSTED"] = "1"
        self.client = TestClient(self.appmod.app)
        self._passkey_login()
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 200)
        self.assertIn("passkey-only", r.text)

    def test_self_host_token_login_still_works(self):
        self.assertNotIn("OIKONOME_HOSTED", os.environ)
        self._token_login()
        self.assertIn('name="token"',
                      TestClient(self.appmod.app).get("/admin/console").text)


class BreakGlassTests(_Base):
    """An operator who keeps a single key must be able to recover from
    losing it on the host, without an .env edit or a restart."""

    def _cli(self, **kw):
        from oikonome import cli
        return cli.admin_keys(**kw)

    def test_reset_restores_token_login_under_the_flag(self):
        self._token_login()
        self._enrol()
        self.client = TestClient(self.appmod.app)
        os.environ["OIKONOME_ADMIN_REQUIRE_PASSKEY"] = "1"
        # locked out: the key is gone and the token alone is refused
        r = self.client.post("/admin/console/login", data={"token": TOKEN},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("requires a passkey", r.text)
        # …one command on the host, and the flag is still set
        self.assertEqual(self._cli(reset=True), 0)
        self.assertEqual(os.environ.get("OIKONOME_ADMIN_REQUIRE_PASSKEY"), "1")
        r = self.client.post("/admin/console/login", data={"token": TOKEN},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303, "token must sign in again")

    def test_reset_is_audited(self):
        self._token_login()
        self._enrol()
        self._cli(reset=True)
        rows = _admin("SELECT action, actor FROM admin_audit "
                      "ORDER BY id DESC LIMIT 1")
        self.assertEqual(rows[0]["action"], "admin_passkey_reset")
        self.assertEqual(rows[0]["actor"], "host-cli")

    def test_remove_one_by_id(self):
        self._token_login()
        cred = self._enrol()
        self.assertEqual(self._cli(remove=cred["id"]), 0)
        self.assertEqual(_admin("SELECT 1 FROM admin_credentials"), [])

    def test_bad_id_and_unknown_id_are_errors_not_crashes(self):
        self.assertEqual(self._cli(remove="not-a-uuid"), 1)
        self.assertEqual(self._cli(remove=str(uuid.uuid4())), 1)

    def test_listing_with_nothing_enrolled_is_clean(self):
        self.assertEqual(self._cli(), 0)

    def test_enrol_prints_a_link_for_every_configured_origin(self):
        """A phone on a private network reaches the console at its
        private-network name and cannot resolve the public one, so printing
        only OIKONOME_BASE_URL prints the one link it cannot open."""
        import io
        from contextlib import redirect_stdout

        from oikonome import cli
        os.environ["OIKONOME_BASE_URL"] = "https://app.example.test"
        os.environ["OIKONOME_ADMIN_ORIGINS"] = \
            "https://host.tailtest.ts.net:8443"
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cli.admin_enrol(5), 0)
            out = buf.getvalue()
        finally:
            os.environ.pop("OIKONOME_BASE_URL", None)
            os.environ.pop("OIKONOME_ADMIN_ORIGINS", None)
        self.assertIn("https://app.example.test/admin/console/enrol?t=", out)
        self.assertIn("https://host.tailtest.ts.net:8443/admin/console/enrol?t=",
                      out)
        # both links carry the SAME ticket — one key, whichever is opened
        tickets = {ln.split("t=")[1].strip()
                   for ln in out.splitlines() if "enrol?t=" in ln}
        self.assertEqual(len(tickets), 1, "one ticket, offered at each origin")
        # and it says the choice of link decides the origin the key binds to
        self.assertIn("SIGN IN at", out)


class AuditActorTests(_Base):
    def test_audit_names_the_credential_that_acted(self):
        self._token_login()
        self._enrol(label="yubikey-5c")
        self.client = TestClient(self.appmod.app)
        self._passkey_login()
        self.client.post("/admin/console/logout", follow_redirects=False)
        # newest first: logout, then the passkey login. (The token login and
        # the enrolment that bootstrapped this sit below them, named "token"
        # — which is exactly the attribution the shared secret could never
        # give.)
        rows = _admin("SELECT action, actor FROM admin_audit "
                      "ORDER BY id DESC LIMIT 4")
        self.assertEqual([(r["action"], r["actor"]) for r in rows[:2]],
                         [("logout", "passkey:yubikey-5c"),
                          ("login", "passkey:yubikey-5c")])
        self.assertEqual(rows[3]["actor"], "token")       # the bootstrap

    def test_token_sessions_are_named_token(self):
        self._token_login()
        self.client.post("/admin/console/logout", follow_redirects=False)
        rows = _admin("SELECT action, actor FROM admin_audit "
                      "ORDER BY id DESC LIMIT 2")
        self.assertEqual({r["actor"] for r in rows}, {"token"})


class StepUpTests(_Base):
    """restore / reset / uninstall reach the ROOT host-agent. A 4-hour
    cookie must not be enough on its own."""

    def setUp(self):
        super().setUp()
        self.state = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.state.name, "cmd"), exist_ok=True)
        os.environ["OIKONOME_STATE_DIR"] = self.state.name
        import oikonome.web.adminconsole as ac
        ac._CMD_NONCES.clear()

    def tearDown(self):
        self.state.cleanup()
        super().tearDown()

    def _arm(self, command="reset"):
        r = self.client.post("/admin/console/run-command",
                             data={"command": command})
        self.assertEqual(r.status_code, 200, r.text)
        m = re.search(r'name="nonce" value="([^"]+)"', r.text)
        self.assertIsNotNone(m, "arming must mint a confirmation nonce")
        return m.group(1)

    def _queued(self) -> bool:
        return os.path.exists(os.path.join(self.state.name, "cmd",
                                           "command-requested"))

    def test_confirmation_only_runs_the_command_it_was_armed_for(self):
        """No passkey enrolled — the self-host case, where arm+confirm is
        the ONLY server-side friction before the root host-agent. A nonce
        armed for `reset` must not authorize `uninstall`: otherwise the
        typed confirmation locks in nothing and a stale form or a client
        bug runs the wrong wipe."""
        self._token_login()
        nonce = self._arm("reset")
        r = self.client.post("/admin/console/run-command", data={
            "command": "uninstall", "stage": "go", "nonce": nonce,
            "confirm": "uninstall"}, follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("that confirmation was for", r.text)
        self.assertFalse(self._queued(),
                         "an uninstall rode a reset's confirmation")
        self.assertIn("run_command_refused",
                      [x["action"] for x in _admin(
                          "SELECT action FROM admin_audit "
                          "ORDER BY id DESC LIMIT 3")])
        # and the mismatch BURNS the nonce — it is single-use whichever
        # command it arrived attached to
        r = self.client.post("/admin/console/run-command", data={
            "command": "reset", "stage": "go", "nonce": nonce,
            "confirm": "reset"}, follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("expired or", r.text)
        self.assertFalse(self._queued())

    def test_matching_confirmation_still_runs_without_a_passkey(self):
        # the positive half: nothing above may cost the passkey-less
        # self-host its own recovery tools
        self._token_login()
        nonce = self._arm("reset")
        r = self.client.post("/admin/console/run-command", data={
            "command": "reset", "stage": "go", "nonce": nonce,
            "confirm": "reset"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertTrue(self._queued())

    def test_destructive_command_refused_without_a_fresh_assertion(self):
        self._token_login()
        self._enrol()
        nonce = self._arm("reset")
        r = self.client.post("/admin/console/run-command", data={
            "command": "reset", "stage": "go", "nonce": nonce,
            "confirm": "reset"}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("fresh passkey confirmation", r.text)
        self.assertFalse(self._queued(), "nothing may reach the host agent")
        self.assertIn("run_command_refused",
                      [x["action"] for x in _admin(
                          "SELECT action FROM admin_audit "
                          "ORDER BY id DESC LIMIT 3")])

    def test_destructive_command_runs_with_one(self):
        self._token_login()
        self._enrol()
        nonce = self._arm("reset")
        ticket = self._ticket()
        r = self.client.post("/admin/console/run-command", data={
            "command": "reset", "stage": "go", "nonce": nonce,
            "confirm": "reset", "stepup": ticket}, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertTrue(self._queued())

    def test_ticket_is_single_use(self):
        self._token_login()
        self._enrol()
        ticket = self._ticket()
        nonce = self._arm("reset")
        r = self.client.post("/admin/console/run-command", data={
            "command": "reset", "stage": "go", "nonce": nonce,
            "confirm": "reset", "stepup": ticket}, follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        os.remove(os.path.join(self.state.name, "cmd", "command-requested"))
        # replaying the SAME ticket on a second command must not work
        nonce2 = self._arm("uninstall")
        r = self.client.post("/admin/console/run-command", data={
            "command": "uninstall", "stage": "go", "nonce": nonce2,
            "confirm": "uninstall", "stepup": ticket}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("fresh passkey confirmation", r.text)
        self.assertFalse(self._queued())

    def test_ticket_is_bound_to_the_session_that_minted_it(self):
        self._token_login()
        self._enrol()
        ticket = self._ticket()
        # a SECOND operator session (e.g. a stolen cookie elsewhere) may not
        # spend a ticket minted by the first
        other = TestClient(self.appmod.app)
        r = other.post("/admin/console/login", data={"token": TOKEN},
                       follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        nonce_r = other.post("/admin/console/run-command",
                             data={"command": "reset"})
        nonce = re.search(r'name="nonce" value="([^"]+)"', nonce_r.text).group(1)
        r = other.post("/admin/console/run-command", data={
            "command": "reset", "stage": "go", "nonce": nonce,
            "confirm": "reset", "stepup": ticket}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("fresh passkey confirmation", r.text)
        self.assertFalse(self._queued())

    def test_expired_ticket_is_refused(self):
        self._token_login()
        self._enrol()
        ticket = self._ticket()
        a = tenancy.admin_connect()
        try:
            a.execute("UPDATE admin_challenges SET expires_at = now() "
                      "- interval '1 minute' WHERE purpose = 'ticket'")
        finally:
            a.close()
        nonce = self._arm("reset")
        r = self.client.post("/admin/console/run-command", data={
            "command": "reset", "stage": "go", "nonce": nonce,
            "confirm": "reset", "stepup": ticket}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self._queued())

    def test_non_destructive_commands_never_ask(self):
        self._token_login()
        self._enrol()
        r = self.client.post("/admin/console/run-command",
                             data={"command": "backup"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertTrue(self._queued())

    def test_upgrade_is_destructive_and_refused_without_a_fresh_assertion(self):
        """Rebuild / restart tears the whole stack down and back up from
        the checkout — one POST from a stolen cookie must not do that. It
        arms like restore/reset/uninstall and needs the nonce, the typed
        word and a fresh passkey assertion."""
        self._token_login()
        self._enrol()
        r = self.client.post("/admin/console/run-command",
                             data={"command": "upgrade"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self._queued(), "one click must only ARM upgrade")
        m = re.search(r'name="nonce" value="([^"]+)"', r.text)
        self.assertIsNotNone(m)
        self.assertIn("Rebuild / restart tears the stack down", r.text)
        self.assertIn('data-admin-stepup="upgrade"', r.text)
        r = self.client.post("/admin/console/run-command", data={
            "command": "upgrade", "stage": "go", "nonce": m.group(1),
            "confirm": "upgrade"}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("fresh passkey confirmation", r.text)
        self.assertFalse(self._queued())
        nonce = self._arm("upgrade")
        r = self.client.post("/admin/console/run-command", data={
            "command": "upgrade", "stage": "go", "nonce": nonce,
            "confirm": "upgrade", "stepup": self._ticket()},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertTrue(self._queued())

    def _arm_reboot(self):
        # host-reboot sits in a tight 5/hour bucket; the arm→refuse→arm
        # sequence below is more posts than that
        from oikonome.web import security
        security._limiter._hits.clear()
        r = self.client.post("/admin/console/host-reboot",
                             data={"stage": "arm"})
        self.assertEqual(r.status_code, 200, r.text)
        m = re.search(r'name="nonce" value="([^"]+)"', r.text)
        self.assertIsNotNone(m, "arming must mint a reboot nonce")
        return m.group(1), r.text

    def _reboot_flagged(self) -> bool:
        return os.path.exists(os.path.join(self.state.name, "cmd",
                                           "reboot-requested"))

    def test_host_reboot_needs_typed_confirm_and_a_fresh_assertion(self):
        """A reboot takes every instance on the box down, so the go leg
        needs the typed word and, once a key is enrolled, a fresh passkey
        assertion — the same gates as the destructive host commands. A
        nonce alone would let a stolen cookie arm and go in two POSTs."""
        self._token_login()
        self._enrol()
        nonce, page = self._arm_reboot()
        self.assertIn('data-admin-stepup="reboot"', page)
        self.assertIn('name="confirm"', page)
        # nonce alone: refused before the flag is written
        r = self.client.post("/admin/console/host-reboot", data={
            "stage": "go", "nonce": nonce}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("type “reboot” to confirm", r.text)
        self.assertFalse(self._reboot_flagged())
        # nonce + typed word, no assertion: refused and audited
        nonce, _ = self._arm_reboot()
        r = self.client.post("/admin/console/host-reboot", data={
            "stage": "go", "nonce": nonce, "confirm": "reboot"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("fresh passkey confirmation", r.text)
        self.assertFalse(self._reboot_flagged())
        self.assertIn("host_reboot_refused",
                      [x["action"] for x in _admin(
                          "SELECT action FROM admin_audit "
                          "ORDER BY id DESC LIMIT 3")])
        # all three: the flag is written
        nonce, _ = self._arm_reboot()
        r = self.client.post("/admin/console/host-reboot", data={
            "stage": "go", "nonce": nonce, "confirm": "reboot",
            "stepup": self._ticket()}, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertTrue(self._reboot_flagged())
        _admin("DELETE FROM broadcast WHERE id = 1 RETURNING id")

    def test_host_reboot_without_a_passkey_needs_only_the_typed_word(self):
        # no key enrolled: same relaxation as the destructive commands
        self._token_login()
        nonce, page = self._arm_reboot()
        self.assertNotIn("data-admin-stepup", page)
        r = self.client.post("/admin/console/host-reboot", data={
            "stage": "go", "nonce": nonce, "confirm": "reboot"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertTrue(self._reboot_flagged())
        _admin("DELETE FROM broadcast WHERE id = 1 RETURNING id")

    def test_self_host_without_a_passkey_behaves_exactly_as_before(self):
        # no credential enrolled → no step-up, or an install that cannot
        # enrol one (plain http) would lose its own recovery tools
        self._token_login()
        nonce = self._arm("reset")
        r = self.client.post("/admin/console/run-command", data={
            "command": "reset", "stage": "go", "nonce": nonce,
            "confirm": "reset"}, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertTrue(self._queued())

    def test_removing_a_key_needs_a_fresh_assertion_too(self):
        # otherwise a stolen cookie downgrades the console back to
        # token-only by deleting the keys
        self._token_login()
        cred = self._enrol()
        r = self.client.post("/admin/console/passkey/delete",
                             data={"cred_id": cred["id"]},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("confirm with your passkey", r.text)
        self.assertEqual(len(_admin("SELECT 1 FROM admin_credentials")), 1)
        r = self.client.post("/admin/console/passkey/delete",
                             data={"cred_id": cred["id"],
                                   "stepup": self._ticket()},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        self.assertEqual(_admin("SELECT 1 FROM admin_credentials"), [])

    def test_last_key_cannot_be_removed_while_the_flag_is_set(self):
        self._token_login()
        cred = self._enrol()
        ticket = self._ticket()
        os.environ["OIKONOME_ADMIN_REQUIRE_PASSKEY"] = "1"
        r = self.client.post("/admin/console/passkey/delete",
                             data={"cred_id": cred["id"], "stepup": ticket},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("only operator passkey", r.text)
        self.assertEqual(len(_admin("SELECT 1 FROM admin_credentials")), 1)


# ---- layer 3: Cloudflare Access -------------------------------------------


def _rsa_jwk():
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    n = key.public_key().public_numbers()

    def b(i):
        raw = i.to_bytes((i.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return key, {"kid": "k1", "kty": "RSA", "alg": "RS256",
                 "n": b(n.n), "e": b(n.e)}


def _mint(key, claims, header=None):
    import json as _json
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    def seg(d):
        return base64.urlsafe_b64encode(
            _json.dumps(d).encode()).rstrip(b"=").decode()

    signing = f"{seg(header or {'alg': 'RS256', 'kid': 'k1'})}.{seg(claims)}"
    sig = key.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing + "." + base64.urlsafe_b64encode(sig).rstrip(b"=").decode()


class CloudflareAccessTests(_Base):
    DOMAIN = "oiktest.cloudflareaccess.com"
    AUD = "aud-" + "a" * 40

    def setUp(self):
        super().setUp()
        from oikonome.web import cfaccess
        self.key, jwk = _rsa_jwk()
        os.environ["OIKONOME_CF_ACCESS_TEAM"] = "oiktest"
        os.environ["OIKONOME_CF_ACCESS_AUD"] = self.AUD
        # seed the JWKS cache so nothing reaches the network
        cfaccess._JWKS_CACHE[self.DOMAIN] = (time.time(), [jwk])
        self.cf = cfaccess

    def tearDown(self):
        self.cf._JWKS_CACHE.clear()
        super().tearDown()

    def _claims(self, **over):
        now = int(time.time())
        c = {"iss": f"https://{self.DOMAIN}", "aud": [self.AUD],
             "exp": now + 600, "iat": now, "email": "ops@example.dev"}
        c.update(over)
        return c

    def test_team_domain_accepts_the_three_shapes_operators_paste(self):
        for raw in ("oiktest", "oiktest.cloudflareaccess.com",
                    "https://oiktest.cloudflareaccess.com/"):
            os.environ["OIKONOME_CF_ACCESS_TEAM"] = raw
            self.assertEqual(self.cf.team_domain(), self.DOMAIN)

    def test_good_token_verifies(self):
        c = self.cf.verify(_mint(self.key, self._claims()))
        self.assertEqual(c["email"], "ops@example.dev")

    def test_wrong_audience_issuer_and_expiry_are_refused(self):
        bad = [self._claims(aud=["someone-elses-app"]),
               self._claims(iss="https://evil.cloudflareaccess.com"),
               self._claims(exp=int(time.time()) - 3600)]
        for claims in bad:
            with self.assertRaises(ValueError):
                self.cf.verify(_mint(self.key, claims))

    def test_forged_signature_is_refused(self):
        other, _ = _rsa_jwk()                 # signed by a key we don't trust
        with self.assertRaises(ValueError):
            self.cf.verify(_mint(other, self._claims()))

    def test_alg_none_is_refused(self):
        # the classic JWT forgery: strip the signature, claim no algorithm
        with self.assertRaises(ValueError):
            self.cf.verify(_mint(self.key, self._claims(),
                                 header={"alg": "none"}))

    def test_origin_direct_request_is_cloaked(self):
        # no assertion at all — the whole point of verifying at the origin
        self.assertEqual(self.client.get("/admin/console").status_code, 404)
        self.assertEqual(
            self.client.post("/admin/console/login",
                             data={"token": TOKEN}).status_code, 404)
        # a forged one is no better
        forged, _ = _rsa_jwk()
        self.assertEqual(self.client.get("/admin/console", headers={
            "Cf-Access-Jwt-Assertion": _mint(forged, self._claims())
        }).status_code, 404)

    def test_good_assertion_reaches_the_console(self):
        r = self.client.get("/admin/console", headers={
            "Cf-Access-Jwt-Assertion": _mint(self.key, self._claims())})
        self.assertEqual(r.status_code, 200)
        # the cookie form Cloudflare sets on a browser navigation works too
        self.client.cookies.set("CF_Authorization",
                                _mint(self.key, self._claims()))
        self.assertEqual(self.client.get("/admin/console").status_code, 200)

    def test_unconfigured_is_inert(self):
        os.environ.pop("OIKONOME_CF_ACCESS_TEAM", None)
        os.environ.pop("OIKONOME_CF_ACCESS_AUD", None)
        self.assertFalse(self.cf.enabled())
        self.assertEqual(self.client.get("/admin/console").status_code, 200)

    def test_jwks_failure_fails_closed(self):
        self.cf._JWKS_CACHE.clear()
        import httpx
        with mock.patch.object(httpx, "get",
                               side_effect=httpx.ConnectError("down")):
            with self.assertRaises(Exception):
                self.cf.verify(_mint(self.key, self._claims()))
            self.assertEqual(self.client.get("/admin/console").status_code,
                             404)


class UnchangedInvariantTests(_Base):
    def test_console_still_404s_when_disabled(self):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)
        self.assertEqual(self.client.get("/admin/console").status_code, 404)
        for path in ("/admin/console/passkey/login/options",
                     "/admin/console/passkey/options",
                     "/admin/console/passkey/stepup/options"):
            self.assertEqual(self.client.post(path, json={}).status_code, 404,
                             path)

    def test_token_only_self_host_is_unchanged(self):
        self._token_login()
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 200)
        self.assertIn("No operator passkey enrolled", r.text)

    def test_session_ttl_is_four_hours(self):
        import oikonome.web.adminconsole as ac
        self.assertEqual(ac.SESSION_TTL, dt.timedelta(hours=4))

    def test_app_role_has_no_grants_on_the_new_tables(self):
        import psycopg
        rows = _admin(
            """SELECT table_name, privilege_type
               FROM information_schema.role_table_grants
               WHERE grantee = 'oikonome_app'
                 AND table_name IN ('admin_credentials', 'admin_challenges')""")
        self.assertEqual(rows, [])
        # and prove it end to end, not just from the catalog
        with psycopg.connect(tenancy.APP_DSN, autocommit=True) as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT * FROM admin_credentials")


if __name__ == "__main__":
    unittest.main()


class EnrolTicketWithLiveSessionTests(_Base):
    def test_a_host_ticket_enrols_even_when_the_browser_holds_a_session(self):
        """The lost-key recovery has to work in the browser you are sitting
        in. `by_ticket` must not require `sess is None`; otherwise a console
        cookie skips the ticket path and falls through to the step-up branch,
        which demands a passkey you already hold — exactly the one that is lost.
        A ticket needs shell on the host, which outranks a console session,
        and it is single-use and short-lived."""
        from oikonome.auth import admin_passkeys
        self.client = TestClient(self.appmod.app, base_url="http://localhost")
        self._token_login()
        self._enrol()                                  # a key now exists
        admin = tenancy.admin_connect()
        try:
            ticket = admin_passkeys.mint_enrol_ticket(admin, 15)
        finally:
            admin.close()
        raw2 = b"\xf0\x0d" + uuid.uuid4().bytes       # a DIFFERENT key
        b64_2 = base64.urlsafe_b64encode(raw2).rstrip(b"=").decode()

        class _Reg2:
            credential_id = raw2
            credential_public_key = b"\x09\x0a\x0b\x0c"
            sign_count = 0

        o = self.client.post("/admin/console/passkey/options",
                             json={"label": "replacement"})
        self.assertEqual(o.status_code, 200, o.text)
        with mock.patch("oikonome.auth.admin_passkeys."
                        "verify_registration_response", return_value=_Reg2()):
            r = self.client.post("/admin/console/passkey", json={
                "challenge_id": o.json()["challenge_id"],
                "label": "replacement", "ticket": ticket,
                "credential": {"id": b64_2, "rawId": b64_2,
                               "response": {"transports": ["usb"]}}})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_session_without_a_ticket_still_needs_the_stepup(self):
        self.client = TestClient(self.appmod.app, base_url="http://localhost")
        self._token_login()
        self._enrol()
        o = self.client.post("/admin/console/passkey/options",
                             json={"label": "second"})
        r = self.client.post("/admin/console/passkey", json={
            "challenge_id": o.json()["challenge_id"], "label": "second",
            "credential": {"id": CRED_B64, "rawId": CRED_B64,
                           "response": {"transports": ["usb"]}}})
        self.assertEqual(r.status_code, 403, r.text)
