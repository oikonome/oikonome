"""A second factor guessed at a step-up door is counted — against the
session that guessed it, never against the account's sign-in lock.

The per-account lock (15 wrong second factors in 15 minutes) is what caps
a distributed brute force of the six-digit code at /api/login. Every
account-security door that takes the same code — re-binding TOTP,
elevating the session, the per-request legacy step-up — is a place to
guess it from a stolen session. If those doors counted a miss only as an
IP strike, the guesses would just move there, IP-rotated, with nothing
capping them. So each miss there counts, and a session that has spent its
guesses takes no code at those doors — not even the right one.

But the count must not land in the account's login lock. That lock is
checked before sign-in verifies anything, so a session able to fill it
could keep the real owner out of sign-in, out of elevation in their own
session and out of a coded reset, for as long as it kept guessing. The
step-up bucket is keyed by user AND credential: it locks only the session
that guessed.
"""

import os
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, clear_totp_burn

PW = "correct-horse-battery"


def _fails(email: str) -> int:
    from oikonome.web import security
    return security._dual("count", security._acct_key(email),
                          security._ACCT_FAIL_WINDOW)


def _lock(email: str) -> None:
    from oikonome.web import security
    for _ in range(security._ACCT_FAIL_MAX):
        security.record_login_failure(email)


def _user_id(email: str) -> str:
    admin = tenancy.admin_connect()
    try:
        return str(admin.execute("SELECT id FROM users WHERE email=%s",
                                 (email,)).fetchone()["id"])
    finally:
        admin.close()


def _session_key(client, email: str) -> tuple:
    import hashlib
    from oikonome.auth import sessions
    cookie = client.cookies.get(sessions.COOKIE_NAME)
    return ("stepup-fail", _user_id(email),
            hashlib.sha256(cookie.encode()).hexdigest()[:32])


def _session_fails(client, email: str) -> int:
    from oikonome.web import security
    return security._dual("count", _session_key(client, email),
                          security._STEP_UP_FAIL_WINDOW)


def _lock_session(client, email: str) -> None:
    from oikonome.web import security
    for _ in range(security._STEP_UP_FAIL_MAX):
        security._dual("add", _session_key(client, email),
                       security._STEP_UP_FAIL_WINDOW)


