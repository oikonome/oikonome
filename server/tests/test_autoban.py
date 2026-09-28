"""Auto-ban: a client IP that keeps tripping limits / failing auth earns a
short, self-expiring hard block, enforced before routing (sheds bot load on a
small host and complements a Cloudflare edge rule). See security.record_strike
/ AutoBanMiddleware. Deliberately conservative: only PUBLIC IPs are eligible,
so loopback, LAN, and the test client are never banned.
"""

import os
import types
import unittest
from unittest import mock

from oikonome.auth import passwords
from oikonome.web import security

PUB = "8.8.8.8"        # a global address — eligible for banning
PRIV = "10.0.0.5"      # LAN — never banned
LOOP = "127.0.0.1"     # loopback — never banned


def _req(peer=PUB, xff=None):
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=peer), headers=headers)


class AutoBanTests(unittest.TestCase):
    def setUp(self):
        security._limiter._hits.clear()
        security._limiter._blocks.clear()
        # force the in-memory path so the test needs no Redis
        self._p = mock.patch.object(security, "_redis_limiter", False)
        self._p.start()
        self._env = mock.patch.dict(
            os.environ, {"OIKONOME_TRUSTED_PROXIES": "", "OIKONOME_AUTOBAN": "1"})
        self._env.start()

    def tearDown(self):
        self._p.stop()
        self._env.stop()

    def test_strikes_ban_after_threshold(self):
        for _ in range(security._STRIKE_MAX - 1):
            security.record_strike(PUB, route="login")
        self.assertIsNone(security.blocked_seconds(PUB))   # not yet
        security.record_strike(PUB, route="login")         # crosses it
        rem = security.blocked_seconds(PUB)
        self.assertIsNotNone(rem)
        self.assertLessEqual(rem, security._BAN_BASE)
        self.assertGreater(rem, 0)

    def test_public_only_private_and_loopback_never_banned(self):
        for ip in (PRIV, LOOP, "testclient", "?"):
            for _ in range(security._STRIKE_MAX + 5):
                security.record_strike(ip, route="login")
            self.assertIsNone(security.blocked_seconds(ip),
                              f"{ip} must never be banned")

    def test_ipv4_mapped_public_ip_is_bannable(self):
        # a proxy/socket path can hand us the IPv4-mapped IPv6 spelling of a
        # public v4 client; the embedded address is global, so it must be
        # judged global and banned, not waved through as non-global
        mapped = "::ffff:8.8.8.8"
        self.assertTrue(security._bannable(mapped))
        for _ in range(security._STRIKE_MAX):
            security.record_strike(mapped, route="login")
        self.assertIsNotNone(security.blocked_seconds(mapped))

    def test_ipv4_mapped_private_ip_still_never_banned(self):
        # the mapped spelling of a LAN address is still non-global
        mapped = "::ffff:10.0.0.5"
        self.assertFalse(security._bannable(mapped))
        for _ in range(security._STRIKE_MAX + 5):
            security.record_strike(mapped, route="login")
        self.assertIsNone(security.blocked_seconds(mapped))

    def test_ban_escalates_for_repeat_offenders(self):
        def _ban_once():
            security._limiter._blocks.clear()
            security._limiter.clear(("strike", PUB))
            for _ in range(security._STRIKE_MAX):
                security.record_strike(PUB, route="login")
            return security.blocked_seconds(PUB)
        first = _ban_once()
        second = _ban_once()
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertGreater(second, first)      # 2nd ban is longer

    def test_clear_lifts_the_ban(self):
        for _ in range(security._STRIKE_MAX):
            security.record_strike(PUB, route="login")
        self.assertIsNotNone(security.blocked_seconds(PUB))
        security.autoban_clear(PUB)
        self.assertIsNone(security.blocked_seconds(PUB))

    def test_disabled_flag_never_bans(self):
        with mock.patch.dict(os.environ, {"OIKONOME_AUTOBAN": "0"}):
            for _ in range(security._STRIKE_MAX + 5):
                security.record_strike(PUB, route="login")
            self.assertIsNone(security.blocked_seconds(PUB))

    def test_limit_429_records_a_strike(self):
        from fastapi import HTTPException
        dep = security.limit(f"ab-{id(self)}", 1, 60)
        dep(_req(peer=PUB))                       # 1st ok
        with self.assertRaises(HTTPException):
            dep(_req(peer=PUB))                   # 2nd → 429 → strike
        self.assertEqual(
            security._limiter.count(("strike", PUB), security._STRIKE_WINDOW), 1)


