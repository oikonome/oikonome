"""The second-factor guess budget at a door where the password is already
proven escalates: each tripped window doubles the next lock (capped at a
day), a successful sign-in does not reset that history, the owner is told
at most once a day, and a right code on the last try lifts the lock it
tripped. A step-up guess from a request with no credential fingerprint is
kept to that caller's own bucket."""

import itertools
import unittest
import uuid
from unittest import mock

from oikonome.web import security


class _Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


class SecondFactorLockEscalates(unittest.TestCase):
    def setUp(self):
        self.clock = _Clock()
        self._p = [mock.patch.object(security, "_shared_store",
                                     return_value=None),
                   mock.patch.object(security.time, "monotonic", self.clock),
                   mock.patch.object(security, "_limiter",
                                     security.RateLimiter())]
        for p in self._p:
            p.start()
        self.notices = []
        self._hook = mock.patch.object(security, "on_second_factor_lockout",
                                       self.notices.append)
        self._hook.start()
        self.email = f"lock-{uuid.uuid4().hex[:8]}@example.dev"

    def tearDown(self):
        self._hook.stop()
        for p in reversed(self._p):
            p.stop()

    def _miss_until_locked(self):
        """Spend wrong codes until the door refuses; the count spent."""
        for n in itertools.count(1):
            if security.account_login_locked(self.email):
                return n - 1
            self.assertIsNotNone(security.reserve_login_guess(self.email))
            self.assertLess(n, 100)

    def _locked_for(self):
        """Seconds (to the nearest 60) the lock in force still holds."""
        start = self.clock.t
        while security.account_login_locked(self.email):
            self.clock.t += 60
        return self.clock.t - start

    def test_each_tripped_window_doubles_the_lock(self):
        self.assertEqual(self._miss_until_locked(), security._ACCT_FAIL_MAX)
        self.assertLessEqual(self._locked_for(), 960)
        self._miss_until_locked()
        self.assertGreaterEqual(self._locked_for(), 1800)
        self._miss_until_locked()
        self.assertGreaterEqual(self._locked_for(), 3600)

    def test_a_successful_sign_in_does_not_reset_the_escalation(self):
        self._miss_until_locked()
        self._locked_for()
        security.clear_login_failures(self.email)     # the owner signs in
        self._miss_until_locked()
        self.assertGreaterEqual(self._locked_for(), 1800)

    def test_the_lock_is_capped_at_a_day_and_drains_after_a_quiet_week(self):
        for _ in range(9):
            self._miss_until_locked()
            held = self._locked_for()
        self.assertLessEqual(held, security._LOCK_MAX + 60)
        self.assertGreaterEqual(held, security._LOCK_MAX)
        self.clock.t += security._LOCK_HISTORY_WINDOW + 1
        self._miss_until_locked()
        self.assertLessEqual(self._locked_for(), 960)

    def test_the_owner_is_told_once_a_day(self):
        self._miss_until_locked()
        self.assertEqual(self.notices, [self.email])
        self._locked_for()
        self._miss_until_locked()
        self.assertEqual(len(self.notices), 1)
        self._locked_for()
        self.clock.t += 86400
        self._miss_until_locked()
        self.assertEqual(len(self.notices), 2)

    def _history(self):
        k = security._acct_key(self.email)
        return security._dual("count", security._lock_keys(k)[1],
                              security._LOCK_HISTORY_WINDOW)

    def test_a_right_code_in_the_last_slot_leaves_nothing_behind(self):
        """A right code that fills the window's 15th slot is handed back:
        no lock, no trip in the history, no notice — and the misses before
        it still count."""
        for _ in range(security._ACCT_FAIL_MAX - 1):
            security.reserve_login_guess(self.email)
        last = security.reserve_login_guess(self.email)
        self.assertIsNotNone(last)
        security.release_login_guess(last)            # the code was right
        self.assertFalse(security.account_login_locked(self.email))
        self.assertEqual(self._history(), 0)
        self.assertEqual(self.notices, [])
        security.reserve_login_guess(self.email)      # one more miss
        self.assertTrue(security.account_login_locked(self.email))
        self.assertEqual(self.notices, [self.email])

    def test_the_owners_right_code_never_hands_back_the_days_misses(self):
        """29 slow misses, the owner signs in, 29 more, and so on: the
        day's misses are kept across every sign-in, so the lock trips."""
        locked = False
        for _ in range(4):
            for i in range(29):
                if security.account_login_locked(self.email):
                    locked = True
                    break
                self.assertIsNotNone(security.reserve_login_guess(self.email))
                if i % 10 == 9:
                    self.clock.t += security._ACCT_FAIL_WINDOW + 1
            if locked:
                break
            self.clock.t += security._ACCT_FAIL_WINDOW + 1
            if security.account_login_locked(self.email):
                locked = True
                break
            security.release_login_guess(
                security.reserve_login_guess(self.email))
            security.clear_login_failures(self.email)
        self.assertTrue(locked)
        self.assertEqual(self._history(), 1)
        self.assertEqual(self.notices, [self.email])

    def test_a_paced_guesser_locks_within_the_day_across_owner_sign_ins(self):
        pace = (security._ACCT_FAIL_WINDOW // (security._ACCT_FAIL_MAX - 1)
                + 1)
        end = self.clock.t + 86400
        n = 0
        while self.clock.t < end and not self._history():
            if not security.account_login_locked(self.email):
                security.reserve_login_guess(self.email)
                n += 1
                if n % 10 == 0:                       # the owner signs in
                    security.release_login_guess(
                        security.reserve_login_guess(self.email))
                    security.clear_login_failures(self.email)
            self.clock.t += pace
        self.assertEqual(self._history(), 1)
        self.assertEqual(self.notices, [self.email])

    def test_a_paced_guesser_still_trips_the_lock_and_the_notice(self):
        """One wrong code just slower than the window refills never fills
        the short window; the day's miss budget trips the lock anyway."""
        pace = (security._ACCT_FAIL_WINDOW // (security._ACCT_FAIL_MAX - 1)
                + 1)
        spent = 0
        end = self.clock.t + 86400
        while self.clock.t < end:
            if not security.account_login_locked(self.email):
                self.assertIsNotNone(security.reserve_login_guess(self.email))
                spent += 1
            self.clock.t += pace
        self.assertEqual(self.notices, [self.email])
        self.assertLess(spent, 300)    # the day budget bounds a paced guesser

    def test_right_codes_are_not_misses_in_the_day_budget(self):
        for _ in range(security._DAY_MISS_MAX * 2):
            security.release_login_guess(
                security.reserve_login_guess(self.email))
            self.clock.t += 120
        self.assertFalse(security.account_login_locked(self.email))
        self.assertEqual(self.notices, [])

    def test_the_reset_link_keeps_its_own_plain_budget(self):
        """The mailbox's reset is not behind the escalating sign-in lock,
        its misses neither trip that lock nor send the password notice,
        and a completed reset lifts the sign-in lock."""
        self._miss_until_locked()
        self.assertEqual(len(self.notices), 1)
        self.assertGreater(security.login_lock_remaining(self.email), 0)
        for _ in range(security._RESET_FAIL_MAX):
            self.assertIsNotNone(security.reserve_reset_guess(self.email))
        self.assertIsNone(security.reserve_reset_guess(self.email))
        self.assertEqual(len(self.notices), 1)
        security.lift_login_lock(self.email)
        self.assertFalse(security.account_login_locked(self.email))
        self.assertEqual(security.login_lock_remaining(self.email), 0)

    def test_the_wait_is_said_in_real_time(self):
        self.assertEqual(security.wait_words(61), "about 2 minutes")
        self.assertEqual(security.wait_words(86400), "about 24 hours")

    def test_step_up_escalates_and_needs_no_shared_bucket(self):
        uid = uuid.uuid4().hex
        tok_c = security._req_cred.set("")
        tok_i = security._req_ip.set("203.0.113.7")
        try:
            for _ in range(security._STEP_UP_FAIL_MAX):
                self.assertIsNotNone(security.reserve_step_up_guess(uid))
            self.assertTrue(security.step_up_locked(uid))
            security._req_ip.set("203.0.113.8")
            self.assertFalse(security.step_up_locked(uid))
            security._req_ip.set("203.0.113.7")
            self.clock.t += security._STEP_UP_FAIL_WINDOW + 1
            self.assertFalse(security.step_up_locked(uid))
            for _ in range(security._STEP_UP_FAIL_MAX):
                security.reserve_step_up_guess(uid)
            self.assertTrue(security.step_up_locked(uid))   # second trip
            self.clock.t += security._STEP_UP_FAIL_WINDOW + 1
            self.assertTrue(security.step_up_locked(uid))   # 30 min now
            self.assertGreater(security.step_up_lock_remaining(uid), 0)
        finally:
            security._req_ip.reset(tok_i)
            security._req_cred.reset(tok_c)


