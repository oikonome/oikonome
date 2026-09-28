"""The app warns once when every client looks like the container network.

Under rootless Podman the app sees its own pod-network address as the peer
of every connection. Auto-ban, the per-IP limits and OIKONOME_ADMIN_IPS then
all act on one shared address: bans never fire and a LAN allowlist locks
everyone out. The app cannot recover the real address, so it must at least
say so in the log — once per process, on the first non-loopback request,
and only when one of those address-based features is configured.
"""

import logging
import os
import unittest
from unittest import mock

from starlette.requests import Request

from oikonome.web import security


def _req(peer: str, server: str = "10.88.0.7") -> Request:
    return Request({"type": "http", "method": "GET", "path": "/",
                    "headers": [], "client": (peer, 40000),
                    "server": (server, 8080)})


class PodAddressWarningTests(unittest.TestCase):
    def setUp(self):
        security._pod_peer_checked = False
        self.addCleanup(setattr, security, "_pod_peer_checked", False)
        env = {k: v for k, v in os.environ.items()
               if k not in ("OIKONOME_AUTOBAN", "OIKONOME_ADMIN_IPS")}
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def _warnings(self, *requests):
        logger = logging.getLogger("oikonome.security")
        with mock.patch.object(logger, "warning") as w:
            for r in requests:
                security.note_first_client(r, security.client_ip(r))
        return [c for c in w.call_args_list
                if "client_address_hidden" in str(c.args[0])]

    def test_pod_network_peer_is_reported_once(self):
        got = self._warnings(_req("10.88.0.1"), _req("10.89.0.4"))
        self.assertEqual(len(got), 1)

    def test_peer_equal_to_the_containers_own_address_is_reported(self):
        got = self._warnings(_req("10.0.2.100", server="10.0.2.100"))
        self.assertEqual(len(got), 1)

    def test_real_client_address_settles_it_without_a_warning(self):
        got = self._warnings(_req("192.0.2.40"), _req("10.88.0.1"))
        self.assertEqual(got, [], "a later pod address re-opened the check")

    def test_loopback_healthcheck_does_not_settle_the_check(self):
        got = self._warnings(_req("127.0.0.1"), _req("10.88.0.1"))
        self.assertEqual(len(got), 1)

    def test_silent_when_neither_autoban_nor_admin_allowlist_is_on(self):
        os.environ["OIKONOME_AUTOBAN"] = "off"
        self.assertEqual(self._warnings(_req("10.88.0.1")), [])

    def test_admin_allowlist_alone_is_enough_to_warn(self):
        os.environ["OIKONOME_AUTOBAN"] = "off"
        os.environ["OIKONOME_ADMIN_IPS"] = "192.168.1.0/24"
        self.assertEqual(len(self._warnings(_req("10.88.0.1"))), 1)


if __name__ == "__main__":
    unittest.main()
