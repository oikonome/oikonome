"""The rate limiter's memory is bounded by time, not by who visits.

Its windows are keyed on (route, client IP) — a shape an anonymous client
picks — and it is the store behind the unauthenticated limits on /login,
/signup and /forgot. Idle keys are removed; otherwise every scanner that
ever tripped a limit stays resident for the life of the process: one more
shared resource whose size untrusted clients decide, in a process that also
holds the SPA and the feedback ring buffer.

Eviction must not become a lever of its own, so two properties matter as
much as the shrinking: a read may never grow the table, and a full table
must degrade by FORGETTING rather than by refusing — refusing the newest key
would let a spray of fresh keys close the door on everybody else.
"""

import time
import unittest

from oikonome.web import security


class WindowsThatCanNoLongerHoldAnythingAreDropped(unittest.TestCase):
    def test_a_scanner_that_never_returns_stops_costing_memory(self):
        """The leak in one line: a key is only ever revisited by the same
        client, so a bucket left behind by a one-shot IP is never looked at
        again — and never trimmed, never dropped."""
        rl = security.RateLimiter()
        for i in range(2000):
            rl.check(("login", f"203.0.113.{i}"), 5, 0.01)
        self.assertEqual(len(rl._hits), 2000)

        time.sleep(0.05)          # every one of those windows is now empty
        # …and ordinary traffic carries the cost of noticing
        for i in range(600):
            rl.check(("login", f"198.51.100.{i}"), 5, 900)

        stale = [k for k in rl._hits if k[1].startswith("203.0.113.")]
        self.assertEqual(stale, [])
        self.assertLessEqual(len(rl._hits), 600)

    def test_a_live_window_is_never_forgotten_early(self):
        """The sweep is exact, not a heuristic: a bucket still inside its
        window keeps counting, or the limit it enforces evaporates under
        exactly the traffic that triggers a sweep."""
        rl = security.RateLimiter()
        victim = ("login", "203.0.113.1")
        for _ in range(5):
            rl.check(victim, 5, 900)
        for i in range(3000):
            rl.check(("login", f"198.51.100.{i // 256}.{i % 256}"), 5, 0.01)
        self.assertFalse(rl.check(victim, 5, 900),
                         "the sixth request inside the window must still be "
                         "refused after the table has been swept")

    def test_counting_an_unknown_key_does_not_create_one(self):
        """`account_login_locked` counts a bucket for every address someone
        types at the login form. If that count materialised a key, the
        lockout check itself would be the leak."""
        rl = security.RateLimiter()
        self.assertEqual(rl.count(("login-fail", "nobody@example.com"), 900),
                         0)
        self.assertEqual(rl._hits, {})

    def test_a_bucket_that_drains_on_a_read_is_dropped(self):
        rl = security.RateLimiter()
        key = ("login-fail", "someone@example.com")
        rl.add(key, 0.01)
        self.assertIn(key, rl._hits)
        time.sleep(0.05)
        self.assertEqual(rl.count(key, 0.01), 0)
        self.assertNotIn(key, rl._hits)

    def test_expired_hard_blocks_are_dropped_too(self):
        """`blocked()` only pops a ban when someone asks about it, and a
        banned scanner that never comes back is never asked about."""
        rl = security.RateLimiter()
        for i in range(300):
            rl.block(("ip", f"203.0.113.{i}"), 0.01)
        time.sleep(0.05)
        for i in range(600):
            rl.check(("login", f"198.51.100.{i}"), 5, 900)
        self.assertEqual(rl._blocks, {})


class AFullTableForgetsRatherThanRefuses(unittest.TestCase):
    """The backstop's failure direction is the whole point of having one.

    Refusing the request that would add the key past the ceiling reads like
    the safe answer and is the opposite: an attacker sprays keys until the
    table is full and every legitimate sign-in is refused for free — the
    denial of service the limiter exists to prevent, delivered by the
    limiter. So the table sheds windows instead, nearest-to-expiry first.
    """

    def setUp(self):
        self._max = security._MAX_KEYS
        security._MAX_KEYS = 512

    def tearDown(self):
        security._MAX_KEYS = self._max

    def _fill(self, rl, n, window=900):
        for i in range(n):
            rl.check(("spray", f"198.51.100.{i // 256}.{i % 256}"), 5, window)

    def test_the_table_stays_bounded_when_every_key_is_live(self):
        rl = security.RateLimiter()
        self._fill(rl, 4000)
        self.assertLessEqual(len(rl._hits), security._MAX_KEYS)

    def test_the_request_that_finds_the_table_full_is_still_allowed(self):
        rl = security.RateLimiter()
        self._fill(rl, 4000)
        self.assertTrue(rl.check(("login", "203.0.113.77"), 5, 900))

    def test_the_longest_lived_windows_are_the_last_to_go(self):
        """A sprayer must not be able to erase the buckets that hold it to
        account. The 24-hour ban history and the 15-minute account lockout
        outlive a flood of one-off per-IP windows by construction, because
        eviction follows the order the windows were going to expire in."""
        rl = security.RateLimiter()
        ban = ("banhist", "203.0.113.9")
        lock = ("login-fail", "victim@example.com")
        rl.add(ban, 86400)
        rl.add(lock, 900)
        self._fill(rl, 4000, window=60)
        self.assertIn(ban, rl._hits)
        self.assertIn(lock, rl._hits)


