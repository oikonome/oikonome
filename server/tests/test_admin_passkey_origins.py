"""The console's passkey origin set follows the same web rule as a
household's: https always, plain http only for localhost.

Console login, enrolment and step-up all verify against this set. A
server-side http:// accept for a public host can never admit a legitimate
login (browsers refuse WebAuthn on insecure public origins); it only weakens
downgrade resistance. The https twin of a derived http origin stays, because
a proxy that drops X-Forwarded-Proto reports http for a page served over
https.
"""

import unittest

from oikonome.auth import admin_passkeys, passkeys


class ConsoleOriginTests(unittest.TestCase):
    def test_public_host_never_accepts_plain_http(self):
        got = admin_passkeys._origins("https://console.example.test")
        self.assertIn("https://console.example.test", got)
        self.assertNotIn("http://console.example.test", got)
        for o in got:
            self.assertTrue(o.startswith("https://"), o)

    def test_derived_http_origin_still_verifies_via_its_https_twin(self):
        got = admin_passkeys._origins("http://console.example.test")
        self.assertIn("https://console.example.test", got)
        self.assertNotIn("http://console.example.test", got)

    def test_localhost_keeps_http_for_dev(self):
        for origin in ("http://localhost:8042", "http://127.0.0.1:8042"):
            self.assertIn(origin, admin_passkeys._origins(origin))

    def test_ports_ride_along(self):
        got = admin_passkeys._origins("https://console.example.test:8443")
        self.assertEqual(got, ["https://console.example.test:8443"])

    def test_matches_the_household_web_rule(self):
        for origin in ("https://console.example.test",
                       "http://console.example.test",
                       "http://localhost:8042"):
            web = [o for o in passkeys._expected_origins(origin)
                   if not o.startswith("android:")]
            self.assertEqual(admin_passkeys._origins(origin), web)


if __name__ == "__main__":
    unittest.main()
