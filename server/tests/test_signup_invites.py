"""invite-gated hosted signup + email verification.

OIKONOME_HOSTED alone would open /api/signup to the whole internet, so a
hosted instance requires an operator-minted invite (single-use, expiring,
bound to one email) unless OIKONOME_OPEN_SIGNUP explicitly opens it.
Hosted accounts start unverified and verify through an emailed one-time
link (password_resets token discipline); self-host/DEV signups stay
verified so nothing nags them. The app role can read+burn invites but
never mint (admin-only INSERT), and verified_at is written on the admin
connection only (column scope stays closed).
"""

import os
import unittest
import uuid

import psycopg
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from oikonome.auth import email_verify, signup_invites
from oikonome.db import tenancy

from .util import _ensure_db

INVITE_403 = "signup is by invitation"


def _mint(email, **kw):
    admin = tenancy.admin_connect()
    try:
        return signup_invites.mint(admin, email, **kw)
    finally:
        admin.close()


def _expire(token):
    """Age a minted invite past its expiry; returns the same raw token."""
    admin = tenancy.admin_connect()
    try:
        admin.execute(
            "UPDATE signup_invites SET expires_at = now() - interval '1 day' "
            "WHERE token_hash = %s", (signup_invites._h(token),))
        return token
    finally:
        admin.close()