class StepUpLockTellsTheAccount(unittest.TestCase):
    """Repeated failed confirmations (the step-up asked before a sensitive
    change) lock that credential's step-up doors; the lock tells the
    account on the same channel, at the same once-a-day rate, as a failed
    sign-in's lock does — one notice for the lock, none per attempt."""

    def setUp(self):
        self.clock = _Clock()
        self._p = [mock.patch.object(security, "_shared_store",
                                     return_value=None),
                   mock.patch.object(security.time, "monotonic", self.clock),
                   mock.patch.object(security, "_limiter",
                                     security.RateLimiter())]
        for p in self._p:
            p.start()
        self.notices = []
        self._hook = mock.patch.object(security, "on_step_up_lockout",
                                       self.notices.append, create=True)
        self._hook.start()
        self.uid = uuid.uuid4().hex
        self._tok = (security._req_cred.set("cookie-a"),
                     security._req_ip.set("203.0.113.9"))

    def tearDown(self):
        security._req_cred.reset(self._tok[0])
        security._req_ip.reset(self._tok[1])
        self._hook.stop()
        for p in reversed(self._p):
            p.stop()

    def _miss_until_locked(self):
        for n in itertools.count(1):
            if security.step_up_locked(self.uid):
                return n - 1
            self.assertIsNotNone(security.reserve_step_up_guess(self.uid))
            self.assertLess(n, 100)

    def test_a_lock_sends_one_notice_and_attempts_send_none(self):
        self.assertEqual(self._miss_until_locked(),
                         security._STEP_UP_FAIL_MAX)
        self.assertEqual(self.notices, [self.uid])
        for _ in range(5):                       # knocking on the lock
            self.assertIsNone(security.reserve_step_up_guess(self.uid))
            self.assertTrue(security.step_up_locked(self.uid))
        self.assertEqual(self.notices, [self.uid])

    def test_at_most_one_notice_a_day_across_locks_and_sessions(self):
        self._miss_until_locked()
        while security.step_up_locked(self.uid):
            self.clock.t += 60
        security._req_cred.set("cookie-b")       # another session's bucket
        self._miss_until_locked()
        self.assertEqual(len(self.notices), 1)
        self.clock.t += 86400
        security._req_cred.set("cookie-c")
        self._miss_until_locked()
        self.assertEqual(len(self.notices), 2)

    def test_two_members_of_one_household_are_each_told(self):
        # the once-a-day hold is the user's, not the household's: one
        # member's lock must not swallow another member's notice
        self._miss_until_locked()
        first = self.uid
        self.uid = uuid.uuid4().hex               # the second member
        security._req_cred.set("cookie-b")
        self._miss_until_locked()
        self.assertEqual(self.notices, [first, self.uid])

    def test_misses_below_the_limit_send_nothing(self):
        for _ in range(security._STEP_UP_FAIL_MAX - 1):
            security.reserve_step_up_guess(self.uid)
        self.assertFalse(security.step_up_locked(self.uid))
        self.assertEqual(self.notices, [])


