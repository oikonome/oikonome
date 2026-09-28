"""An Android app an operator names must be able to sign in with a passkey.

Two server-side halves make that work, and both are pinned here:

- ``/.well-known/assetlinks.json`` — Digital Asset Links. Android's
  Credential Manager fetches it anonymously from the server's domain
  before offering that domain's passkeys to the app, so it must be
  public, unauthenticated, and present on every install.
- WebAuthn origin acceptance — a native app has no page origin;
  Credential Manager signs ``android:apk-key-hash:<b64url(sha256 of the
  signing cert)>`` instead. Verification must accept exactly the
  certificates the operator configured and nothing else.

The identity is configuration (OIKONOME_ANDROID_PACKAGE,
OIKONOME_ANDROID_CERT_SHA256): a server with no app of its own publishes an
empty statement and accepts no app origin.
"""

import base64
import hashlib
import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.auth import passkeys

PACKAGE = "org.example.oikonome"


def _fingerprint(seed: bytes) -> str:
    digest = hashlib.sha256(seed).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, 64, 2))


def _origin(fp: str) -> str:
    raw = bytes.fromhex(fp.replace(":", ""))
    return "android:apk-key-hash:" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


# two invented signing certificates: an upload key for sideloads and a
# store's app-signing key for store installs
UPLOAD_FP = _fingerprint(b"example upload key")
STORE_FP = _fingerprint(b"example store signing key")
ENV = {"OIKONOME_ANDROID_PACKAGE": PACKAGE,
       "OIKONOME_ANDROID_CERT_SHA256": f"{UPLOAD_FP},{STORE_FP.lower()}"}


class AndroidOriginTests(unittest.TestCase):
    def test_both_signing_certs_yield_their_exact_origins(self):
        with mock.patch.dict(os.environ, ENV):
            self.assertEqual(passkeys.android_app_origins(),
                             [_origin(UPLOAD_FP), _origin(STORE_FP)])

    def test_login_origin_set_accepts_the_app_alongside_https(self):
        with mock.patch.dict(os.environ, ENV):
            got = passkeys._expected_origins("https://money.example.org")
        self.assertIn("https://money.example.org", got)
        self.assertIn(_origin(UPLOAD_FP), got)
        self.assertIn(_origin(STORE_FP), got)

    def test_no_other_apk_key_hash_is_accepted(self):
        # a different signing cert (say, a repackaged APK) must not be in
        # the accepted set — only the configured digests
        with mock.patch.dict(os.environ, ENV):
            got = passkeys._expected_origins("https://money.example.org")
        forged = _origin(_fingerprint(b"repackaged"))
        self.assertNotIn(forged, got)
        self.assertEqual(
            [o for o in got if o.startswith("android:apk-key-hash:")],
            [_origin(UPLOAD_FP), _origin(STORE_FP)])

    def test_a_malformed_fingerprint_is_dropped_not_published(self):
        # a SHA-1 fingerprint (20 bytes) or free text is not a certificate
        # digest; publishing it would break the statement for every app
        sha1 = ":".join(["AB"] * 20)
        with mock.patch.dict(os.environ, {**ENV, "OIKONOME_ANDROID_CERT_SHA256":
                                          f"{UPLOAD_FP}, {sha1} ,junk"}):
            self.assertEqual(passkeys.android_cert_fingerprints(), [UPLOAD_FP])

    def test_no_configured_app_means_no_app_origin(self):
        with mock.patch.dict(os.environ, {"OIKONOME_ANDROID_PACKAGE": "",
                                          "OIKONOME_ANDROID_CERT_SHA256": ""}):
            got = passkeys._expected_origins("https://money.example.org")
        self.assertEqual([o for o in got if o.startswith("android:")], [])


class AssetlinksRouteTests(unittest.TestCase):
    def _get(self):
        from oikonome.web.app import app
        return TestClient(app).get("/.well-known/assetlinks.json")

    def test_served_unauthenticated_with_both_certs(self):
        with mock.patch.dict(os.environ, {**ENV, "OIKONOME_ANDROID_EXTRA_PACKAGES": ""}):
            r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("application/json"))
        (stmt,) = r.json()
        self.assertEqual(stmt["target"]["package_name"], PACKAGE)
        self.assertEqual(stmt["target"]["sha256_cert_fingerprints"],
                         [UPLOAD_FP, STORE_FP])

    def test_declares_login_creds_and_app_links_relations(self):
        with mock.patch.dict(os.environ, ENV):
            (stmt,) = self._get().json()
        self.assertIn("delegate_permission/common.get_login_creds", stmt["relation"])
        self.assertIn("delegate_permission/common.handle_all_urls", stmt["relation"])

    def test_a_renamed_build_can_be_trusted_by_naming_it(self):
        """Credential Manager gates on the package as well as the cert, so
        a side-by-side dev variant with its own application id is refused
        until the server names it — with the same certificates."""
        with mock.patch.dict(os.environ, {**ENV, "OIKONOME_ANDROID_EXTRA_PACKAGES":
                                          f"{PACKAGE}.dev"}):
            stmts = self._get().json()
        self.assertEqual([s["target"]["package_name"] for s in stmts],
                         [PACKAGE, f"{PACKAGE}.dev"])
        for s in stmts:
            self.assertEqual(s["target"]["sha256_cert_fingerprints"],
                             [UPLOAD_FP, STORE_FP])

    def test_only_real_package_names_reach_the_statement(self):
        """This file is a published trust grant — it must not echo
        whatever the env happens to contain."""
        with mock.patch.dict(os.environ, {**ENV, "OIKONOME_ANDROID_EXTRA_PACKAGES":
                                          f"not a package, ../etc/passwd ,{PACKAGE}.dev,,{PACKAGE}"}):
            names = [s["target"]["package_name"] for s in self._get().json()]
        # the junk is dropped and the main package is not doubled
        self.assertEqual(names, [PACKAGE, f"{PACKAGE}.dev"])

    def test_the_default_grants_nothing(self):
        """A server with no app configured publishes an empty statement:
        no package, no certificate, nothing a phone could trust."""
        with mock.patch.dict(os.environ, {"OIKONOME_ANDROID_PACKAGE": "",
                                          "OIKONOME_ANDROID_CERT_SHA256": "",
                                          "OIKONOME_ANDROID_EXTRA_PACKAGES": f"{PACKAGE}.dev"}):
            r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), [])


if __name__ == "__main__":
    unittest.main()