class SecurityBucketsAreTheLastToBeForgotten(unittest.TestCase):
    """Which bucket gives way is a security question, and expiry order
    alone answers it backwards.

    `dead_at` is now + window_s, so ranking candidates by it would rank them by
    WINDOW LENGTH. The security buckets carry the SHORTEST windows in the
    table — a 15-minute account lockout, a 10-minute strike count, a
    15-minute risk signal — while the ordinary per-route limiters mostly run
    an hour (/forgot, /reset, /signup, /verify-email, the API's throttled
    doors). Ranked on expiry alone the lockout would go FIRST and
    /forgot's hourly counter outlive it, which is a sprayer's win
    condition: fill the table from long-window routes and the victim's
    second-factor lockout resets to zero, indefinitely.
    """

    def setUp(self):
        self._max = security._MAX_KEYS
        security._MAX_KEYS = 512

    def tearDown(self):
        security._MAX_KEYS = self._max

    def _spray_hourly_routes(self, rl, n=4000):
        """What an anonymous client can actually fill the table with: one
        bucket per source address on the 3600-second public routes."""
        for i in range(n):
            rl.check(("forgot", f"198.51.100.{i // 256}.{i % 256}"), 5, 3600)

    def test_an_hourly_spray_cannot_erase_an_account_lockout(self):
        rl = security.RateLimiter()
        lock = ("login-fail", "victim@example.com")
        for _ in range(security._ACCT_FAIL_MAX):
            rl.add(lock, security._ACCT_FAIL_WINDOW)
        self._spray_hourly_routes(rl)
        self.assertIn(lock, rl._hits,
                      "the account lockout must outlive a flood of "
                      "longer-window route buckets")
        self.assertGreaterEqual(
            rl.count(lock, security._ACCT_FAIL_WINDOW),
            security._ACCT_FAIL_MAX,
            "an evicted-and-recreated bucket counts zero, which is the "
            "lockout lifted")

    def test_the_autoban_evidence_survives_the_same_spray(self):
        """Strikes are what admit an IP to a ban and banhist is what makes
        the second ban longer than the first; either one erased and the
        repeat offender is a first offender forever."""
        rl = security.RateLimiter()
        strike = ("strike", "203.0.113.9")
        hist = ("banhist", "203.0.113.9")
        for _ in range(security._STRIKE_MAX - 1):
            rl.add(strike, security._STRIKE_WINDOW)
        rl.add(hist, security._BAN_HISTORY_WINDOW)
        self._spray_hourly_routes(rl)
        self.assertEqual(rl.count(strike, security._STRIKE_WINDOW),
                         security._STRIKE_MAX - 1)
        self.assertIn(hist, rl._hits)

    def test_the_human_check_risk_signal_survives_the_same_spray(self):
        """All three risk buckets, including the site-wide one that is the
        only defence against stuffing spread over a botnet."""
        rl = security.RateLimiter()
        keys = [security._RISK_SITE_KEY, ("risk-ip", "203.0.113.9"),
                ("risk-acct", "victim@example.com")]
        for k in keys:
            rl.add(k, security._RISK_WINDOW)
        self._spray_hourly_routes(rl)
        for k in keys:
            self.assertIn(k, rl._hits, k)

    def test_a_table_of_nothing_but_security_buckets_is_still_bounded(self):
        """The exemption is LAST, not never. One strike bucket per source
        address is a shape an attacker picks just as freely as a route
        bucket, so a namespace that could pin the table open would only move
        the memory exhaustion into it."""
        rl = security.RateLimiter()
        for i in range(4000):
            rl.add(("strike", f"198.51.100.{i // 256}.{i % 256}"),
                   security._STRIKE_WINDOW)
        self.assertLessEqual(len(rl._hits), security._MAX_KEYS)

    def test_every_namespace_the_limiter_stores_is_classified(self):
        """A new security bucket added without a line in _PROTECTED_NS is a
        silent regression — it is the ordering that changes, not anything
        that fails — so the set is pinned here against the namespaces the
        code actually writes."""
        self.assertEqual(
            security._PROTECTED_NS,
            frozenset({"login-fail", "strike", "banhist",
                       "risk-ip", "risk-acct", "risk-site"}))


if __name__ == "__main__":
    unittest.main()