def _user_row(email):
    admin = tenancy.admin_connect()
    try:
        return admin.execute(
            "SELECT id, tenant_id, verified_at FROM users WHERE email=%s",
            (email,)).fetchone()
    finally:
        admin.close()


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)

    def setUp(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        from oikonome.web import security
        security._limiter._hits.clear()          # 5/h signup bucket

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        os.environ.pop("OIKONOME_OPEN_SIGNUP", None)

    def _signup(self, email, invite=""):
        return TestClient(self.client.app).post(
            "/api/signup", data={"email": email,
                                 "password": "correct-horse-battery",
                                 "invite": invite})


class InviteGateTests(_Base):
    def test_hosted_signup_without_invite_403(self):
        r = self._signup(f"gate-{uuid.uuid4().hex[:8]}@x.dev")
        self.assertEqual(r.status_code, 403)
        self.assertIn(INVITE_403, r.json()["detail"])

    def test_bogus_expired_and_wrong_email_all_read_identically(self):
        email = f"gate-{uuid.uuid4().hex[:8]}@x.dev"
        # aged by the clock, not minted dead: `mint` bounds its own `days`,
        # so a link that is already expired the moment it is issued is a
        # state the operator can no longer create
        expired = _expire(_mint(email))
        other = _mint(f"other-{uuid.uuid4().hex[:8]}@x.dev")
        msgs = set()
        for tok in ("not-a-token", expired, other):
            r = self._signup(email, invite=tok)
            self.assertEqual(r.status_code, 403)
            msgs.add(r.json()["detail"])
        self.assertEqual(len(msgs), 1)           # one generic message

    def test_valid_invite_admits_and_burns(self):
        email = f"invitee-{uuid.uuid4().hex[:8]}@x.dev"
        token = _mint(email, note="test invite")
        r = self._signup(email, invite=token)
        self.assertEqual(r.status_code, 200)
        u = _user_row(email)
        self.assertIsNone(u["verified_at"])      # hosted → verify by email
        admin = tenancy.admin_connect()
        try:
            inv = admin.execute(
                "SELECT used_at, used_by_tenant FROM signup_invites "
                "WHERE email=%s ORDER BY created_at DESC LIMIT 1",
                (email,)).fetchone()
            nver = admin.execute(
                "SELECT COUNT(*) AS n FROM email_verifications "
                "WHERE user_id=%s", (u["id"],)).fetchone()["n"]
        finally:
            admin.close()
        self.assertIsNotNone(inv["used_at"])
        self.assertEqual(inv["used_by_tenant"], u["tenant_id"])
        self.assertEqual(nver, 1)                # verification minted
        # single use: the same invite admits nobody else
        r2 = self._signup(email + ".again", invite=token)
        self.assertEqual(r2.status_code, 403)


    def test_reinvite_burns_the_older_link(self):
        email = f"invitee-{uuid.uuid4().hex[:8]}@x.dev"
        old = _mint(email)
        _mint(email)                              # resend
        r = self._signup(email, invite=old)
        self.assertEqual(r.status_code, 403)      # only the newest works

    def test_the_invite_lifetime_is_bounded_at_the_mint_itself(self):
        """`days` becomes timedelta(days=days) and arrives from whatever an
        operator typed — a console form field or the CLI's `--days`. Zero or
        negative mints a link that is already expired, which the operator
        cannot tell from a working one; past timedelta's ~2.7-million-day
        ceiling it raises OverflowError. The bound belongs to `mint`, so
        that every door inherits it rather than the one written last."""
        admin = tenancy.admin_connect()
        try:
            for days in (0, -30, 10 ** 12):
                email = f"bound-{uuid.uuid4().hex[:8]}@x.dev"
                token = signup_invites.mint(admin, email, days=days)
                row = admin.execute(
                    """SELECT expires_at - now() AS left FROM signup_invites
                       WHERE token_hash = %s""",
                    (signup_invites._h(token),)).fetchone()
                # inside the range, never in the past and never overflowed
                self.assertGreater(row["left"].total_seconds(), 0,
                                   f"days={days}")
                self.assertLessEqual(row["left"].days, 3650, f"days={days}")
        finally:
            admin.close()

    def test_the_cli_reports_the_lifetime_it_minted_not_the_one_typed(self):
        """The clamp above makes the typed number the wrong thing to echo:
        `--days 999999` mints a 3650-day invite and `--days 0` a one-day
        one, so printing the raw value tells the operator a lifetime the
        invite does not have — and "already dead" about a link that works.
        The confirmation line must name what was minted."""
        import contextlib
        import io

        from oikonome.cli import mint_invite
        for typed, said in ((10 ** 6, 3650), (0, 1), (14, 14)):
            email = f"cliexp-{uuid.uuid4().hex[:8]}@x.dev"
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), \
                 contextlib.redirect_stderr(err):
                mint_invite(email, typed, None)
            self.assertIn(f"expires in {said} days", err.getvalue(),
                          f"typed --days {typed}")
            # and the line is true: that is the row's real lifetime
            admin = tenancy.admin_connect()
            try:
                row = admin.execute(
                    "SELECT expires_at - now() AS left FROM signup_invites "
                    "WHERE email = %s", (email,)).fetchone()
            finally:
                admin.close()
            # ceil: the row aged a few microseconds since it was written
            self.assertEqual(row["left"].days + 1, said, f"typed {typed}")

    def test_open_signup_env_lifts_the_gate(self):
        os.environ["OIKONOME_OPEN_SIGNUP"] = "1"
        email = f"open-{uuid.uuid4().hex[:8]}@x.dev"
        r = self._signup(email)
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(_user_row(email)["verified_at"])  # still verifies

    def test_selfhost_dev_signup_unchanged_and_verified(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        email = f"dev-{uuid.uuid4().hex[:8]}@x.dev"
        r = self._signup(email)
        self.assertEqual(r.status_code, 200)
        self.assertIsNotNone(_user_row(email)["verified_at"])

    def test_app_role_cannot_mint_invites(self):
        with psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                             autocommit=True) as conn:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    "INSERT INTO signup_invites (token_hash, email, "
                    "expires_at) VALUES ('x', 'x@x', now())")


