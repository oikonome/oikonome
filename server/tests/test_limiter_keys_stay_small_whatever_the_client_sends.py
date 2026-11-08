"""A limiter key costs the same however long the text a client sent.

The anonymous sign-in doors turn the email a client typed into per-account
buckets (the second-factor lock, the human-check risk signal). If that text
went into the key as-is, a client choosing a megabyte of "email" would
choose how much memory each key holds — in the process and in Redis — and
the key-count ceiling that bounds the table would bound nothing. So every
per-account key carries a fixed-size digest of the normalized address, and
the limiter itself shrinks any oversized key part, so a future door that
forgets cannot bring the class back.
"""

import unittest
from unittest import mock

from oikonome.web import security

HUGE = "a" * 100_000 + "@example.com"
BOUND = 128


def _parts(key) -> list[str]:
    return [str(p) for p in key]


class InProcessKeysAreBounded(unittest.TestCase):
    def setUp(self):
        security._limiter._hits.clear()
        security._limiter._blocks.clear()
        self._p = mock.patch.object(security, "_redis_limiter", False)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        security._limiter._hits.clear()
        security._limiter._blocks.clear()

    def _assert_bounded(self):
        self.assertTrue(security._limiter._hits)
        for key in security._limiter._hits:
            for part in _parts(key):
                self.assertLessEqual(len(part), BOUND, key[0])

    def test_a_failed_sign_in_with_a_huge_email_stores_small_keys(self):
        security.record_auth_failure("198.51.100.7", HUGE)
        security.record_login_failure(HUGE)
        security.reserve_login_guess(HUGE)
        self._assert_bounded()

    def test_any_oversized_key_part_is_shrunk_by_the_limiter_itself(self):
        rl = security.RateLimiter()
        rl.add(("some-future-door", HUGE), 60)
        rl.check(("another-door", HUGE), 5, 60)
        rl.block(("ip", HUGE), 60)
        for key in list(rl._hits) + list(rl._blocks):
            for part in _parts(key):
                self.assertLessEqual(len(part), BOUND)
        # and the shrunk key is still the same bucket on the next call
        self.assertEqual(rl.count(("some-future-door", HUGE), 60), 1)
        self.assertIsNotNone(rl.blocked(("ip", HUGE)))

    def test_the_same_person_is_still_one_bucket(self):
        security.record_login_failure(" Victim@Example.COM ")
        security.record_login_failure("victim@example.com")
        self.assertEqual(
            security._dual("count", security._acct_key("VICTIM@example.com"),
                           security._ACCT_FAIL_WINDOW), 2)
        security.record_challenge_risk("", "Victim@Example.com")
        acct = security._risk_keys("", "victim@example.com")[-1][0]
        self.assertEqual(security._risk_count(acct), 1)
        security.clear_challenge_risk("", "VICTIM@EXAMPLE.COM")
        self.assertEqual(security._risk_count(acct), 0)

    def test_per_account_keys_never_carry_the_address(self):
        """Redis holds key names in the clear; an address is personal."""
        for key in (security._acct_key("victim@example.com"),
                    security._risk_keys("", "victim@example.com")[-1][0]):
            self.assertNotIn("victim", ":".join(_parts(key)))


class RedisKeyNamesAreBounded(unittest.TestCase):
    def setUp(self):
        self.lim = security.RedisRateLimiter.__new__(security.RedisRateLimiter)

    def test_per_account_key_names_are_small(self):
        for key in (security._acct_key(HUGE),
                    security._risk_keys("", HUGE)[-1][0]):
            self.assertLessEqual(len(self.lim._k(key)), 2 * BOUND)

    def test_any_oversized_part_is_shrunk_in_the_key_name(self):
        self.assertLessEqual(len(self.lim._k(("future-door", HUGE))),
                             2 * BOUND)
        self.assertLessEqual(len(self.lim._bk(("ip", HUGE))), 2 * BOUND)
        self.assertEqual(self.lim._k(("future-door", HUGE)),
                         self.lim._k(("future-door", HUGE)))
        # a short part is untouched, so existing buckets keep their names
        self.assertEqual(self.lim._k(("route", "198.51.100.7")),
                         "oikonome:rl:route:198.51.100.7")


if __name__ == "__main__":
    unittest.main()
