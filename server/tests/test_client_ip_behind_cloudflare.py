"""Behind a Cloudflare-style proxy the app must still resolve the REAL
client IP — rate limiting, lockout and the admin allowlist all key off it —
and it must not be spoofable by an attacker who reaches the origin
directly. The rightmost-untrusted-hop walk gets this right only while the
proxy's ranges are listed in OIKONOME_TRUSTED_PROXIES; leave them out and
the edge's own address becomes every visitor's identity."""

import os
import types
import unittest
from unittest import mock

from oikonome.web import security

# sample Cloudflare edge ranges + the container network Caddy sits on
CF = "104.16.0.0/13,173.245.48.0/20"
NAT = "10.88.0.0/15,172.16.0.0/12"
TRUSTED = NAT + "," + CF


def _req(peer, xff=None):
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    return types.SimpleNamespace(
        client=types.SimpleNamespace(host=peer), headers=headers)


class CloudflareClientIpTests(unittest.TestCase):
    def test_orange_cloud_resolves_the_real_client(self):
        # browser(198.51.100.7) → CF edge(104.16.0.5) → Caddy(10.88.0.5) → app.
        # CF appended the real client; Caddy appended the CF edge.
        with mock.patch.dict(os.environ, {"OIKONOME_TRUSTED_PROXIES": TRUSTED}):
            ip = security.client_ip(
                _req(peer="10.88.0.5", xff="198.51.100.7, 104.16.0.5"))
        self.assertEqual(ip, "198.51.100.7")

    def test_direct_origin_bypass_cannot_spoof_a_victim_ip(self):
        # attacker reaches Caddy directly and forges a CF-looking chain; Caddy
        # appends the attacker's REAL ip as the rightmost hop → the walk stops
        # there and the forged victim ip on the left is never returned.
        with mock.patch.dict(os.environ, {"OIKONOME_TRUSTED_PROXIES": TRUSTED}):
            ip = security.client_ip(_req(
                peer="10.88.0.5",
                xff="198.51.100.7, 104.16.0.5, 45.9.9.9"))
        self.assertEqual(ip, "45.9.9.9")     # attacker only attributes itself

    def test_untrusted_peer_ignores_forged_header_entirely(self):
        with mock.patch.dict(os.environ, {"OIKONOME_TRUSTED_PROXIES": TRUSTED}):
            ip = security.client_ip(
                _req(peer="45.9.9.9", xff="198.51.100.7, 104.16.0.5"))
        self.assertEqual(ip, "45.9.9.9")

    def test_without_cf_ranges_the_cf_edge_leaks_through(self):
        # the FAILURE mode we must avoid: if CF ranges are NOT trusted, the
        # walk stops at the CF edge and every visitor collapses to a CF ip.
        # This is why adding CF ranges to OIKONOME_TRUSTED_PROXIES is required.
        with mock.patch.dict(os.environ, {"OIKONOME_TRUSTED_PROXIES": NAT}):
            ip = security.client_ip(
                _req(peer="10.88.0.5", xff="198.51.100.7, 104.16.0.5"))
        self.assertEqual(ip, "104.16.0.5")   # regression guard for the config


if __name__ == "__main__":
    unittest.main()
