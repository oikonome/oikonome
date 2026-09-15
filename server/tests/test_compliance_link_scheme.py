"""An obligation's link is an anchor href every household member renders.

Nothing checked its scheme, so `javascript:` stored on one obligation ran
for whoever opened the Business page — stored XSS inside the tenant, written
by an owner and executed by everyone.
"""

import unittest

from oikonome.engine import compliance


class ObligationLinkSchemeTests(unittest.TestCase):

    def test_a_javascript_url_is_refused(self):
        for bad in ("javascript:alert(1)", "JavaScript:alert(1)",
                    "data:text/html,<script>alert(1)</script>",
                    "  javascript:alert(1)  "):
            with self.assertRaises(ValueError, msg=bad):
                compliance._safe_url(bad)

    def test_http_and_https_survive_untouched(self):
        for ok in ("https://sos.example.gov/filing",
                   "http://example.dev/x?y=1"):
            self.assertEqual(compliance._safe_url(ok), ok)

    def test_empty_stays_empty(self):
        self.assertIsNone(compliance._safe_url(""))
        self.assertIsNone(compliance._safe_url(None))
        self.assertIsNone(compliance._safe_url("   "))

    def test_a_relative_path_is_refused_rather_than_guessed_at(self):
        """Guessing a scheme would be inventing the user's intent, and the
        page opens these in a new tab."""
        with self.assertRaises(ValueError):
            compliance._safe_url("sos.example.gov/filing")
