"""Concurrent argon2 work is capped process-wide, and a request that cannot
get a slot in time is answered 503 with Retry-After.

Each argon2 call holds the hasher's memory cost (64 MiB at the defaults)
and releases the GIL, so without a cap the web server's thread pool runs
dozens at once and a burst of sign-ins pushes the container past its memory
limit, which kills the server for everyone. The unknown-address dummy
verify goes through the same gate as a real one.
"""

import os
import threading
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import passwords

from .util import _ensure_db


class _CountingHasher:
    """Stand-in for PasswordHasher that records peak concurrency."""

    def __init__(self, hold=0.05):
        self.hold = hold
        self.live = 0
        self.peak = 0
        self._lock = threading.Lock()

    def _work(self):
        with self._lock:
            self.live += 1
            self.peak = max(self.peak, self.live)
        time.sleep(self.hold)
        with self._lock:
            self.live -= 1

    def hash(self, password):
        self._work()
        return "$argon2id$fake"

    def verify(self, hashed, password):
        self._work()
        return True


class GateTests(unittest.TestCase):
    def _gate(self, size, hasher):
        for target, value in (("_GATE", threading.BoundedSemaphore(size)),
                              ("_ph", hasher)):
            p = mock.patch.object(passwords, target, value)
            p.start()
            self.addCleanup(p.stop)

    def test_peak_concurrency_never_exceeds_the_cap(self):
        hasher = _CountingHasher()
        self._gate(3, hasher)
        errors = []

        def one(i):
            try:
                if i % 2:
                    passwords.hash_password("x" * 12)
                else:
                    passwords.verify_password("$argon2id$fake", "x" * 12)
            except Exception as e:              # noqa: BLE001
                errors.append(e)
        threads = [threading.Thread(target=one, args=(i,)) for i in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(hasher.peak, 3)

    def test_a_caller_that_waits_too_long_is_refused(self):
        hasher = _CountingHasher(hold=0.5)
        self._gate(1, hasher)
        with mock.patch.dict(os.environ, {"OIKONOME_ARGON2_WAIT": "0.05"}):
            holder = threading.Thread(
                target=passwords.hash_password, args=("x" * 12,))
            holder.start()
            time.sleep(0.05)
            with self.assertRaises(passwords.HasherBusy):
                passwords.verify_password("$argon2id$fake", "x" * 12)
            holder.join()
        # the slot is released afterwards: the next call goes through
        self.assertTrue(passwords.verify_password("$argon2id$fake", "y" * 12))

    def test_the_cap_reads_the_environment(self):
        with mock.patch.dict(os.environ, {"OIKONOME_ARGON2_CONCURRENCY": "5"}):
            self.assertEqual(passwords._gate_size(), 5)
        with mock.patch.dict(os.environ, {"OIKONOME_ARGON2_CONCURRENCY": ""}):
            self.assertEqual(passwords._gate_size(), 3)
        with mock.patch.dict(os.environ, {"OIKONOME_ARGON2_CONCURRENCY": "0"}):
            self.assertEqual(passwords._gate_size(), 1)

    def test_real_hashes_still_verify_through_the_gate(self):
        h = passwords.hash_password("correct-horse-battery")
        self.assertTrue(passwords.verify_password(h, "correct-horse-battery"))
        self.assertFalse(passwords.verify_password(h, "wrong-horse-battery"))


class BusyHasherAnswers503Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()
        from oikonome.web.app import app
        cls.client = TestClient(app, raise_server_exceptions=False)

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        p = mock.patch.object(passwords, "_gated",
                              side_effect=passwords.HasherBusy("full"))
        p.start()
        self.addCleanup(p.stop)

    def test_api_login_for_an_unknown_address_answers_503(self):
        # the dummy verify for an address nobody owns is gated too
        r = self.client.post("/api/login", data={
            "email": "nobody-gate@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertEqual(r.headers.get("Retry-After"), "15")

    def test_login_form_answers_the_busy_page(self):
        r = self.client.post("/login", data={
            "email": "nobody-gate@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertIn("Catching our breath", r.text)


class ABusyHasherSpendsNothingTests(unittest.TestCase):
    """A busy hasher is a passing refusal ("try again"), so a retry has to
    find everything as it was: no guess counted, no one-time link spent,
    no account erased. Every door hashes or verifies before its first
    irreversible step, and a door that reserved a guess first gives it
    back."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()
        import oikonome.web.app as appmod
        cls.appmod = appmod

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()
        p = mock.patch.object(security, "_redis_limiter", False)
        p.start()
        self.addCleanup(p.stop)

    def _busy(self):
        return mock.patch.object(passwords, "_gated",
                                 side_effect=passwords.HasherBusy("full"))

    def test_a_busy_hasher_costs_no_step_up_guess(self):
        from oikonome.web import security
        uid = "00000000-0000-0000-0000-00000000b05e"
        row = {"password_hash": "$argon2id$fake"}
        with self._busy():
            for _ in range(security._STEP_UP_FAIL_MAX + 3):
                with self.assertRaises(passwords.HasherBusy):
                    self.appmod._in_session_password_ok(
                        uid, row, "a-password-here", "x@example.dev",
                        "step_up")
        self.assertFalse(security.step_up_locked(uid))

    def test_a_busy_hasher_leaves_the_invite_unspent(self):
        import uuid
        from oikonome.auth import invites
        from oikonome.db import tenancy
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"busy-{uuid.uuid4().hex[:8]}")
            owner = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role) "
                "VALUES (%s, %s, 'x', 'owner') RETURNING id",
                (tid, f"own-{uuid.uuid4().hex[:8]}@example.dev")
            ).fetchone()["id"]
            email = f"mem-{uuid.uuid4().hex[:8]}@example.dev"
            token = invites.create(admin, tid, owner, label=email)["token"]
            with self._busy():
                with self.assertRaises(passwords.HasherBusy):
                    invites.claim(admin, token, email,
                                  "correct-horse-battery")
            out = invites.claim(admin, token, email, "correct-horse-battery")
            self.assertEqual(str(out["tenant_id"]), str(tid))
        finally:
            admin.close()

    def test_a_busy_hasher_leaves_the_reclaim_link_and_the_account(self):
        import uuid
        from oikonome.db import tenancy
        sent = []
        patches = (
            mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1",
                                         "OIKONOME_OPEN_SIGNUP": "1"}),
            mock.patch.object(self.appmod, "DEV_MODE", False),
            mock.patch.object(self.appmod, "_deliver_verification"),
            mock.patch.object(self.appmod, "_deliver_reclaim",
                              side_effect=lambda e, t: sent.append(t)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        email = f"busy-{uuid.uuid4().hex[:8]}@x.dev"
        c = TestClient(self.appmod.app, raise_server_exceptions=False)
        for pw in ("first-person-password", "second-person-password"):
            c.post("/api/signup", data={"email": email, "password": pw})
        self.assertEqual(len(sent), 1)

        def tenant():
            admin = tenancy.admin_connect()
            try:
                return admin.execute("SELECT tenant_id FROM users "
                                     "WHERE email=%s", (email,)).fetchone()
            finally:
                admin.close()
        before = tenant()
        with self._busy():
            r = c.post("/signup/reclaim",
                       data={"token": sent[0],
                             "password": "clicker-chosen-password"},
                       follow_redirects=False)
        self.assertEqual(r.status_code, 503, r.text)
        self.assertEqual(tenant(), before, "nothing was erased")
        r = TestClient(self.appmod.app).post(
            "/signup/reclaim", data={"token": sent[0],
                                     "password": "clicker-chosen-password"},
            follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)



def _only_hashing_busy():
    """The gate as a burst leaves it for a request that needs a NEW hash:
    checking the current password still gets through, hashing does not."""
    real = passwords._gated

    def gated(fn, *args):
        if fn == passwords._ph.hash:
            raise passwords.HasherBusy("full")
        return real(fn, *args)
    return mock.patch.object(passwords, "_gated", side_effect=gated)


class ABusyHasherSpendsNoOneTimeFactorTests(unittest.TestCase):
    """A busy hasher verified and hashed nothing, so the "try again" it
    answers has to find every one-time factor where it was: the recovery
    code a reset carries, the authenticator step a password change
    carries, and the emailed unlock a sign-in from the app spends. Each
    door hashes or verifies first and consumes its one-time proof last."""

    PW = "correct-horse-battery"
    NEW = "a-brand-new-passphrase"

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
        p = mock.patch.object(security, "_redis_limiter", False)
        p.start()
        self.addCleanup(p.stop)

    def _user(self, *, totp_secret=None):
        import uuid
        from oikonome.db import tenancy
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, f"busy-{uuid.uuid4().hex[:8]}")
            email = f"busy-{uuid.uuid4().hex[:8]}@example.dev"
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, "
                "totp_secret, verified_at) VALUES (%s,%s,%s,%s,now()) "
                "RETURNING id",
                (tid, email, passwords.hash_password(self.PW),
                 totp_secret)).fetchone()["id"]
            admin.commit()
        finally:
            admin.close()
        return uid, email

    def _control(self):
        from oikonome.db import tenancy
        return tenancy.control_connect()

    def test_a_busy_reset_leaves_its_recovery_code_unspent(self):
        from oikonome.auth import recovery
        from oikonome.auth import reset as reset_mod
        uid, _ = self._user(totp_secret="JBSWY3DPEHPK3PXP")
        from oikonome.db import tenancy
        admin = tenancy.admin_connect()
        try:
            codes = recovery.issue(admin, uid)
            admin.commit()
        finally:
            admin.close()
        with self._control() as conn:
            link = reset_mod.create_reset(conn, uid)
        c = TestClient(self.appmod.app, raise_server_exceptions=False)
        form = {"token": link, "password": self.NEW,
                "recovery_code": codes[0]}
        with mock.patch.object(passwords, "_gated",
                               side_effect=passwords.HasherBusy("full")):
            r = c.post("/reset", data=form, follow_redirects=False)
        self.assertEqual(r.status_code, 503, r.text)
        with self._control() as conn:
            self.assertEqual(recovery.remaining(conn, uid), len(codes),
                             "the busy reset spent a recovery code")
        r = c.post("/reset", data=form, follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)

    def test_a_busy_password_change_leaves_its_one_time_code_unspent(self):
        from oikonome.auth import sessions, totp
        from oikonome.db import tenancy
        from .util import clear_totp_burn
        from oikonome.db import crypto
        secret = totp.new_secret()
        uid, email = self._user(totp_secret=crypto.encrypt_cp(secret))
        admin = tenancy.admin_connect()
        try:
            tid = admin.execute("SELECT tenant_id FROM users WHERE id=%s",
                                (uid,)).fetchone()["tenant_id"]
            token = sessions.create_session(admin, uid, tid, "ua")
            admin.commit()
        finally:
            admin.close()
        c = TestClient(self.appmod.app, raise_server_exceptions=False)
        c.cookies.set(sessions.COOKIE_NAME, token)
        clear_totp_burn()
        body = {"current_password": self.PW, "new_password": self.NEW,
                "totp_code": totp.code_now(secret)}
        with _only_hashing_busy():
            r = c.post("/api/password/change", json=body)
        self.assertEqual(r.status_code, 503, r.text)
        r = c.post("/api/password/change", json=body)
        self.assertEqual(r.status_code, 200,
                         "the busy change burnt the authenticator step: "
                         + r.text)

    def _unlocked(self, uid, email):
        from oikonome.auth import login_unlock
        from oikonome.web import turnstile
        env = mock.patch.dict(os.environ, {
            "OIKONOME_TURNSTILE_SITE_KEY": "site",
            "OIKONOME_TURNSTILE_SECRET": "secret"})
        env.start()
        self.addCleanup(env.stop)
        # the site-wide check is armed, as in a burst of mistyped sign-ins
        p = mock.patch.object(turnstile, "required_for_login",
                              return_value=True)
        p.start()
        self.addCleanup(p.stop)
        with self._control() as conn:
            login_unlock.follow(conn, login_unlock.create(conn, uid))

    def _still_unlocked(self, email):
        from oikonome.auth import login_unlock
        with self._control() as conn:
            return login_unlock.is_unlocked(conn, email)

    def test_a_busy_app_sign_in_leaves_the_emailed_unlock_unspent(self):
        uid, email = self._user()
        self._unlocked(uid, email)
        c = TestClient(self.appmod.app, raise_server_exceptions=False)
        form = {"email": email, "password": self.PW}
        with mock.patch.object(passwords, "_gated",
                               side_effect=passwords.HasherBusy("full")):
            r = c.post("/api/login", data=form)
        self.assertEqual(r.status_code, 503, r.text)
        self.assertTrue(self._still_unlocked(email),
                        "the busy sign-in spent the emailed unlock")
        r = c.post("/api/login", data=form)
        self.assertEqual(r.status_code, 200, r.text)

    def test_a_busy_form_sign_in_leaves_the_emailed_unlock_unspent(self):
        uid, email = self._user()
        self._unlocked(uid, email)
        c = TestClient(self.appmod.app, raise_server_exceptions=False)
        form = {"email": email, "password": self.PW}
        with mock.patch.object(passwords, "_gated",
                               side_effect=passwords.HasherBusy("full")):
            r = c.post("/login", data=form, follow_redirects=False)
        self.assertEqual(r.status_code, 503, r.text)
        self.assertTrue(self._still_unlocked(email),
                        "the busy sign-in spent the emailed unlock")
        r = c.post("/login", data=form, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303), r.text)

    def test_a_busy_sign_in_does_not_re_arm_an_unlock_it_never_spent(self):
        """Passing by a real token spends no unlock, so a busy attempt has
        nothing to give back — in particular not an exemption an earlier,
        failed attempt already used up."""
        from oikonome.auth import login_unlock
        from oikonome.web import turnstile
        uid, email = self._user()
        self._unlocked(uid, email)
        with self._control() as conn:
            # spent by an earlier attempt that got as far as a wrong password
            self.assertTrue(login_unlock.consume(conn, email))
            conn.execute("UPDATE login_unlocks SET consumed_at = consumed_at"
                         " - interval '5 seconds' WHERE user_id=%s", (uid,))
        c = TestClient(self.appmod.app, raise_server_exceptions=False)
        with mock.patch.object(turnstile, "verify", return_value=True), \
                mock.patch.object(passwords, "_gated",
                                  side_effect=passwords.HasherBusy("full")):
            r = c.post("/api/login", data={"email": email,
                                           "password": self.PW,
                                           "cf-turnstile-response": "tok"})
        self.assertEqual(r.status_code, 503, r.text)
        self.assertFalse(self._still_unlocked(email))


if __name__ == "__main__":
    unittest.main()
