"""A loopback-only published port trusts its own forwarder, and nothing else.

Under rootless Podman the port forwarder hands every host connection to the
app with the CONTAINER'S OWN address as the peer, so a host reverse proxy
can never be matched by OIKONOME_TRUSTED_PROXIES and every client collapses
onto that one address: auto-ban never fires, per-IP limits are shared by
everyone, and an OIKONOME_ADMIN_IPS allowlist sees the wrong address. When
the port is published on loopback only, only host-local processes can reach
it, so the forwarded headers are believed from the container's own address.
On any other bind the same forwarder also carries LAN clients, and believing
their X-Forwarded-For would let each of them choose its own address.
"""

import os
import unittest
from unittest import mock

from starlette.requests import Request

from oikonome.web import security

OWN = "192.0.2.7"          # the container's own address on its bridge


def _req(peer, xff=None, proto=None, server=OWN):
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    if proto is not None:
        headers.append((b"x-forwarded-proto", proto.encode()))
    return Request({"type": "http", "method": "GET", "path": "/",
                    "scheme": "http", "headers": headers,
                    "client": (peer, 40000), "server": (server, 8080)})


class LoopbackBindForwarderTests(unittest.TestCase):
    def setUp(self):
        env = {k: v for k, v in os.environ.items()
               if k not in ("OIKONOME_TRUSTED_PROXIES",
                            "OIKONOME_BIND_ADDRESS")}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        own = mock.patch.object(security, "_own_addrs",
                                frozenset({"127.0.0.1", "::1", OWN}),
                                create=True)
        own.start()
        self.addCleanup(own.stop)

    def test_loopback_bind_resolves_the_client_behind_the_host_proxy(self):
        os.environ["OIKONOME_BIND_ADDRESS"] = "127.0.0.1"
        self.assertEqual(
            security.client_ip(_req(OWN, xff="203.0.113.9")), "203.0.113.9")

    def test_loopback_bind_believes_the_forwarded_scheme(self):
        os.environ["OIKONOME_BIND_ADDRESS"] = "127.0.0.1"
        self.assertEqual(
            security.request_scheme(_req(OWN, proto="https")), "https")

    def test_loopback_bind_takes_the_rightmost_hop_not_a_forged_left(self):
        os.environ["OIKONOME_BIND_ADDRESS"] = "127.0.0.1"
        self.assertEqual(security.client_ip(
            _req(OWN, xff="198.51.100.1, 203.0.113.9")), "203.0.113.9")

    def test_peer_matching_the_socket_local_address_counts_as_own(self):
        os.environ["OIKONOME_BIND_ADDRESS"] = "127.0.0.1"
        self.assertEqual(security.client_ip(
            _req("192.0.2.30", xff="203.0.113.9", server="192.0.2.30")),
            "203.0.113.9")

    def test_every_interface_bind_refuses_the_same_forgery(self):
        for bind in ("", "0.0.0.0", "192.0.2.10"):
            os.environ["OIKONOME_BIND_ADDRESS"] = bind
            self.assertEqual(
                security.client_ip(_req(OWN, xff="203.0.113.9")), OWN, bind)
            self.assertEqual(
                security.request_scheme(_req(OWN, proto="https")), "http",
                bind)

    def test_loopback_bind_does_not_trust_another_container_address(self):
        os.environ["OIKONOME_BIND_ADDRESS"] = "127.0.0.1"
        self.assertEqual(security.client_ip(
            _req("192.0.2.5", xff="203.0.113.9")), "192.0.2.5")

    def test_listed_proxies_still_work_alongside(self):
        os.environ["OIKONOME_TRUSTED_PROXIES"] = "192.0.2.5"
        self.assertEqual(security.client_ip(
            _req("192.0.2.5", xff="203.0.113.9")), "203.0.113.9")


class OwnAddressEnumerationTests(unittest.TestCase):
    def test_enumerates_loopback_and_logs_what_it_trusts(self):
        with mock.patch.object(security, "_own_addrs", None), \
                mock.patch.object(security._seclog, "warning") as info:
            got = security._own_addresses()
            again = security._own_addresses()
        self.assertIn("127.0.0.1", got)
        self.assertIs(got, again)
        self.assertEqual(info.call_count, 1)
        self.assertIn("forwarder_trusted", info.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