class BanEscalationSingleCrossingTests(unittest.TestCase):
    """One threshold crossing escalates once.

    A burst of requests from a single IP can all read the strike count at or
    over the threshold before any of them clears the bucket, so every one of
    them reaches the escalation block for what is ONE logical offense. Each
    must not count as a separate prior ban: a first offense stays at the
    15-minute base, never jumping toward the 24-hour cap because the
    offender happened to send requests in parallel (CGNAT users share the
    penalty, which makes over-escalation collateral damage, not deterrence).
    """

    def setUp(self):
        security._limiter._hits.clear()
        security._limiter._blocks.clear()
        # force the in-memory path so the test needs no Redis
        self._p = mock.patch.object(security, "_redis_limiter", False)
        self._p.start()
        self._env = mock.patch.dict(
            os.environ, {"OIKONOME_TRUSTED_PROXIES": "", "OIKONOME_AUTOBAN": "1"})
        self._env.start()

    def tearDown(self):
        self._p.stop()
        self._env.stop()

    def test_a_burst_crossing_gets_the_base_ban_not_an_escalated_one(self):
        # Deterministic replay of the concurrent interleaving: the bucket is
        # already at the threshold and no caller's clear has landed yet, so
        # every call reads the count as over the line.
        for _ in range(security._STRIKE_MAX):
            security._limiter.add(("strike", PUB), security._STRIKE_WINDOW)
        with mock.patch.object(security._limiter, "clear"):
            for _ in range(5):
                security.record_strike(PUB, route="login")
        rem = security.blocked_seconds(PUB)
        self.assertIsNotNone(rem)
        self.assertLessEqual(rem, security._BAN_BASE)
        # exactly one ban in the history — future escalation stays honest
        self.assertEqual(
            security._limiter.count(("banhist", PUB),
                                    security._BAN_HISTORY_WINDOW), 1)

    def test_concurrent_strikes_agree_on_a_single_first_ban(self):
        """The invariant under a real interleaving: however the burst lands,
        a first offense never exceeds the base ban."""
        import threading
        for _ in range(security._STRIKE_MAX):
            security._limiter.add(("strike", PUB), security._STRIKE_WINDOW)
        barrier = threading.Barrier(8)

        def hit():
            barrier.wait()
            security.record_strike(PUB, route="login")

        threads = [threading.Thread(target=hit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rem = security.blocked_seconds(PUB)
        self.assertIsNotNone(rem)
        self.assertLessEqual(rem, security._BAN_BASE)

    def test_a_genuine_second_offense_still_escalates(self):
        """The fix must not blunt real escalation: a ban that expires and a
        fresh crossing afterwards is a SECOND offense and earns a longer ban."""
        for _ in range(security._STRIKE_MAX):
            security.record_strike(PUB, route="login")
        first = security.blocked_seconds(PUB)
        # the ban runs out; the offender comes back and crosses again
        security._limiter._blocks.clear()
        security._limiter.clear(("strike", PUB))
        for _ in range(security._STRIKE_MAX):
            security.record_strike(PUB, route="login")
        second = security.blocked_seconds(PUB)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertGreater(second, first)


class PwnedPasswordTests(unittest.TestCase):
    def _resp(self, text):
        return types.SimpleNamespace(text=text, raise_for_status=lambda: None)

    def test_range_match_returns_count(self):
        # SHA1("password") = 5BAA61E4C9B93F3F0682250B6CF8331B7EE68FD8
        # prefix 5BAA6, suffix 1E4C9B93F3F0682250B6CF8331B7EE68FD8
        body = "1E4C9B93F3F0682250B6CF8331B7EE68FD8:99\nAAAA:3"
        with mock.patch("httpx.get", return_value=self._resp(body)):
            self.assertEqual(passwords.pwned_count("password"), 99)

    def test_unseen_returns_zero(self):
        with mock.patch("httpx.get", return_value=self._resp("AAAA:3")):
            self.assertEqual(passwords.pwned_count("password"), 0)

    def test_network_error_fails_open_to_none(self):
        with mock.patch("httpx.get", side_effect=RuntimeError("down")):
            self.assertIsNone(passwords.pwned_count("password"))

    def test_password_error_skips_check_when_disabled(self):
        with mock.patch.dict(os.environ, {"OIKONOME_PWNED_CHECK": ""}), \
             mock.patch.object(passwords, "pwned_count") as pc:
            self.assertIsNone(passwords.password_error("a-long-enough-pw"))
            pc.assert_not_called()

    def test_password_error_blocks_a_breached_password(self):
        with mock.patch.dict(os.environ, {"OIKONOME_PWNED_CHECK": "1"}), \
             mock.patch.object(passwords, "pwned_count", return_value=42):
            self.assertIn("breach", passwords.password_error("a-long-enough-pw"))

    def test_password_error_fails_open_on_none(self):
        with mock.patch.dict(os.environ, {"OIKONOME_PWNED_CHECK": "1"}), \
             mock.patch.object(passwords, "pwned_count", return_value=None):
            self.assertIsNone(passwords.password_error("a-long-enough-pw"))

    def test_length_policy_still_applies_first(self):
        with mock.patch.dict(os.environ, {"OIKONOME_PWNED_CHECK": "1"}), \
             mock.patch.object(passwords, "pwned_count") as pc:
            self.assertIn("at least", passwords.password_error("short"))
            pc.assert_not_called()               # never reach the network


if __name__ == "__main__":
    unittest.main()
