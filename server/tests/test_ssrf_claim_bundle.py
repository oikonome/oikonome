"""SSRF: the second hop and the other door.

`netguard` is the SSRF policy for tenant-supplied URLs. Two paths can reach
around it:

Second hop — `claim_setup_token` checks the CLAIM url, then returns the claim
RESPONSE as the permanent access URL. That URL is persisted and GET on every
sync forever, so checking only the door we knock on leaves the address that
door hands back unchecked.

Other door — `.oikx` bundle import whitelists config KEY NAMES; without
checking VALUES too, an imported file can set `llm_url` (netguarded when
saved via Settings) or a SimpleFIN access token to an internal address. Same
guard, different door.

The metadata IP is blocked in BOTH modes, so most of these need no env
juggling; the private-address cases set OIKONOME_HOSTED because self-host
deliberately ALLOWS private addresses (a local LLM is a supported setup).
"""

import base64
import json
import os
import unittest
from unittest import mock

import httpx

from oikonome.sync import connections_bundle as cb
from oikonome.sync import simplefin
from oikonome.web import netguard

from .util import make_db, write_config


class CgnatSsrfTests(unittest.TestCase):
    """An SSRF guard that enumerates is_private/loopback misses RFC 6598
    CGNAT 100.64.0.0/10, a range VPN overlays use; `not is_global` covers
    it and the IPv4-mapped form."""

    def _blocked(self, ip):
        with self.assertRaises(netguard.BlockedURL):
            netguard._check_ip(ip, what="the URL", hosted=True)

    def test_cgnat_and_tailnet_blocked_on_hosted(self):
        for ip in ("100.64.0.1", "100.100.100.100", "100.127.255.254",
                   "::ffff:100.64.1.1"):
            self._blocked(ip)

    def test_still_blocks_the_known_ranges(self):
        for ip in ("169.254.169.254", "10.1.2.3", "127.0.0.1", "fd00::1"):
            self._blocked(ip)

    def test_public_still_allowed(self):
        for ip in ("1.1.1.1", "140.82.112.3", "2606:4700:4700::1111"):
            netguard._check_ip(ip, what="the URL", hosted=True)  # no raise

    def test_self_host_allows_private(self):
        netguard._check_ip("100.64.0.1", what="x", hosted=False)  # no raise

METADATA = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
INTERNAL = "http://10.1.2.3:6379/"


def _claim_returning(access_url: str):
    """A claim endpoint whose URL is perfectly public but whose RESPONSE
    hands back somewhere internal — the second-hop shape."""
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, text=access_url)
        return httpx.Response(200, text=json.dumps({"accounts": []}))
    return httpx.MockTransport(handler)


class SimplefinAccessUrlTests(unittest.TestCase):
    def test_claim_response_pointing_at_metadata_is_rejected(self):
        token = base64.b64encode(b"https://bridge.test/claim/abc").decode()
        with self.assertRaises(ValueError) as e:
            simplefin.claim_setup_token(
                token, transport=_claim_returning(METADATA))
        self.assertIn("metadata", str(e.exception).lower())

    def test_claim_response_pointing_internal_is_rejected_when_hosted(self):
        token = base64.b64encode(b"https://bridge.test/claim/abc").decode()
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            with self.assertRaises(ValueError):
                simplefin.claim_setup_token(
                    token, transport=_claim_returning(INTERNAL))

    def test_claim_happy_path_still_returns_the_access_url(self):
        # the guard must not break real onboarding
        token = base64.b64encode(b"https://bridge.test/claim/abc").decode()
        url = simplefin.claim_setup_token(
            token, transport=_claim_returning("https://u:p@bridge.test/sf"))
        self.assertEqual(url, "https://u:p@bridge.test/sf")

    def test_fetch_re_guards_a_persisted_access_url(self):
        # the value is stored and replayed hourly, and can arrive from a
        # restore or import that never passed through claim_setup_token —
        # so the check cannot live only at claim time
        with self.assertRaises(ValueError) as e:
            simplefin.fetch(METADATA, transport=_claim_returning(METADATA))
        self.assertIn("metadata", str(e.exception).lower())

    def test_fetch_happy_path_still_works(self):
        payload = simplefin.fetch("https://u:p@bridge.test/sf",
                                  transport=_claim_returning("x"))
        self.assertEqual(payload, {"accounts": []})


class BundleImportUrlTests(unittest.TestCase):
    def _db(self):
        conn = make_db()
        self.addCleanup(conn.close)
        return conn

    def _payload(self, **kw):
        p = {"oikonome_config": cb.FORMAT, "config": {}, "secrets": {},
             "items": []}
        p.update(kw)
        return p

    def test_bundle_llm_url_at_metadata_is_rejected(self):
        conn = self._db()
        write_config(conn)
        with self.assertRaises(ValueError) as e:
            cb.apply_payload(conn, self._payload(
                config={"llm_url": METADATA, "llm_model": "qwen"}))
        self.assertIn("metadata", str(e.exception).lower())

    def test_bundle_llm_url_internal_is_rejected_when_hosted(self):
        conn = self._db()
        write_config(conn)
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            with self.assertRaises(ValueError):
                cb.apply_payload(conn, self._payload(
                    config={"llm_url": INTERNAL}))

    def test_bundle_simplefin_access_token_at_metadata_is_rejected(self):
        conn = self._db()
        write_config(conn)
        with self.assertRaises(ValueError) as e:
            cb.apply_payload(conn, self._payload(items=[
                {"id": "sfin-main", "aggregator": "simplefin",
                 "institution_name": "SimpleFIN", "access_token": METADATA}]))
        self.assertIn("metadata", str(e.exception).lower())

    def test_bundle_rejection_does_not_half_apply_the_item(self):
        # a blocked item must not be upserted — fail at the door, not after
        conn = self._db()
        write_config(conn)
        with self.assertRaises(ValueError):
            cb.apply_payload(conn, self._payload(items=[
                {"id": "sfin-main", "aggregator": "simplefin",
                 "institution_name": "SimpleFIN", "access_token": METADATA}]))
        self.assertIsNone(conn.execute(
            "SELECT id FROM items WHERE id='sfin-main'").fetchone())

    def test_local_llm_bundle_still_imports_on_self_host(self):
        # a local LLM on a private address is a FIRST-CLASS self-host setup —
        # the guard must not turn the supported case into an error
        conn = self._db()
        write_config(conn)
        os.environ.pop("OIKONOME_HOSTED", None)
        out = cb.apply_payload(conn, self._payload(
            config={"llm_url": "http://192.168.1.50:11434", "llm_model": "q"}))
        self.assertIn("llm", out["config"])

    def test_non_simplefin_item_token_is_not_url_checked(self):
        # a Plaid access token is an opaque string, not a URL — checking it
        # would reject every legitimate Plaid bundle
        conn = self._db()
        write_config(conn)
        out = cb.apply_payload(conn, self._payload(items=[
            {"id": "plaid-1", "aggregator": "plaid",
             "institution_name": "Chase", "access_token": "access-sandbox-x"}]))
        self.assertEqual(out["items"], 1)


if __name__ == "__main__":
    unittest.main()
