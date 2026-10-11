"""An endpoint URL's query credential is masked whatever its key is called.

Settings (readable by a view-only member), the export's config scrub, the
run record and the Doctor page all show AI endpoint URLs through redact_url.
A proxy that takes its key in the query string names it as it likes, so a
credential-shaped name (one containing key, token, secret, password, auth,
signature or credential) must be masked, not only an exact list of names.
A plain parameter stays readable, and a round-trip save keeps the stored
credential.
"""

import unittest

from oikonome.engine import llm_categorize as lc

BASE = "https://apim.example.test/openai/v1"


class QueryCredentialsMasked(unittest.TestCase):
    def test_credential_shaped_names_are_masked(self):
        for name in ("subscription-key", "x-api-key", "api_token",
                     "auth_token", "X-Amz-Signature", "client_credential",
                     "key", "Token"):
            with self.subTest(name=name):
                out = lc.redact_url(f"{BASE}?{name}=LIVEKEY123&model=m")
                self.assertNotIn("LIVEKEY123", out)
                self.assertIn("model=m", out)

    def test_plain_parameters_stay_readable(self):
        url = f"{BASE}?model=llama3&api-version=2024-01-01"
        self.assertEqual(lc.redact_url(url), url)

    def test_a_masked_round_trip_keeps_the_stored_credential(self):
        stored = f"{BASE}?subscription-key=LIVEKEY123"
        self.assertEqual(lc.unredact_url(lc.redact_url(stored), [stored]),
                         stored)


if __name__ == "__main__":
    unittest.main()