class LockoutNoticeIsWired(unittest.TestCase):
    def test_a_step_up_lock_reaches_the_account_on_the_security_channel(self):
        import os
        from .util import _ensure_db
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        from oikonome.web import app as appmod
        appmod.DEV_MODE = True
        self.assertIs(security.on_step_up_lockout,
                      appmod._notify_step_up_lockout)
        self.assertIn("step_up_failed", appmod._SECURITY_CHANGES)
        self.assertIn("step_up_failed", appmod._KEEPS_OTHER_SIGN_INS)
        import time as _t
        from fastapi.testclient import TestClient
        c = TestClient(appmod.app)
        email = f"stp-{uuid.uuid4().hex[:8]}@example.dev"
        c.post("/api/signup", data={"email": email,
                                    "password": "correct-horse-battery"})
        with appmod._control_conn() as conn:
            row = conn.execute("SELECT id, tenant_id FROM users "
                               "WHERE email=%s", (email,)).fetchone()
        sent = []
        with mock.patch.object(appmod, "_deliver_security_notice",
                               side_effect=lambda *a: sent.append(a)):
            appmod._notify_step_up_lockout(str(row["id"]))
            for _ in range(50):
                if sent:
                    break
                _t.sleep(0.05)
        self.assertEqual(sent, [(email, row["tenant_id"], "step_up_failed")])


    def test_the_web_app_tells_the_owner_on_the_security_channel(self):
        from .util import _ensure_db
        _ensure_db()
        from oikonome.web import app as appmod
        self.assertIs(security.on_second_factor_lockout,
                      appmod._notify_second_factor_lockout)
        self.assertIn("password", appmod._SECURITY_CHANGES[
            "second_factor_failed"])
        self.assertIn("second_factor_failed", appmod._KEEPS_OTHER_SIGN_INS)