class _Base(unittest.TestCase):
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

    def _totp_user(self):
        from oikonome.auth import totp as totp_mod
        client = TestClient(self.app)
        email = f"lock-{uuid.uuid4().hex[:10]}@example.dev"
        r = client.post("/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        secret = client.post("/api/totp/enroll",
                             data={"password": PW}).json()["secret"]
        r = client.post("/api/totp/confirm", data={
            "secret": secret, "code": totp_mod.code_now(secret),
            "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        clear_totp_burn()
        return client, email, secret


class TotpDoorTests(_Base):

    def test_wrong_current_code_at_totp_rebind_counts_toward_the_session_lock(
            self):
        from oikonome.auth import totp as totp_mod
        c, email, _ = self._totp_user()
        new = totp_mod.new_secret()
        before, acct = _session_fails(c, email), _fails(email)
        r = c.post("/api/totp/confirm", data={
            "secret": new, "code": totp_mod.code_now(new),
            "password": PW, "current_code": "000000"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(_session_fails(c, email), before + 1)
        self.assertEqual(_fails(email), acct)

    def test_wrong_current_code_at_totp_enroll_counts_toward_the_session_lock(
            self):
        c, email, _ = self._totp_user()
        before, acct = _session_fails(c, email), _fails(email)
        r = c.post("/api/totp/enroll",
                   data={"password": PW, "current_code": "000000"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(_session_fails(c, email), before + 1)
        self.assertEqual(_fails(email), acct)

    def test_legacy_rebind_takes_no_code_without_the_password(self):
        """A bare cookie must not be able to guess the code at the legacy
        re-enroll / re-bind branch: the password is checked first, so the
        code is never tried and nothing is counted against the factor."""
        from oikonome.auth import totp as totp_mod
        c, email, secret = self._totp_user()
        before = _session_fails(c, email)
        r = c.post("/api/totp/enroll", data={"current_code": "000000"})
        self.assertEqual(r.status_code, 401, r.text)
        r = c.post("/api/totp/enroll",
                   data={"current_code": totp_mod.code_now(secret)})
        self.assertEqual(r.status_code, 401, r.text)
        new = totp_mod.new_secret()
        r = c.post("/api/totp/confirm", data={
            "secret": new, "code": totp_mod.code_now(new),
            "current_code": totp_mod.code_now(secret)})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(_session_fails(c, email), before)

    def test_a_locked_session_cannot_rebind_totp_even_with_the_right_code(
            self):
        from oikonome.auth import totp as totp_mod
        c, email, secret = self._totp_user()
        _lock_session(c, email)
        new = totp_mod.new_secret()
        r = c.post("/api/totp/confirm", data={
            "secret": new, "code": totp_mod.code_now(new),
            "password": PW, "current_code": totp_mod.code_now(secret)})
        self.assertEqual(r.status_code, 429, r.text)
        # the old authenticator is still the one bound
        admin = tenancy.admin_connect()
        try:
            from oikonome.db import crypto
            enc = admin.execute("SELECT totp_secret FROM users WHERE email=%s",
                                (email,)).fetchone()["totp_secret"]
        finally:
            admin.close()
        self.assertEqual(crypto.decrypt_cp(enc), secret)

    def test_wrong_code_at_elevate_counts_toward_the_session_lock(self):
        c, email, _ = self._totp_user()
        before, acct = _session_fails(c, email), _fails(email)
        r = c.post("/api/auth/elevate",
                   json={"password": PW, "totp_code": "000000"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(_session_fails(c, email), before + 1)
        self.assertEqual(_fails(email), acct)

    def test_a_locked_session_cannot_elevate_with_the_right_code(self):
        from oikonome.auth import totp as totp_mod
        c, email, secret = self._totp_user()
        _lock_session(c, email)
        r = c.post("/api/auth/elevate", json={
            "password": PW, "totp_code": totp_mod.code_now(secret)})
        self.assertEqual(r.status_code, 429, r.text)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])

    def test_a_junk_bearer_header_does_not_open_a_fresh_lock_bucket(self):
        """The bucket is keyed by the credential that AUTHENTICATED the
        request. An Authorization value the instance never issued is not a
        credential: a cookie holder who adds one (and varies it) must find
        the same lock, not a fresh one per request."""
        from oikonome.auth import totp as totp_mod
        c, email, secret = self._totp_user()
        _lock_session(c, email)
        r = c.post("/api/auth/elevate",
                   json={"password": PW, "totp_code": totp_mod.code_now(secret)},
                   headers={"Authorization": "Bearer not-a-token-we-issued"})
        self.assertEqual(r.status_code, 429, r.text)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])

    def test_a_login_lock_does_not_stop_the_owners_session_elevating(self):
        """Someone holding the leaked password can keep the sign-in lock
        full from outside. The owner's live session must still be able to
        elevate with the right code — that is how they rotate the password
        the attacker is using."""
        from oikonome.auth import totp as totp_mod
        c, email, secret = self._totp_user()
        _lock(email)
        r = c.post("/api/auth/elevate", json={
            "password": PW, "totp_code": totp_mod.code_now(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(c.get("/api/auth/elevation").json()["elevated"])

    def test_session_guesses_never_lock_sign_in_or_another_session(self):
        """A session that grinds the code until it is locked out of its own
        step-up doors leaves the account's sign-in, and the owner's other
        sessions, exactly as they were."""
        from oikonome.auth import totp as totp_mod
        from oikonome.web import security
        thief, email, secret = self._totp_user()
        def forget_route_budgets():
            # the elevate door's hourly budget is not what is under test;
            # the step-up bucket in the same table must survive
            for k in list(security._limiter._hits):
                if k[0] != "stepup-fail":
                    del security._limiter._hits[k]
        for _ in range(security._STEP_UP_FAIL_MAX):
            forget_route_budgets()
            r = thief.post("/api/auth/elevate",
                           json={"password": PW, "totp_code": "000000"})
            self.assertEqual(r.status_code, 401, r.text)
        forget_route_budgets()
        r = thief.post("/api/auth/elevate", json={
            "password": PW, "totp_code": totp_mod.code_now(secret)})
        self.assertEqual(r.status_code, 429, r.text)
        self.assertFalse(security.account_login_locked(email))
        clear_totp_burn()
        owner = TestClient(self.app)
        r = owner.post("/api/login", data={
            "email": email, "password": PW,
            "totp_code": totp_mod.code_now(secret)})
        self.assertEqual(r.status_code, 200, r.text)
        clear_totp_burn()
        r = owner.post("/api/auth/elevate", json={
            "password": PW, "totp_code": totp_mod.code_now(secret)})
        self.assertEqual(r.status_code, 200, r.text)

    def test_wrong_code_at_a_legacy_step_up_counts_toward_the_session_lock(
            self):
        c, email, _ = self._totp_user()
        before, acct = _session_fails(c, email), _fails(email)
        r = c.post("/api/totp/recovery-regenerate",
                   data={"password": PW, "totp_code": "000000"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(_session_fails(c, email), before + 1)
        self.assertEqual(_fails(email), acct)

    def test_an_empty_code_is_a_prompt_not_a_guess(self):
        c, email, _ = self._totp_user()
        before, acct = _session_fails(c, email), _fails(email)
        r = c.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(_session_fails(c, email), before)
        self.assertEqual(_fails(email), acct)


class PasskeyOnlyRecoveryTests(_Base):

    def setUp(self):
        super().setUp()
        os.environ["OIKONOME_HOSTED"] = "1"
        self.addCleanup(os.environ.pop, "OIKONOME_HOSTED", None)

    def _passkey_only_user(self):
        from oikonome.auth import passwords, recovery
        email = f"lockpk-{uuid.uuid4().hex[:8]}@example.dev"
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, email)
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "verified_at) VALUES (%s, %s, %s, now()) RETURNING id",
                (tid, email, passwords.hash_password(PW))).fetchone()["id"]
        finally:
            admin.close()
        client = TestClient(self.app)
        r = client.post("/login", data={"email": email, "password": PW},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO passkeys (user_id, credential_id, public_key, "
                "sign_count) VALUES (%s, %s, %s, 0)",
                (uid, f"cred{uuid.uuid4().hex}", "cHVibGljLWtleQ"))
            codes = recovery.issue(admin, uid)
        finally:
            admin.close()
        return client, email, codes

    def test_wrong_recovery_code_at_elevate_counts_toward_the_session_lock(
            self):
        c, email, _ = self._passkey_only_user()
        before, acct = _session_fails(c, email), _fails(email)
        r = c.post("/api/auth/elevate",
                   json={"password": PW, "recovery_code": "wrong-code-x"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(_session_fails(c, email), before + 1)
        self.assertEqual(_fails(email), acct)

    def test_wrong_password_beside_a_recovery_code_counts_toward_the_session_lock(  # noqa: E501
            self):
        """The password is the half of a recovery-code elevation a session
        thief lacks. Guessing it from inside the session must spend the
        same per-session budget a wrong code does — otherwise the two
        halves could be ground in turn — and must leave the code unspent
        and the sign-in lock untouched."""
        c, email, codes = self._passkey_only_user()
        before, acct = _session_fails(c, email), _fails(email)
        r = c.post("/api/auth/elevate",
                   json={"password": "not-the-password",
                         "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(_session_fails(c, email), before + 1)
        self.assertEqual(_fails(email), acct)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])
        r = c.post("/api/auth/elevate",
                   json={"password": PW, "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_recovery_code_alone_does_not_elevate_a_hosted_passkey_account(
            self):
        """On hosted, every other posture door of a passkey-only account
        wants the passkey itself. A recovery code is written down, so on
        its own it must not open the window for whoever holds a session
        and found the sheet; it goes with the password. Leaving the
        password out is a prompt, not a guess, and spends nothing."""
        c, email, codes = self._passkey_only_user()
        before = _session_fails(c, email)
        for body in ({"recovery_code": codes[0]},
                     {"password": "", "recovery_code": codes[0]}):
            r = c.post("/api/auth/elevate", json=body)
            self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("password_required", r.text)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])
        self.assertEqual(_session_fails(c, email), before)
        r = c.post("/api/auth/elevate",
                   json={"password": PW, "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["elevated"])

    def test_a_spent_session_is_still_prompted_for_the_password_beside_a_code(  # noqa: E501
            self):
        """Leaving the password out beside a recovery code is the prompt for
        it. A session that has spent its guesses must still be told a
        password is needed, spending nothing, while a real attempt is
        refused as locked."""
        c, email, codes = self._passkey_only_user()
        _lock_session(c, email)
        spent = _session_fails(c, email)
        r = c.post("/api/auth/elevate",
                   json={"password": "", "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("password_required", r.text)
        self.assertEqual(_session_fails(c, email), spent)
        r = c.post("/api/auth/elevate",
                   json={"password": PW, "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 429, r.text)

    def test_wrong_recovery_code_at_a_legacy_step_up_counts_toward_the_session_lock(  # noqa: E501
            self):
        c, email, _ = self._passkey_only_user()
        before, acct = _session_fails(c, email), _fails(email)
        r = c.post("/api/totp/recovery-regenerate",
                   data={"password": PW, "recovery_code": "wrong-code-x"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(_session_fails(c, email), before + 1)
        self.assertEqual(_fails(email), acct)

    def test_a_locked_session_cannot_elevate_with_a_real_recovery_code(self):
        c, email, codes = self._passkey_only_user()
        _lock_session(c, email)
        r = c.post("/api/auth/elevate",
                   json={"password": PW, "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 429, r.text)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])

    def test_a_login_lock_does_not_stop_a_session_elevating_with_a_real_code(
            self):
        c, email, codes = self._passkey_only_user()
        _lock(email)
        r = c.post("/api/auth/elevate",
                   json={"password": PW, "recovery_code": codes[0]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(c.get("/api/auth/elevation").json()["elevated"])


class PasswordGuessTests(_Base):
    """The password is the half of every step-up a cookie thief lacks, so a
    wrong one offered from inside a session spends the same per-session
    budget a wrong code does — at elevate, at the legacy per-request doors
    and at the doors that re-prove the password in their own body — and a
    locked session is refused before the password is even checked, so the
    responses stop answering "was that the password"."""

    def _password_user(self):
        client = TestClient(self.app)
        email = f"lockpw-{uuid.uuid4().hex[:10]}@example.dev"
        r = client.post("/api/signup", data={"email": email, "password": PW})
        self.assertEqual(r.status_code, 200, r.text)
        return client, email

    def test_wrong_password_at_elevate_counts_toward_the_session_lock(self):
        for c, email in (self._password_user(), self._totp_user()[:2]):
            before, acct = _session_fails(c, email), _fails(email)
            r = c.post("/api/auth/elevate", json={"password": "not-it"})
            self.assertEqual(r.status_code, 401, r.text)
            self.assertEqual(_session_fails(c, email), before + 1)
            self.assertEqual(_fails(email), acct)

    def test_a_locked_session_cannot_elevate_with_the_right_password(self):
        c, email = self._password_user()
        _lock_session(c, email)
        r = c.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 429, r.text)
        self.assertFalse(c.get("/api/auth/elevation").json()["elevated"])

    def test_a_locked_totp_session_learns_nothing_from_the_right_password(
            self):
        """Without the lock the right password answers totp_required and a
        wrong one password_required — an oracle for the password."""
        c, email, _ = self._totp_user()
        _lock_session(c, email)
        r = c.post("/api/auth/elevate", json={"password": PW})
        self.assertEqual(r.status_code, 429, r.text)

    def test_wrong_password_at_a_legacy_step_up_counts_toward_the_session_lock(  # noqa: E501
            self):
        c, email = self._password_user()
        before = _session_fails(c, email)
        r = c.post("/api/totp/recovery-regenerate",
                   data={"password": "not-it"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(_session_fails(c, email), before + 1)
        _lock_session(c, email)
        r = c.post("/api/totp/recovery-regenerate", data={"password": PW})
        self.assertEqual(r.status_code, 429, r.text)

    def test_in_body_password_doors_honour_the_session_lock(self):
        for path, data in (
                ("json:/api/password/change",
                 {"current_password": "not-it",
                  "new_password": "another-long-passphrase-9"}),
                ("/api/email/change",
                 {"new_email": "moved@example.dev", "password": "not-it"}),
                ("/api/account/delete", {"password": "not-it"})):
            with self.subTest(path=path):
                c, email = self._password_user()
                before, acct = _session_fails(c, email), _fails(email)
                def post(body):
                    if path.startswith("json:"):
                        return c.post(path[5:], json=body)
                    return c.post(path, data=body)
                r = post(data)
                self.assertEqual(r.status_code, 401, r.text)
                self.assertEqual(_session_fails(c, email), before + 1)
                self.assertEqual(_fails(email), acct)
                _lock_session(c, email)
                right = {k: (PW if v == "not-it" else v)
                         for k, v in data.items()}
                r = post(right)
                self.assertEqual(r.status_code, 429, r.text)

    def test_an_empty_password_is_a_prompt_not_a_guess(self):
        c, email = self._password_user()
        before = _session_fails(c, email)
        r = c.post("/api/totp/recovery-regenerate",
                   data={"recovery_code": "x"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(_session_fails(c, email), before)

    def test_a_spent_session_is_still_prompted_for_its_password(self):
        """An empty password is the prompt for one. Once the session has
        spent its guesses it must still be told a password is needed (and
        spend nothing asking), while a wrong one is refused as locked."""
        c, email = self._password_user()
        _lock_session(c, email)
        spent = _session_fails(c, email)
        r = c.post("/api/totp/recovery-regenerate",
                   data={"recovery_code": "x"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertIn("password_required", r.text)
        self.assertEqual(_session_fails(c, email), spent)
        r = c.post("/api/totp/recovery-regenerate",
                   data={"password": "not-it"})
        self.assertEqual(r.status_code, 429, r.text)


class ConcurrentGuessTests(_Base):
    """The session's guess budget holds under a burst, not only in
    sequence: each guess takes its slot before it is verified, so a burst
    of concurrent wrong guesses cannot all be verified by reading a count
    that none of them has added to yet."""

    BURST = 20

    def setUp(self):
        super().setUp()
        import threading
        self.verified = []
        self._lock = threading.Lock()

    def _slow_miss(self, *_a, **_k):
        """A wrong answer that takes as long as a real verify, to widen the
        window between reading the budget and spending it."""
        import time
        with self._lock:
            self.verified.append(1)
        time.sleep(0.05)
        return None

    def _burst(self, guess):
        import threading
        from fastapi import HTTPException
        from oikonome.web import security
        user_id = str(uuid.uuid4())
        refused = []
        gate = threading.Barrier(self.BURST)

        def one():
            gate.wait()
            try:
                guess(user_id)
            except HTTPException as e:
                refused.append(e.status_code)
        threads = [threading.Thread(target=one) for _ in range(self.BURST)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(len(self.verified), security._STEP_UP_FAIL_MAX)
        self.assertEqual(len(self.verified) + len(refused), self.BURST)
        self.assertEqual(set(refused), {429})

    def test_a_burst_of_wrong_passwords_is_capped_at_the_budget(self):
        from unittest import mock
        import oikonome.web.app as appmod
        with mock.patch.object(appmod.passwords, "verify_password",
                               side_effect=lambda *a: bool(self._slow_miss())):
            self._burst(lambda uid: appmod._in_session_password_ok(
                uid, {"password_hash": "x"}, "wrong-guess",
                "burst@example.dev", "elevate"))

    def test_a_burst_of_wrong_codes_is_capped_at_the_budget(self):
        from unittest import mock
        import oikonome.web.app as appmod
        from oikonome.auth import totp as totp_mod
        with mock.patch.object(totp_mod, "verify_used",
                               side_effect=self._slow_miss), \
                mock.patch.object(appmod.crypto, "decrypt_cp",
                                  side_effect=lambda v: v):
            self._burst(lambda uid: appmod._totp_consume(
                uid, "secret", "000000"))

    def test_right_passwords_do_not_spend_the_budget(self):
        from oikonome.auth import passwords
        from oikonome.web import security
        import oikonome.web.app as appmod
        user_id = str(uuid.uuid4())
        row = {"password_hash": passwords.hash_password(PW)}
        for _ in range(security._STEP_UP_FAIL_MAX + 2):
            self.assertTrue(appmod._in_session_password_ok(
                user_id, row, PW, "owner@example.dev", "elevate"))


class ConcurrentLoginGuessTests(_Base):
    """The sign-in door's account lock holds under a burst too. With the
    password in hand, a burst of concurrent wrong codes from many
    addresses must not all be verified by reading a failure count that none
    of them has added to yet: each takes its slot in the lock first."""

    BURST = 20

    def _request(self, n: int):
        from starlette.requests import Request
        return Request({"type": "http", "method": "POST", "path": "/api/login",
                        "headers": [], "query_string": b"",
                        "client": (f"198.51.100.{n + 1}", 40000)})

    def test_a_burst_of_wrong_codes_at_sign_in_is_capped_at_the_lock(self):
        import threading
        import time
        from unittest import mock
        from fastapi import HTTPException
        import oikonome.web.app as appmod
        from oikonome.auth import recovery
        from oikonome.auth import totp as totp_mod
        from oikonome.web import security
        _, email, _ = self._totp_user()
        verified, refused, lock = [], [], threading.Lock()

        def slow_miss(*_a, **_k):
            with lock:
                verified.append(1)
            time.sleep(0.05)
            return None
        gate = threading.Barrier(self.BURST)

        def one(n):
            gate.wait()
            try:
                appmod.login(self._request(n), email=email, password=PW,
                             totp_code="000000", cf_token="")
            except HTTPException as e:
                refused.append(e.status_code)
        with mock.patch.object(totp_mod, "verify_used",
                               side_effect=slow_miss), \
                mock.patch.object(recovery, "redeem", return_value=False), \
                mock.patch.object(appmod.turnstile, "verify_login",
                                  return_value=True):
            threads = [threading.Thread(target=one, args=(n,))
                       for n in range(self.BURST)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertLessEqual(len(verified), security._ACCT_FAIL_MAX)
        self.assertEqual(len(refused), self.BURST)
        self.assertEqual(refused.count(429), self.BURST - len(verified))
        self.assertEqual(_fails(email), len(verified))

    def test_a_wrong_code_at_sign_in_counts_once(self):
        _, email, _ = self._totp_user()
        r = TestClient(self.app).post("/api/login", data={
            "email": email, "password": PW, "totp_code": "000000"})
        self.assertEqual(r.status_code, 401, r.text)
        self.assertEqual(_fails(email), 1)

    def test_a_replayed_right_code_at_sign_in_is_not_a_guess(self):
        """A right code that a concurrent request spent first is refused,
        but it is not a guess at the factor: both sign-in doors give its
        slot in the account lock back."""
        from unittest import mock
        import oikonome.web.app as appmod
        from oikonome.auth import totp as totp_mod
        for path in ("/login", "/api/login"):
            with self.subTest(path=path):
                _, email, secret = self._totp_user()
                with mock.patch.object(appmod, "_burn_totp_counter",
                                       return_value=False):
                    r = TestClient(self.app).post(path, data={
                        "email": email, "password": PW,
                        "totp_code": totp_mod.code_now(secret)})
                self.assertEqual(r.status_code, 401, r.text)
                self.assertEqual(_fails(email), 0)


if __name__ == "__main__":
    unittest.main()
