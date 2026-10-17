"""A host routed a whole IPv6 /48 (a tunnel broker hands one out free, and
many VPS and ISP plans give a /56) answers from up to 65,536 /64s. The
per-source human check keys on the /64, and the site-wide count charges the
/48 as one source that stops counting after its first few failures, so a
host rotating /64s inside its /48 would otherwise spray passwords forever
without meeting either check. The /48 must be challenged as one source once
it has failed far more than one customer plausibly could — and only the
/48: a different network is never challenged for it."""
import os
import unittest
from unittest import mock

from oikonome.web import security

PREFIX = "2001:db8:4a2c"            # the /48 under test (documentation range)


class OneIPv6SlashFortyEightIsChallengedAsOneSourceTests(unittest.TestCase):
    def setUp(self):
        security._limiter._hits.clear()
        security._limiter._blocks.clear()
        for p in (mock.patch.object(security, "_redis_limiter", False),
                  mock.patch.dict(os.environ,
                                  {"OIKONOME_TRUSTED_PROXIES": ""})):
            p.start()
            self.addCleanup(p.stop)

    def _spray(self, n_64s, per_64):
        for i in range(n_64s):
            for _ in range(per_64):
                security.record_challenge_risk(f"{PREFIX}:{i:x}::1",
                                               f"victim{i}@example.com")

    def test_a_48_rotating_its_64s_is_challenged_on_a_fresh_64(self):
        # stay under the per-/64 threshold on every /64, so only breadth
        # across the /48 can trip anything
        self._spray(60, security._RISK_IP_MAX - 1)
        self.assertTrue(
            security.challenge_warranted(f"{PREFIX}:ffff::1"),
            "a /64 the host has not used yet is inside the same /48")
        self.assertTrue(
            security.challenge_warranted(f"{PREFIX}:ffff::1",
                                         "someone-new@example.com"),
            "and naming a fresh account does not get around it")

    def test_the_48_is_challenged_before_the_site_could_ever_arm(self):
        n = security._RISK_IP48_MAX
        for i in range(n):
            security.record_challenge_risk(f"{PREFIX}:{i:x}::1")
        self.assertTrue(security.challenge_warranted(f"{PREFIX}:beef::1"))
        self.assertLess(security._RISK_IP48_MAX, security._RISK_SITE_MAX)

    def test_a_few_failures_in_a_48_do_not_challenge_its_neighbours(self):
        # an office or carrier pool sharing a /48: a couple of customers
        # mistyping is not a reason to challenge the rest of them
        self._spray(security._RISK_IP48_MAX // security._RISK_IP_MAX - 1,
                    security._RISK_IP_MAX)
        self.assertFalse(security.challenge_warranted(f"{PREFIX}:ffff::1"))

    def test_another_network_is_never_challenged_for_the_48(self):
        self._spray(60, security._RISK_IP_MAX)
        self.assertFalse(security.challenge_warranted("2001:db8:4a2d::1"))
        self.assertFalse(security.challenge_warranted("198.51.100.7"))

    def test_ipv4_and_unparseable_sources_keep_their_one_bucket(self):
        for _ in range(security._RISK_IP_MAX - 1):
            security.record_challenge_risk("198.51.100.7")
            security.record_challenge_risk("testclient")
        self.assertFalse(security.challenge_warranted("198.51.100.7"))
        self.assertFalse(security.challenge_warranted("testclient"))
        security.record_challenge_risk("198.51.100.7")
        self.assertTrue(security.challenge_warranted("198.51.100.7"))


if __name__ == "__main__":
    unittest.main()