class ResetDoorUnderTheLock(unittest.TestCase):
    """Through the real /reset door: wrong recovery codes never deliver the
    password-worded notice, and the door still answers while sign-in is
    locked; /api/login's refusal states the real wait."""

    def test_wrong_recovery_codes_on_a_reset_link(self):
        import os
        from fastapi.testclient import TestClient
        from .util import _ensure_db
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        from oikonome.auth import reset as reset_mod
        appmod.DEV_MODE = True
        c = TestClient(appmod.app)
        email = f"rst-{uuid.uuid4().hex[:8]}@example.dev"
        c.post("/api/signup", data={"email": email,
                                    "password": "correct-horse-battery"})
        with appmod._control_conn() as conn:
            uid = conn.execute("SELECT id FROM users WHERE email=%s",
                               (email,)).fetchone()["id"]
            token = reset_mod.create_reset(conn, uid)
        sent = []
        anon = TestClient(appmod.app)
        with mock.patch.object(appmod, "_reset_needs_recovery",
                               return_value=True), \
             mock.patch.object(appmod, "_deliver_security_notice",
                               side_effect=lambda *a: sent.append(a)), \
             mock.patch.object(security, "_shared_store", return_value=None), \
             mock.patch.object(security, "_limiter", security.RateLimiter()):
            # sign-in already locked by someone holding the password
            for _ in range(security._ACCT_FAIL_MAX):
                security.reserve_login_guess(email)
            self.assertTrue(security.account_login_locked(email))
            import time as _t
            _t.sleep(0.2)                # the setup's own notice lands
            sent.clear()
            # the route's per-IP limit stops a burst before the door's own
            # budget would; five wrong codes all reach the door
            codes = []
            for i in range(5):
                r = anon.post("/reset", data={
                    "token": token, "password": "brand-new-password-1",
                    "recovery_code": f"WRONG-{i:04d}"})
                codes.append(r.status_code)
            self.assertEqual(codes, [401] * 5)
            r = anon.post("/api/login", data={
                "email": email, "password": "correct-horse-battery",
                "totp_code": "000000"})
            from fastapi import HTTPException
            with self.assertRaises(HTTPException) as cm:
                appmod._raise_login_locked(email)
            self.assertEqual(cm.exception.status_code, 429)
            self.assertIn("about", cm.exception.detail)
            self.assertIn("Retry-After", cm.exception.headers)
            if r.status_code == 429:
                self.assertIn("Retry-After", r.headers)
                self.assertNotIn("a few minutes", r.text)
        _t.sleep(0.2)                    # let any notice thread run
        self.assertFalse([a for a in sent if a[2] == "second_factor_failed"])


if __name__ == "__main__":
    unittest.main()