class VerifyEmailTests(_Base):
    def _hosted_user(self):
        email = f"vfy-{uuid.uuid4().hex[:8]}@x.dev"
        token = _mint(email)
        anon = TestClient(self.client.app)
        r = anon.post("/api/signup",
                      data={"email": email,
                            "password": "correct-horse-battery",
                            "invite": token})
        assert r.status_code == 200, r.text
        return email, anon

    def test_me_exposes_created_at_for_banner_grace(self):
        # /api/me carries created_at (ISO) so the SPA can hold the
        # verify banner until the account is ≥3 days old
        _email, session = self._hosted_user()
        me = session.get("/api/me").json()
        self.assertFalse(me["verified"])
        self.assertIsNotNone(me["created_at"])
        # a fresh signup is well under the 3-day grace → banner suppressed
        import datetime as _dt
        age = _dt.datetime.now(_dt.timezone.utc) - _dt.datetime.fromisoformat(
            me["created_at"])
        self.assertLess(age.total_seconds(), 3 * 24 * 3600)

    def test_link_verifies_once(self):
        email, session = self._hosted_user()
        self.assertFalse(session.get("/api/me").json()["verified"])
        u = _user_row(email)
        admin = tenancy.admin_connect()
        try:
            vtoken = email_verify.create(admin, u["id"])
        finally:
            admin.close()
        # The click in the mail is the whole task: the
        # GET verifies, says so in place, and forwards to sign-in.
        r = session.get(f"/verify-email?token={vtoken}",
                        follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Email confirmed", r.text)
        self.assertIn("/login?verified=1", r.headers.get("refresh", ""))
        self.assertIsNotNone(_user_row(email)["verified_at"])
        self.assertTrue(session.get("/api/me").json()["verified"])
        # a second fetch of the same link (a scanner before, or the human
        # after) is not told "already used" — the address IS verified
        r2 = session.get(f"/verify-email?token={vtoken}",
                         follow_redirects=False)
        self.assertEqual(r2.status_code, 200)
        self.assertIn("Email confirmed", r2.text)
        # while a token that never verified anyone still refuses: see
        # test_link_prefetch

    def test_garbage_token_400(self):
        r = TestClient(self.client.app).get("/verify-email?token=nope")
        self.assertEqual(r.status_code, 400)

    def test_resend_mints_and_burns_prior(self):
        email, session = self._hosted_user()
        u = _user_row(email)
        r = session.post("/api/verify-email/resend")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["verified"])
        admin = tenancy.admin_connect()
        try:
            live = admin.execute(
                "SELECT COUNT(*) AS n FROM email_verifications "
                "WHERE user_id=%s AND used_at IS NULL",
                (u["id"],)).fetchone()["n"]
        finally:
            admin.close()
        self.assertEqual(live, 1)                 # newest only

    def test_resend_after_verified_reports_verified(self):
        email, session = self._hosted_user()
        admin = tenancy.admin_connect()
        try:
            vtoken = email_verify.create(admin, _user_row(email)["id"])
        finally:
            admin.close()
        session.post("/verify-email", data={"token": vtoken},
                     follow_redirects=False)
        r = session.post("/api/verify-email/resend")
        self.assertTrue(r.json()["verified"])


class HostedRootRoutingTests(_Base):
    """A hosted instance with no account must not route visitors into the
    self-host /setup token wizard (which 403s) — root and /setup send
    them to /login; invite recipients use their direct /signup link."""

    def test_root_goes_to_login_not_setup(self):
        r = TestClient(self.client.app).get("/", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")

    def test_setup_bounces_to_login(self):
        r = TestClient(self.client.app).get("/setup", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/login")


class SignupPageTests(_Base):
    def test_page_renders_with_prefill(self):
        r = TestClient(self.client.app).get(
            "/signup?invite=abc&email=a%40b.dev")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Create your account", r.text)
        self.assertIn("abc", r.text)

    def test_form_flow_signs_up_and_redirects(self):
        email = f"form-{uuid.uuid4().hex[:8]}@x.dev"
        token = _mint(email)
        anon = TestClient(self.client.app)
        r = anon.post("/signup",
                      data={"email": email,
                            "password": "correct-horse-battery",
                            "invite": token},
                      follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/app/welcome")
        self.assertEqual(anon.get("/api/me").json()["email"], email)

    def test_form_error_rerenders_with_message(self):
        r = TestClient(self.client.app).post(
            "/signup", data={"email": f"x-{uuid.uuid4().hex[:6]}@x.dev",
                             "password": "correct-horse-battery",
                             "invite": "wrong"})
        self.assertEqual(r.status_code, 403)
        self.assertIn(INVITE_403, r.text)


if __name__ == "__main__":
    unittest.main()
