"""Passkey RP derivation honors the trusted-proxy gate.

`_rp()` binds WebAuthn credentials to an origin, so the forwarded
host/proto headers it reads must pass the same trust gate as every
other proxy-header consumer: believed only from a peer listed in
OIKONOME_TRUSTED_PROXIES, rightmost entry wins on an appended chain.
Believing ANY peer would let a direct client bind credentials to a forged
origin."""

import os
import unittest

from starlette.requests import Request as StarRequest


def _req(headers: dict, peer: str = "203.0.113.9") -> StarRequest:
    scope = {
        "type": "http", "method": "GET", "path": "/", "scheme": "http",
        "query_string": b"", "server": ("app.internal", 8080),
        "client": (peer, 4711),
        "headers": [(k.lower().encode(), v.encode())
                    for k, v in headers.items()],
    }
    return StarRequest(scope)


class PasskeyRpTrustTests(unittest.TestCase):
    """Taking X-Forwarded-Host / X-Forwarded-Proto from ANY peer is
    inconsistent with the trusted-proxy policy and enough to bind WebAuthn
    credentials to a foreign origin. `_rp()` must mirror request_scheme's
    trust gate; without it, test_untrusted_peer_headers_ignored fails and
    rp_id comes back as the forged host."""

    def setUp(self):
        os.environ.pop("OIKONOME_TRUSTED_PROXIES", None)

    def tearDown(self):
        os.environ.pop("OIKONOME_TRUSTED_PROXIES", None)

    def test_untrusted_peer_headers_ignored(self):
        from oikonome.web.app import _rp
        rp_id, origin = _rp(_req({"host": "money.example.com",
                                  "x-forwarded-host": "evil.example.net",
                                  "x-forwarded-proto": "https"}))
        self.assertEqual(rp_id, "money.example.com")
        self.assertEqual(origin, "http://money.example.com")

    def test_trusted_proxy_headers_honored(self):
        from oikonome.web.app import _rp
        os.environ["OIKONOME_TRUSTED_PROXIES"] = "10.0.0.0/8"
        rp_id, origin = _rp(_req({"host": "backend.internal:8080",
                                  "x-forwarded-host": "money.example.com",
                                  "x-forwarded-proto": "https"},
                                 peer="10.1.2.3"))
        self.assertEqual(rp_id, "money.example.com")
        self.assertEqual(origin, "https://money.example.com")

    def test_trusted_proxy_without_headers_falls_back(self):
        from oikonome.web.app import _rp
        os.environ["OIKONOME_TRUSTED_PROXIES"] = "10.0.0.0/8"
        rp_id, origin = _rp(_req({"host": "money.example.com"},
                                 peer="10.1.2.3"))
        self.assertEqual(rp_id, "money.example.com")
        self.assertEqual(origin, "http://money.example.com")

    def test_appended_chain_rightmost_wins(self):
        """An appending proxy leaves client forgeries on the LEFT of the
        chain — the rightmost entry is the nearest trusted hop's word."""
        from oikonome.web.app import _rp
        os.environ["OIKONOME_TRUSTED_PROXIES"] = "10.0.0.0/8"
        rp_id, _ = _rp(_req({"host": "backend.internal",
                             "x-forwarded-host":
                                 "evil.example.net, money.example.com"},
                            peer="10.1.2.3"))
        self.assertEqual(rp_id, "money.example.com")


if __name__ == "__main__":
    unittest.main()
