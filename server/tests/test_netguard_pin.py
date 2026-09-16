"""fetch-time DNS pinning for tenant-supplied URLs.

check_url validates a NAME; a rebinding resolver can answer a public
address at check time and 169.254.169.254 at connect time. On hosted,
netguard.pinned_transport() closes that window: it resolves once, applies
the address policy to what actually resolved, and rewrites the request URL
to the vetted IP (Host header and TLS SNI keep the original hostname).
These tests drive the transport directly with a stubbed resolver and a
captured base handle_request — no live network.
"""

import os
import unittest
from unittest import mock

import httpx

from oikonome.web import netguard

PUBLIC_IP = "93.184.216.34"


def _req(url: str) -> httpx.Request:
    # build through a client so the Host header exists, as it does when
    # the transport runs inside Client.send
    return httpx.Client().build_request("GET", url)


class PinnedTransportTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def _pin(self, resolved, url):
        """Run `url` through the pinned transport with the resolver stubbed
        to `resolved`; return the request the BASE transport would send."""
        seen = {}

        def capture(self_, request):
            seen["request"] = request
            return httpx.Response(200)

        t = netguard.pinned_transport()
        with mock.patch.object(netguard, "_resolved_ips",
                               return_value=resolved), \
             mock.patch.object(httpx.HTTPTransport, "handle_request",
                               capture):
            t.handle_request(_req(url))
        return seen["request"]

    def test_self_host_returns_none(self):
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": ""}):
            self.assertIsNone(netguard.pinned_transport())

    def test_public_resolution_pins_ip_keeps_host_and_sni(self):
        r = self._pin([PUBLIC_IP], "https://llm.example.com/v1/models")
        self.assertEqual(r.url.host, PUBLIC_IP)
        self.assertEqual(r.headers["host"], "llm.example.com")
        self.assertEqual(r.extensions["sni_hostname"], "llm.example.com")

    def test_http_gets_no_sni(self):
        r = self._pin([PUBLIC_IP], "http://llm.example.com/x")
        self.assertEqual(r.url.host, PUBLIC_IP)
        self.assertNotIn("sni_hostname", r.extensions)

    def test_rebind_to_metadata_blocked(self):
        with self.assertRaises(netguard.BlockedURL):
            self._pin(["169.254.169.254"], "http://llm.example.com/x")

    def test_rebind_to_private_blocked(self):
        with self.assertRaises(netguard.BlockedURL):
            self._pin(["10.1.2.3"], "http://llm.example.com/x")

    def test_mixed_resolution_blocked(self):
        # one public + one private A record: an attacker splitting records
        # must not win the pick — every address must pass
        with self.assertRaises(netguard.BlockedURL):
            self._pin([PUBLIC_IP, "10.1.2.3"], "http://llm.example.com/x")

    def test_unresolvable_blocked(self):
        with self.assertRaises(netguard.BlockedURL):
            self._pin([], "http://llm.example.com/x")

    def test_ipv4_preferred_over_ipv6(self):
        r = self._pin(["2606:2800:220:1::1", PUBLIC_IP],
                      "http://llm.example.com/x")
        self.assertEqual(r.url.host, PUBLIC_IP)

    def test_literal_metadata_ip_blocked(self):
        t = netguard.pinned_transport()
        with self.assertRaises(netguard.BlockedURL):
            t.handle_request(_req("http://169.254.169.254/latest"))

    def test_literal_public_ip_passes_through(self):
        seen = {}

        def capture(self_, request):
            seen["request"] = request
            return httpx.Response(200)

        t = netguard.pinned_transport()
        with mock.patch.object(httpx.HTTPTransport, "handle_request",
                               capture):
            t.handle_request(_req(f"http://{PUBLIC_IP}/x"))
        self.assertEqual(seen["request"].url.host, PUBLIC_IP)


class MetadataBlockIsSpellingProofTest(unittest.TestCase):
    """The cloud metadata IP (169.254.169.254) must be blocked in every
    spelling, on self-host as much as hosted — a self-host box often runs on
    a cloud VM whose metadata service hands out IAM credentials, and the
    kernel routes an IPv6 socket aimed at ::ffff:169.254.169.254 straight to
    the embedded v4 metadata endpoint. The IPv4-mapped IPv6 spelling of the
    metadata IP is therefore the metadata IP.
    """

    MAPPED = "::ffff:169.254.169.254"

    def _check(self, host, *, hosted):
        env = {"OIKONOME_HOSTED": "1" if hosted else ""}
        with mock.patch.dict(os.environ, env):
            netguard.check_host(host)

    def test_mapped_metadata_literal_blocked_self_host(self):
        with self.assertRaises(netguard.BlockedURL):
            self._check(self.MAPPED, hosted=False)

    def test_mapped_metadata_literal_blocked_hosted(self):
        with self.assertRaises(netguard.BlockedURL):
            self._check(self.MAPPED, hosted=True)

    def test_plain_metadata_literal_still_blocked_self_host(self):
        with self.assertRaises(netguard.BlockedURL):
            self._check("169.254.169.254", hosted=False)

    def test_mapped_metadata_via_hostname_blocked_self_host(self):
        # a tenant name whose A/AAAA record is the mapped metadata literal
        with mock.patch.object(netguard, "_resolved_ips",
                               return_value=[self.MAPPED]), \
             self.assertRaises(netguard.BlockedURL):
            self._check("llm.evil.example", hosted=False)

    def test_mapped_metadata_via_hostname_blocked_hosted(self):
        with mock.patch.object(netguard, "_resolved_ips",
                               return_value=[self.MAPPED]), \
             self.assertRaises(netguard.BlockedURL):
            self._check("llm.evil.example", hosted=True)

    def test_normal_public_url_still_allowed_self_host(self):
        # self-host allows anything but the metadata IP; a plain public
        # host must pass
        self._check("llm.example.com", hosted=False)

    def test_normal_public_url_still_allowed_hosted(self):
        with mock.patch.object(netguard, "_resolved_ips",
                               return_value=[PUBLIC_IP]):
            self._check("llm.example.com", hosted=True)


class ResolverIsBoundedAndCachedTest(unittest.TestCase):
    """A non-answering nameserver must not pin a request thread, and a
    burst for the same host must pay the DNS cost once."""

    def setUp(self):
        netguard._DNS_CACHE.clear()
        netguard._DNS_LOCKS.clear()

    def test_a_hanging_lookup_times_out_to_empty_not_forever(self):
        import time as _t

        def _hang(host):
            _t.sleep(30)          # nameserver that never answers
            return ["1.2.3.4"]

        started = _t.monotonic()
        with mock.patch.object(netguard, "_getaddrinfo", side_effect=_hang),                 mock.patch.object(netguard, "_DNS_TIMEOUT", 0.3):
            got = netguard._resolved_ips("black-hole.example")
        elapsed = _t.monotonic() - started
        self.assertEqual(got, [])                 # fail-closed
        self.assertLess(elapsed, 5.0)             # bounded, not 30s

    def test_a_resolved_host_is_cached_not_re_resolved(self):
        calls = []

        def _once(host):
            calls.append(host)
            return ["203.0.113.7"]

        with mock.patch.object(netguard, "_getaddrinfo", side_effect=_once):
            a = netguard._resolved_ips("cache-me.example")
            b = netguard._resolved_ips("cache-me.example")
        self.assertEqual(a, b)
        self.assertEqual(len(calls), 1, "the second lookup must hit cache")


if __name__ == "__main__":
    unittest.main()
